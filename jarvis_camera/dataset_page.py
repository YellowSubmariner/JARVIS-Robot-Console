"""Local demonstration library and review UI.

Long-running preparation, validation and export use one worker at a time.
Workers only put messages on a queue; Tk widgets are touched on the UI thread.
No robot connection, motion command, model download or dependency install lives here.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from PIL import Image, ImageTk

from .settings import app_directory
from .typography import FONT


BG = '#0a101b'
CARD = '#111e30'
TEXT = '#e7eef8'
MUTED = '#91a3bc'
CYAN = '#5ddac6'
AMBER = '#f4c779'
MAX_QUEUED_JOBS = 32
REVIEW_KEYS = ('alignment', 'position_targets', 'stable_end')
INCREMENTAL_MANIFEST = 'jarvis_incremental_manifest.json'


def read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def scan_episodes(root: Path) -> list[dict]:
    """Read only small metadata here; export revalidates full source fingerprints."""
    if not root.is_dir():
        return []
    entries = []
    for path in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not path.is_dir() or not (path / 'metadata.json').is_file():
            continue
        raw = read_json_object(path / 'metadata.json')
        prepared = read_json_object(path / 'prepared' / 'manifest.json')
        review = read_json_object(path / 'prepared' / 'review.json')
        checks = review.get('checks') if isinstance(review.get('checks'), dict) else {}
        reviewed = bool(raw.get('recording_status') == 'complete'
                        and raw.get('user_outcome') == 'success'
                        and review and prepared.get('technical_pass') is True
                        and prepared.get('status') == 'ready_for_review'
                        and prepared.get('source_fingerprint')
                        and prepared.get('prepared_signature')
                        and review.get('reviewed_for_export') is True
                        and all(checks.get(key) is True for key in REVIEW_KEYS)
                        and review.get('source_fingerprint') == prepared.get('source_fingerprint')
                        and review.get('prepared_signature') == prepared.get('prepared_signature'))
        if not raw:
            status = '记录无法读取'
        elif prepared.get('status') == 'blocked':
            status = '需要处理'
        elif reviewed:
            status = '已检查（导出时复核）'
        elif prepared.get('status') == 'ready_for_review':
            status = '等待检查'
        elif raw.get('recording_status') != 'complete':
            status = '录制未完整'
        else:
            status = '尚未整理'
        entries.append({'path': path, 'task': str(raw.get('task') or '未填写任务'),
                        'outcome': str(raw.get('user_outcome') or '未结束'),
                        'status': status, 'reviewed': reviewed,
                        'raw': raw, 'prepared': prepared})
    return entries


def safe_image_path(episode: Path, relative: str) -> Path:
    """A recording index must not make the viewer read an unrelated local file."""
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError('图片索引必须使用样本内的相对路径。')
    root = episode.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('图片不存在或路径超出本条样本。')
    return resolved


def recovery_initial_dir(last_item, recordings_root: Path) -> Path:
    """Prefer this session's recoverable item, then the recording recovery area."""
    if last_item:
        item = Path(last_item)
        if item.is_dir():
            return item
    root = Path(recordings_root)
    area = root / '.jarvis_trash'
    return area if area.is_dir() else root


def _run_conversion_worker(worker: Path, arguments: list[str], python: Path, progress) -> dict:
    """Run the worker's JSON progress protocol without opening a window."""
    if not python.is_file() or not worker.is_file():
        raise ValueError('转换程序或所选 Python 不存在。')
    result = None
    reported_error = None
    process = subprocess.Popen(
        [str(python), str(worker), *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'progress':
                progress(event)
            elif event.get('type') == 'result':
                result = event
            elif event.get('type') == 'error':
                reported_error = str(event.get('message') or '转换失败。')
        code = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        # Normal close waits for the worker; it must not truncate a dataset.
        if process.poll() is None:
            process.wait()
    if code != 0 or result is None or reported_error:
        raise ValueError(reported_error or '转换未完成。请确认所选环境已经安装兼容的 LeRobot 和视频依赖。')
    return result


def run_conversion(bundle: Path, output: Path, python: Path, progress) -> dict:
    """Convert an existing ordinary bundle with the legacy conversion worker."""
    worker = Path(__file__).resolve().with_name('lerobot_export_worker.py')
    return _run_conversion_worker(worker, ['--bundle', str(bundle), '--output', str(output)], python, progress)


def run_incremental_conversion(bundle: Path, output: Path, python: Path, progress,
                               prior: Path | None = None) -> dict:
    """Create a new dataset, optionally reusing a verified earlier generation."""
    worker = Path(__file__).resolve().with_name('incremental_export.py')
    arguments = ['--bundle', str(bundle), '--output', str(output)]
    if prior is not None:
        arguments.extend(('--prior', str(prior)))
    return _run_conversion_worker(worker, arguments, python, progress)


def is_incremental_candidate(path: Path) -> bool:
    """Quick structural check, never a substitute for worker integrity checks."""
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        return False
    manifest_path = path / INCREMENTAL_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    manifest = read_json_object(manifest_path)
    return (type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1
            and manifest.get('format') == 'jarvis_incremental_lerobot_v1'
            and manifest.get('complete') is True
            and isinstance(manifest.get('episodes'), list)
            and isinstance(manifest.get('files_sha256'), dict))


def find_latest_incremental(parent: Path) -> Path | None:
    """Find a candidate manifest; the worker must verify it before any reuse.

    Old exports contain no per-episode provenance and cannot be auto-reused.
    A newer invalid or incomplete manifest must not silently fall back to an
    older generation or cause a full re-encode. The worker revalidates all data.
    """
    if not parent.is_dir():
        return None
    candidates = []
    for path in parent.iterdir():
        if not path.name.startswith('lerobot_'):
            continue
        if path.is_symlink() or path.is_junction():
            raise ValueError('发现链接形式的转换目录，不能安全选择历史记录；请检查目录或勾选“重新建立（全部转换）”。')
        if not path.is_dir():
            continue
        manifest_path = path / INCREMENTAL_MANIFEST
        try:
            # lstat includes broken links or unexpected directories as invalid
            # candidates instead of silently falling back to an older export.
            stat = manifest_path.lstat()
        except FileNotFoundError:
            continue  # legacy export: no provenance manifest at all
        except OSError as error:
            raise ValueError('无法读取转换记录，请检查目录权限或勾选“重新建立（全部转换）”。') from error
        candidates.append((stat.st_mtime_ns, path.name, path))
    if not candidates:
        return None
    latest = max(candidates)[2]
    if not is_incremental_candidate(latest):
        raise ValueError('最新转换记录不完整或无效，未重新转换；请检查记录或勾选“重新建立（全部转换）”。')
    return latest


def check_conversion_environment(python: Path) -> dict:
    """Check the selected standalone interpreter without opening a window or network."""
    worker = Path(__file__).resolve().with_name('lerobot_export_worker.py')
    if not python.is_file() or not worker.is_file():
        raise ValueError('转换 Python 或转换组件不存在，请重新配置。')
    script = r'''
import importlib.util, json, sys
try:
    spec = importlib.util.spec_from_file_location('jarvis_conversion_worker', sys.argv[1])
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    factory, info = worker._load_dataset_factory()
    import numpy, PIL, av
    print(json.dumps({'ok': True, 'revision': info['revision'], 'version': info['version']}))
except Exception as error:
    print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
    sys.exit(1)
'''
    try:
        result = subprocess.run([str(python), '-c', script, str(worker)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                                timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise ValueError('转换环境检查超时，请检查该 Python 是否可用。') from error
    except OSError as error:
        raise ValueError('无法启动所选 Python，请检查路径和权限。') from error
    parsed = None
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'ok' in value:
            parsed = value
            break
    if result.returncode or not parsed or parsed.get('ok') is not True:
        error = str((parsed or {}).get('error') or '缺少 LeRobot 或视频转换依赖。')
        raise ValueError('转换环境检查未通过：' + error)
    return parsed


def perform_job(kind: str, payload: dict, progress) -> object:
    """Worker-only entry point, separated so orchestration can be tested offline."""
    if kind in ('prepare', 'load', 'approve'):
        from .preparation import prepare_episode, load_prepared_episode, approve_episode
        if kind == 'prepare':
            return prepare_episode(payload['episode'])
        if kind == 'load':
            return load_prepared_episode(payload['episode'], require_review=False)
        return approve_episode(payload['episode'], payload['checks'])
    if kind == 'export':
        from .dataset_export import export_bundle
        return export_bundle(payload['episodes'], payload['output'], seed=42, progress=progress)
    if kind == 'convert':
        return run_conversion(payload['bundle'], payload['output'], payload['python'], progress)
    if kind == 'check_env':
        return check_conversion_environment(payload['python'])
    if kind in ('trash_episode', 'trash_dataset', 'restore_trash'):
        from .trash_manager import (move_episode_to_trash, move_dataset_to_trash,
                                    restore_from_trash)
        protected = payload.get('protected_paths', ())
        if kind == 'trash_episode':
            return move_episode_to_trash(payload['path'], payload['recordings_root'],
                                         protected_paths=protected)
        if kind == 'trash_dataset':
            return move_dataset_to_trash(payload['path'], payload['recordings_root'],
                                         protected_paths=protected)
        return restore_from_trash(payload['path'], protected_paths=protected)
    if kind == 'direct_export':
        from .dataset_export import export_bundle
        output = Path(payload['output'])
        if output.exists() or output.is_symlink():
            raise FileExistsError('输出目录已经存在，未覆盖。')
        progress({'message': '正在检查转换环境…'})
        check_conversion_environment(payload['python'])
        if payload.get('rebuild') is True:
            prior = None
            progress({'message': '已选择重新建立，将转换全部示范；原始数据和旧导出不受影响。'})
        else:
            saved_prior = payload.get('prior_path')
            if saved_prior:
                prior = Path(saved_prior)
                if not is_incremental_candidate(prior):
                    raise ValueError('上次转换记录不可用，未重新转换。请检查原路径，或勾选“重新建立（全部转换）”。')
            else:
                prior = find_latest_incremental(output.parent)
            if prior is None:
                progress({'message': '没有可用转换记录，将重新处理全部示范；旧导出不受影响。'})
            else:
                progress({'message': '正在核对之前的转换记录：' + str(prior)})
        # TemporaryDirectory owns this new, uniquely named staging directory;
        # the original recordings and any prior exports remain untouched.
        with tempfile.TemporaryDirectory(prefix='.jc-', dir=output.parent) as folder:
            bundle = Path(folder) / 'bundle'
            progress({'message': '正在检查并整理已审核示范…'})
            export_bundle(payload['episodes'], bundle, seed=42, progress=progress)
            progress({'message': '正在检查已有数据并转换新增示范…'})
            return run_incremental_conversion(bundle, output, payload['python'], progress, prior=prior)
    raise ValueError('未知的样本处理任务。')


class DatasetPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.entries = []
        self._tree_paths = {}
        self._jobs = deque()
        self._events = queue.Queue()
        self._active = None
        self._thread = None
        self._next_job_id = 0
        self._closing = False
        self._close_callback = None
        self._dirty = True
        self._view_episode = None
        self._rows = []
        self._view_manifest = {}
        self._photos = {}
        self._preview_after = None
        self._current_approved = False
        self._last_bundle = None
        self._last_trash_item = None
        self._dataset_python = self._saved_dataset_python()
        self._dataset_prior = self._saved_prior_export()
        self.environment_text = tk.StringVar(value=self._environment_description())
        self.prior_text = tk.StringVar(value=self._prior_description())
        self.status_text = tk.StringVar(value='录完一条示范后，会自动整理到这里。')
        self.folder_text = tk.StringVar(value='')
        self.details_text = tk.StringVar(value='选择一条样本，查看两路画面及记录结果。')
        self.sample_text = tk.StringVar(value='')
        self.rebuild_export = tk.BooleanVar(value=False)
        self.review_vars = {key: tk.BooleanVar(value=False) for key in REVIEW_KEYS}
        self._build()
        self._set_controls()

    @property
    def busy(self):
        return self._active is not None or bool(self._jobs)

    @property
    def exporting(self):
        blocking = ('export', 'convert', 'direct_export', 'trash_episode', 'trash_dataset', 'restore_trash')
        return bool((self._active and self._active['kind'] in blocking)
                    or any(job['kind'] in blocking for job in self._jobs))

    def _recording_busy(self):
        record = getattr(self.app, 'pages', {}).get('record')
        return bool(record is not None and getattr(record, 'busy', False))

    def _recording_root(self):
        record = getattr(self.app, 'pages', {}).get('record')
        folder = getattr(record, 'folder', None)
        value = folder.get().strip() if folder is not None else ''
        return Path(value).expanduser() if value else app_directory() / 'recordings'

    def _saved_dataset_python(self):
        settings = getattr(self.app, 'settings', None)
        try:
            saved = str(settings.get('dataset_python') or '').strip() if settings else ''
        except KeyError:
            saved = ''
        # An explicit choice always wins, even if it is currently unavailable:
        # the user needs to see that state rather than silently switch Python.
        if saved:
            return saved
        bundled_environment = app_directory().parent / 'JARVIS-LeRobot-Environment' / 'Scripts' / 'python.exe'
        return str(bundled_environment) if bundled_environment.is_file() else ''

    def _environment_description(self):
        if not self._dataset_python:
            return '转换环境：尚未配置（请点“配置转换环境”）'
        if not Path(self._dataset_python).is_file():
            return '转换环境：所选 Python 不存在，请重新配置  ' + self._dataset_python
        return '转换环境：已选择，尚未检查  ' + self._dataset_python

    def _saved_prior_export(self):
        settings = getattr(self.app, 'settings', None)
        try:
            return str(settings.get('dataset_last_export') or '').strip() if settings else ''
        except KeyError:
            return ''

    def _prior_description(self):
        if not self._dataset_prior:
            return '已有转换：尚未选择；导出时会检查保存目录。'
        return '已有转换：' + self._dataset_prior + '（复用前核验）'

    def _label(self, parent, text='', size=10, color=TEXT, **kwargs):
        return tk.Label(parent, text=text, bg=kwargs.pop('bg', BG), fg=color,
                        font=(FONT, size), **kwargs)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                         bg=CYAN if primary else '#21334d', fg='#08231f' if primary else TEXT,
                         activebackground='#88e8d9' if primary else '#2a4263',
                         disabledforeground='#63748b', font=(FONT, 10, 'bold'),
                         cursor='hand2', padx=11, pady=7)

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky='ew')
        self._label(header, '示范样本', 21).pack(side='left')
        self.check_env_button = self._button(header, '检查环境', self._check_environment)
        self.check_env_button.pack(side='right', padx=(7, 0))
        self.configure_env_button = self._button(header, '配置转换环境', self._configure_environment)
        self.configure_env_button.pack(side='right')
        toolbar = tk.Frame(self, bg=BG)
        toolbar.grid(row=1, column=0, sticky='ew', pady=(10, 6))
        self.refresh_button = self._button(toolbar, '刷新', self.refresh)
        self.prepare_button = self._button(toolbar, '整理所选', self._prepare_selected)
        self.view_button = self._button(toolbar, '查看与检查', self._load_selected)
        self.direct_export_button = self._button(toolbar, '直接导出 LeRobot', self._direct_export, True)
        self.export_button = self._button(toolbar, '导出已检查样本', self._export)
        self.convert_button = self._button(toolbar, '转为 LeRobot…', self._convert)
        for widget in (self.refresh_button, self.prepare_button, self.view_button,
                       self.direct_export_button, self.export_button, self.convert_button):
            widget.pack(side='left', padx=(0, 7))
        folders = tk.Frame(self, bg=BG)
        folders.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        self._label(folders, textvariable=self.folder_text, size=9, color=MUTED,
                    anchor='w').pack(fill='x')
        self._label(folders, textvariable=self.environment_text, size=9, color=MUTED,
                    anchor='w').pack(fill='x')
        self._label(folders, textvariable=self.prior_text, size=9, color=MUTED,
                    anchor='w').pack(fill='x')
        reuse_options = tk.Frame(folders, bg=BG)
        reuse_options.pack(fill='x')
        self.choose_prior_button = self._button(reuse_options, '选择已有增量数据集', self._choose_prior)
        self.choose_prior_button.pack(side='left', padx=(0, 12))
        self.rebuild_check = tk.Checkbutton(
            reuse_options, text='重新建立（全部转换，保留旧结果）', variable=self.rebuild_export,
            bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            selectcolor=CARD, font=(FONT, 9), anchor='w')
        self.rebuild_check.pack(side='left')
        removal_options = tk.Frame(folders, bg=BG)
        removal_options.pack(fill='x', pady=(3, 0))
        self.trash_episode_button = self._button(removal_options, '删除选中示范', self._trash_episode)
        self.trash_dataset_button = self._button(removal_options, '删除导出数据集', self._trash_dataset)
        self.restore_trash_button = self._button(removal_options, '恢复已删除数据', self._restore_trash)
        for widget in (self.trash_episode_button, self.trash_dataset_button, self.restore_trash_button):
            widget.pack(side='left', padx=(0, 7))
        self._label(removal_options, '移入软件回收区，可恢复；暂不释放空间',
                    size=9, color=MUTED).pack(side='left')

        body = tk.PanedWindow(self, orient='horizontal', bg=BG, bd=0,
                              sashwidth=7, sashrelief='flat')
        body.grid(row=3, column=0, sticky='nsew')
        library = tk.Frame(body, bg=CARD)
        library.rowconfigure(0, weight=1)
        library.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(library, columns=('task', 'outcome', 'status'),
                                 show='headings', selectmode='browse', height=12)
        for name, title, width in (('task', '任务', 160), ('outcome', '结果', 58),
                                   ('status', '数据', 130)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=45, stretch=name == 'task')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(library, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Double-1>', lambda _event: self._load_selected())
        body.add(library, minsize=250, width=350)

        review_panel = tk.Frame(body, bg=BG)
        review_panel.columnconfigure(0, weight=1)
        review_panel.rowconfigure(0, weight=1)
        review_canvas = tk.Canvas(review_panel, bg=BG, highlightthickness=0)
        review_canvas.grid(row=0, column=0, sticky='nsew')
        review_scroll = ttk.Scrollbar(review_panel, orient='vertical', command=review_canvas.yview)
        review_scroll.grid(row=0, column=1, sticky='ns')
        review_canvas.configure(yscrollcommand=review_scroll.set)
        review = tk.Frame(review_canvas, bg=BG)
        review_window = review_canvas.create_window((0, 0), window=review, anchor='nw')
        review.bind('<Configure>', lambda _event: review_canvas.configure(scrollregion=review_canvas.bbox('all')))
        review.columnconfigure(0, weight=1)
        review.rowconfigure(1, weight=1)
        self.details_label = self._label(review, textvariable=self.details_text, size=9, color=MUTED,
                                         anchor='w', justify='left', wraplength=660)
        self.details_label.grid(row=0, column=0, sticky='ew')
        previews = tk.Frame(review, bg=BG)
        previews.grid(row=1, column=0, sticky='nsew', pady=6)
        previews.columnconfigure((0, 1), weight=1, uniform='preview')
        previews.rowconfigure(1, weight=1)
        self.previews = {}
        for i, (key, title) in enumerate((('top', '外部相机 · 历史样本'), ('wrist', '腕部相机 · 历史样本'))):
            self._label(previews, title, size=9, color=MUTED).grid(row=0, column=i, sticky='w', padx=3)
            canvas = tk.Canvas(previews, bg='#050a12', width=280, height=180,
                                highlightbackground='#24354d', highlightthickness=1)
            canvas.grid(row=1, column=i, sticky='nsew', padx=3)
            canvas.bind('<Configure>', lambda _event: self._schedule_preview())
            self.previews[key] = canvas
        self.slider = tk.Scale(review, from_=0, to=0, orient='horizontal',
                               showvalue=False, resolution=1, bg=BG, fg=TEXT,
                               troughcolor='#21334d', highlightthickness=0,
                               command=lambda _value: self._schedule_preview())
        self.slider.grid(row=2, column=0, sticky='ew')
        self.sample_label = self._label(review, textvariable=self.sample_text, size=9, color=TEXT,
                                        anchor='w', justify='left', wraplength=660)
        self.sample_label.grid(row=3, column=0, sticky='ew', pady=4)
        self._label(review, '导出前请检查（不会自动勾选）：', size=10).grid(row=4, column=0, sticky='w', pady=(5, 2))
        explanations = (
            ('alignment', '已回看整段：两路画面、关节和夹爪变化对应，没有明显错位。'),
            ('position_targets', '团队已验证可跟踪这种关节和夹爪位置目标；夹爪标签是连续实测位置重建，不是手柄原始指令。'),
            ('stable_end', '示范实际成功，末尾已放稳并保持终态，没有混入复位或人工介入。'),
        )
        self.review_checks = []
        for offset, (key, explanation) in enumerate(explanations):
            check = tk.Checkbutton(review, text=explanation, variable=self.review_vars[key],
                                    bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                                    selectcolor=CARD, wraplength=650, justify='left', anchor='w',
                                    font=(FONT, 9), command=self._set_controls)
            check.grid(row=5 + offset, column=0, sticky='ew')
            self.review_checks.append(check)
        self.approve_button = self._button(review, '保存检查结果', self._approve, True)
        self.approve_button.grid(row=8, column=0, sticky='w', pady=(7, 0))
        def resize_review(event):
            review_canvas.itemconfigure(review_window, width=max(1, event.width))
            wrap = max(150, event.width - 14)
            self.details_label.configure(wraplength=wrap)
            self.sample_label.configure(wraplength=wrap)
            for check in self.review_checks:
                check.configure(wraplength=max(130, wrap - 30))

        review_canvas.bind('<Configure>', resize_review)
        body.add(review_panel, minsize=470, stretch='always')
        self.status_label = self._label(self, textvariable=self.status_text, size=10, color=CYAN,
                                        anchor='w', justify='left', wraplength=1040)
        self.status_label.grid(row=4, column=0, sticky='ew', pady=(9, 0))
        self.bind('<Configure>', lambda event: self.status_label.configure(wraplength=max(250, event.width - 10)))

    def _selected(self):
        selection = self.tree.selection()
        return self._tree_paths.get(selection[0]) if selection else None

    def _selection_changed(self, _event=None):
        selected = self._selected()
        if selected != self._view_episode:
            self._view_episode = None
            self._rows = []
            self._current_approved = False
            for variable in self.review_vars.values():
                variable.set(False)
            self.sample_text.set('')
            self.slider.configure(to=0)
            self._schedule_preview()
            entry = next((x for x in self.entries if x['path'] == selected), None)
            if entry:
                issues = entry['prepared'].get('issues', [])
                issues = sorted((issue for issue in issues if isinstance(issue, dict)),
                                key=lambda issue: issue.get('severity') != 'error')
                messages = [str(issue.get('message') or issue.get('code')) for issue in issues]
                self.details_text.set(entry['task'] + '\n' + ( '\n'.join(messages[:4])
                                      if messages else '点击“查看与检查”回放本条示范。'))
        self._set_controls()

    def mark_dirty(self):
        self._dirty = True

    def refresh(self):
        selected = self._selected()
        root = self._recording_root()
        self.folder_text.set('录制目录：' + str(root))
        try:
            self.entries = scan_episodes(root)
        except OSError:
            self.entries = []
            self.status_text.set('无法读取录制目录，请检查保存位置。')
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._tree_paths = {}
        labels = {'success': '成功', 'failed': '失败', 'intervention': '介入',
                  'incomplete': '未完整', '未结束': '未结束'}
        for i, entry in enumerate(self.entries):
            item = f'episode_{i}'
            self._tree_paths[item] = entry['path']
            self.tree.insert('', 'end', iid=item, values=(entry['task'],
                             labels.get(entry['outcome'], entry['outcome']), entry['status']))
            if entry['path'] == selected:
                self.tree.selection_set(item)
        self._dirty = False
        self._set_controls()

    def _set_controls(self):
        available = not self._closing
        selected = self._selected() if hasattr(self, 'tree') else None
        idle = available and not self.busy
        recording = self._recording_busy()
        self.refresh_button.configure(state='normal' if available else 'disabled')
        self.prepare_button.configure(state='normal' if idle and selected and not recording else 'disabled')
        self.view_button.configure(state='normal' if idle and selected else 'disabled')
        self.export_button.configure(state='normal' if idle and not recording else 'disabled')
        self.convert_button.configure(state='normal' if idle and not recording else 'disabled')
        self.direct_export_button.configure(state='normal' if idle and not recording else 'disabled')
        self.configure_env_button.configure(state='normal' if idle and not recording else 'disabled')
        self.check_env_button.configure(state='normal' if idle and not recording else 'disabled')
        self.choose_prior_button.configure(state='normal' if idle and not recording else 'disabled')
        self.rebuild_check.configure(state='normal' if idle and not recording else 'disabled')
        self.trash_episode_button.configure(state='normal' if idle and selected and not recording else 'disabled')
        self.trash_dataset_button.configure(state='normal' if idle and not recording else 'disabled')
        self.restore_trash_button.configure(state='normal' if idle and not recording else 'disabled')
        view_ready = bool(idle and self._rows and self._view_episode == selected)
        for widget in self.review_checks:
            widget.configure(state='normal' if view_ready else 'disabled')
        accepted = all(v.get() for v in self.review_vars.values())
        self.approve_button.configure(state='normal' if view_ready and accepted and not self._current_approved else 'disabled')

    def _enqueue(self, kind, payload):
        if self._closing:
            return False
        if len(self._jobs) >= MAX_QUEUED_JOBS:
            self.status_text.set('等待整理的样本较多；原始记录已保留，稍后可点“整理所选”。')
            return False
        self._next_job_id += 1
        self._jobs.append({'id': self._next_job_id, 'kind': kind, 'payload': payload})
        self._set_controls()
        self._start_next()
        return True

    def enqueue_episode(self, episode: Path):
        path = Path(episode)
        matching = lambda j: j and j['kind'] == 'prepare' and j['payload']['episode'] == path
        if matching(self._active) or any(matching(job) for job in self._jobs):
            return False
        self._dirty = True
        return self._enqueue('prepare', {'episode': path})

    def _start_next(self):
        if self._active is not None or not self._jobs:
            return
        # Avoid beginning another large read while raw camera recording is busy.
        if self._recording_busy() and self._jobs[0]['kind'] == 'prepare' and not self._closing:
            return
        job = self._jobs.popleft()
        self._active = job
        verbs = {'prepare': '正在整理示范…', 'load': '正在检查并读取样本…',
                 'approve': '正在保存检查结果…', 'export': '正在导出已检查样本…',
                 'convert': '正在转换 LeRobot 数据…',
                 'direct_export': '正在直接导出 LeRobot 数据…',
                 'check_env': '正在检查转换环境…',
                 'trash_episode': '正在将示范移入回收区…',
                 'trash_dataset': '正在将数据集移入回收区…',
                 'restore_trash': '正在恢复数据…'}
        self.status_text.set(verbs[job['kind']])
        self._thread = threading.Thread(target=self._work, args=(job,),
                                        name='dataset-' + job['kind'], daemon=True)
        self._thread.start()
        self._set_controls()

    def _work(self, job):
        try:
            result = perform_job(job['kind'], job['payload'],
                                 lambda info: self._events.put(('progress', job['id'], info)))
            self._events.put(('result', job['id'], result))
        except Exception as error:
            self._events.put(('error', job['id'], str(error) or type(error).__name__))

    def update_library(self):
        if self._dirty:
            self.refresh()
        while True:
            try:
                event, job_id, value = self._events.get_nowait()
            except queue.Empty:
                break
            if not self._active or self._active['id'] != job_id:
                continue
            if event == 'progress':
                if isinstance(value, dict):
                    self.status_text.set(str(value.get('message') or '正在处理数据…'))
                continue
            job = self._active
            self._active = None
            self._thread = None
            if event == 'error':
                self.status_text.set('处理未完成：' + str(value))
                if job['kind'] == 'check_env':
                    self.environment_text.set('转换环境：检查未通过  ' + self._dataset_python)
                elif job['kind'] == 'direct_export':
                    self.status_text.set('处理未完成：' + str(value) +
                        ' 如转换记录失效，可勾选“重新建立（全部转换）”再导出；旧数据保留。')
            else:
                self._completed(job, value)
            self._dirty = True
        if self._dirty:
            self.refresh()
        self._start_next()
        self._set_controls()
        if self._closing and not self.busy and self._close_callback is not None:
            callback, self._close_callback = self._close_callback, None
            callback()

    def _completed(self, job, result):
        kind = job['kind']
        if kind == 'prepare':
            if result.get('status') == 'ready_for_review':
                self.status_text.set('示范已自动整理，请在“查看与检查”中回看并确认。')
            else:
                issues = [issue for issue in result.get('issues', []) if isinstance(issue, dict)]
                issue = next((issue for issue in issues if issue.get('severity') == 'error'),
                             issues[0] if issues else {})
                message = str(issue.get('message') or '请查看质量报告。')
                self.status_text.set('本条示范需要处理：' + message)
            if self._view_episode == job['payload']['episode']:
                self._view_episode = None
                self._rows = []
        elif kind == 'load':
            episode = job['payload']['episode']
            if episode != self._selected():
                return
            self._view_manifest, self._rows = result
            self._view_episode = episode
            self._current_approved = False
            for variable in self.review_vars.values():
                variable.set(False)
            self.slider.configure(to=max(0, len(self._rows) - 1))
            self.slider.set(0)
            self.details_text.set(str(self._view_manifest.get('task') or '') + '\n'
                                  '拖动时间条回看整段；以下为重建的位置目标，不是手柄原始指令。')
            self.status_text.set('样本已打开。请检查整段后，再逐项确认。')
            self._schedule_preview()
        elif kind == 'approve':
            self._current_approved = self._view_episode == job['payload']['episode']
            self.status_text.set('检查结果已保存。导出时会再次核对原始文件是否变化。')
        elif kind == 'export':
            self._last_bundle = Path(result.get('output_dir') or job['payload']['output'])
            counts = result.get('counts') or {}
            self.status_text.set(f"已导出样本包：训练 {counts.get('train', 0)} 条、验证 {counts.get('val', 0)} 条、测试 {counts.get('test', 0)} 条。位置：{self._last_bundle}")
        elif kind in ('convert', 'direct_export'):
            saved_path = str(result.get('output_dir') or job['payload']['output'])
            incremental = result.get('incremental') or {}
            if kind == 'direct_export' and isinstance(incremental, dict) and 'reused' in incremental and 'converted' in incremental:
                self.status_text.set(f"已复用 {incremental['reused']} 条，新转换 {incremental['converted']} 条。位置：{saved_path}")
            else:
                self.status_text.set('LeRobot 数据已生成：' + saved_path)
            if kind == 'direct_export':
                self._dataset_prior = saved_path
                prior_text = getattr(self, 'prior_text', None)
                if prior_text is not None:
                    prior_text.set(self._prior_description())
                rebuild_export = getattr(self, 'rebuild_export', None)
                if rebuild_export is not None:
                    rebuild_export.set(False)
                settings = getattr(self.app, 'settings', None)
                try:
                    if settings is None:
                        raise OSError('settings unavailable')
                    settings.save(dataset_last_export=saved_path)
                except OSError:
                    self.status_text.set('本次路径未记住，下次自动复用无法保证；请修复写入权限并保留该数据集：' + saved_path)
        elif kind == 'check_env':
            if str(job['payload']['python']) == self._dataset_python:
                self.environment_text.set('转换环境：依赖检查通过（实际转换仍需验证）  ' + self._dataset_python)
            self.status_text.set('转换环境检查通过：LeRobot 固定版本与基础依赖可导入。')
        elif kind in ('trash_episode', 'trash_dataset'):
            if kind == 'trash_episode' and self._view_episode == job['payload']['path']:
                self._view_episode = None
                self._rows = []
                self._current_approved = False
                for variable in self.review_vars.values():
                    variable.set(False)
                self.slider.configure(to=0)
                self.sample_text.set('')
                self.details_text.set('示范已移入回收区，可使用“恢复已删除数据”。')
                self._schedule_preview()
            location = str(result['trash_item'])
            self._last_trash_item = Path(location)
            self.status_text.set('已移入软件回收区，不会立即释放磁盘空间。回收位置：' + location)
            messagebox.showinfo('已移入回收区',
                '数据尚可恢复，也尚未释放磁盘空间。\n\n回收位置：\n' + location +
                '\n\n可点击“恢复已删除数据”并选择该回收文件夹。', parent=self.app)
        elif kind == 'restore_trash':
            location = str(result['original_path'])
            self._last_trash_item = None
            self.status_text.set('数据已恢复到：' + location)
            messagebox.showinfo('数据已恢复', '已恢复到原来的位置：\n' + location, parent=self.app)

    def _prepare_selected(self):
        selected = self._selected()
        if selected is None or self.busy or self._recording_busy():
            return
        self.enqueue_episode(selected)

    def _protected_paths(self):
        protected = [app_directory()]
        for text in (self._dataset_python,
                     getattr(getattr(self.app, 'pages', {}).get('record'), 'python_path', None)):
            if hasattr(text, 'get'):
                text = text.get()
            if text:
                path = Path(text).expanduser()
                # A virtual environment's Python lives in its Scripts folder.
                protected.append(path.parent.parent if path.parent.name.lower() == 'scripts' else path.parent)
        return protected

    def _trash_episode(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        path = self._selected()
        if path is None:
            messagebox.showinfo('先选择示范', '请在左侧选择要删除的单条示范。', parent=self.app)
            return
        entry = next((x for x in self.entries if x['path'] == path), None)
        if entry is None:
            return
        outcome = {'success': '成功', 'failed': '失败', 'intervention': '人工介入',
                   'incomplete': '未完整'}.get(entry.get('outcome'), '未知')
        confirmed = messagebox.askyesno('删除这条原始示范？',
            f"任务：{entry['task']}\n结果：{outcome}\n目录：{path}\n\n"
            '将整条示范（原始图片、机器人反馈和检查结果）移入软件回收区，可恢复，暂不释放磁盘空间。'
            '已导出的数据集不会自动改变；若要从之后的数据集中排除此示范，需要重新建立数据集。\n\n'
            '请先在其他打开的软件版本中结束录制和处理。确定移入回收区吗？', parent=self.app)
        if confirmed and not self.busy and not self._recording_busy() and not self._closing:
            self._enqueue('trash_episode', {'path': path, 'recordings_root': self._recording_root(),
                                            'protected_paths': self._protected_paths()})

    def _trash_dataset(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        path = filedialog.askdirectory(title='选择要删除的已导出 LeRobot 数据集文件夹',
                                       initialdir=str(self._recording_root().parent), parent=self.app)
        if not path:
            return
        path = Path(path)
        confirmed = messagebox.askyesno('删除这个导出数据集？',
            f'数据集目录：{path}\n\n'
            '仅将这个已导出数据集移入软件回收区，原始示范不会改变；可恢复，暂不释放磁盘空间。'
            '若它是上次用于增量复用的数据集，之后需恢复它、选择其他已有数据集或重新建立。\n\n'
            '请先在其他打开的软件版本中结束录制和处理。确定移入回收区吗？', parent=self.app)
        if confirmed and not self.busy and not self._recording_busy() and not self._closing:
            self._enqueue('trash_dataset', {'path': path, 'recordings_root': self._recording_root(),
                                            'protected_paths': self._protected_paths()})

    def _restore_trash(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        start = recovery_initial_dir(getattr(self, '_last_trash_item', None), self._recording_root())
        path = filedialog.askdirectory(title='选择回收区内的编号记录文件夹或其中的 payload 文件夹',
                                       initialdir=str(start), parent=self.app)
        if not path:
            return
        path = Path(path)
        confirmed = messagebox.askyesno('恢复这条回收记录？',
            f'回收记录：{path}\n\n'
            '数据将恢复到原来的位置；如果原位置已有内容，程序不会覆盖。'
            '请先在其他打开的软件版本中结束录制和处理。', parent=self.app)
        if confirmed and not self.busy and not self._recording_busy() and not self._closing:
            self._enqueue('restore_trash', {'path': path, 'protected_paths': self._protected_paths()})

    def _load_selected(self):
        selected = self._selected()
        if selected is not None and not self.busy and not self._closing:
            self._enqueue('load', {'episode': selected})

    def _approve(self):
        checks = {key: variable.get() for key, variable in self.review_vars.items()}
        if not self._rows or self._view_episode != self._selected() or self.busy or self._closing:
            return
        if not all(checks.values()):
            self.status_text.set('请实际检查后逐项确认，不能自动勾选。')
            return
        self._enqueue('approve', {'episode': self._view_episode, 'checks': checks})

    def _configure_environment(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        python = filedialog.askopenfilename(
            title='选择已安装 LeRobot 的转换 Python',
            initialdir=str(Path(self._dataset_python).parent) if self._dataset_python else None,
            filetypes=[('Python', 'python.exe'), ('所有文件', '*')], parent=self.app)
        if not python:
            return
        if not Path(python).is_file():
            messagebox.showerror('Python 不可用', '所选文件不存在。', parent=self.app)
            return
        self._dataset_python = str(Path(python))
        settings = getattr(self.app, 'settings', None)
        if settings:
            try:
                settings.save(dataset_python=self._dataset_python)
            except OSError:
                self.status_text.set('无法保存环境路径，本次仍可使用。')
        self.environment_text.set(self._environment_description())
        self._check_environment()

    def _check_environment(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        if not self._dataset_python or not Path(self._dataset_python).is_file():
            self.environment_text.set(self._environment_description())
            messagebox.showinfo('先配置转换环境', '请选择已经安装 LeRobot 的转换 Python。', parent=self.app)
            return
        self.environment_text.set('转换环境：正在检查…  ' + self._dataset_python)
        self._enqueue('check_env', {'python': Path(self._dataset_python)})

    def _choose_prior(self):
        if self.busy or self._recording_busy() or self._closing:
            return
        selected = filedialog.askdirectory(
            title='选择已有增量数据集（导出时会完整核验）',
            initialdir=self._dataset_prior or str(self._recording_root().parent), parent=self.app)
        if not selected:
            return
        path = Path(selected)
        if not is_incremental_candidate(path):
            messagebox.showerror('无法使用转换记录', '所选目录没有完整的增量来源清单，旧版导出不能直接复用。', parent=self.app)
            return
        self._dataset_prior = str(path)
        self.prior_text.set(self._prior_description())
        settings = getattr(self.app, 'settings', None)
        try:
            if settings is None:
                raise OSError('settings unavailable')
            settings.save(dataset_last_export=self._dataset_prior)
        except OSError:
            self.status_text.set('本次路径未记住，下次自动复用无法保证；请修复写入权限并保留该数据集：' + self._dataset_prior)
            return
        self.status_text.set('已选择此前的数据集，导出时会核验来源和文件完整性。')

    def _direct_export(self):
        if self.busy or self._recording_busy() or self._closing:
            self.status_text.set('请先结束录制并等待当前处理完成，再导出。')
            return
        if not self._dataset_python or not Path(self._dataset_python).is_file():
            messagebox.showinfo('先配置转换环境', '请先点击“配置转换环境”，选择已安装 LeRobot 的 Python。', parent=self.app)
            return
        self.refresh()
        candidates = [entry['path'] for entry in self.entries if entry['reviewed']]
        if len(candidates) < 3:
            messagebox.showinfo('先检查更多样本', '至少需要 3 条已检查的成功示范，才能分别保留训练、验证和测试数据。', parent=self.app)
            return
        folder = filedialog.askdirectory(title='选择 LeRobot 数据保存位置',
                                          initialdir=str(self._recording_root().parent), parent=self.app)
        if not folder:
            return
        output = Path(folder) / f'lerobot_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
        saved_prior = self._dataset_prior
        self._enqueue('direct_export', {'episodes': candidates, 'output': output,
                                        'python': Path(self._dataset_python),
                                        'prior_path': saved_prior or None,
                                        'rebuild': bool(self.rebuild_export.get())})

    def _export(self):
        if self.busy or self._recording_busy() or self._closing:
            self.status_text.set('请先结束录制并等待当前处理完成，再导出。')
            return
        self.refresh()
        candidates = [entry['path'] for entry in self.entries if entry['reviewed']]
        if len(candidates) < 3:
            messagebox.showinfo('先检查更多样本', '至少需要 3 条已检查的成功示范，才能分别保留训练、验证和测试数据。', parent=self.app)
            return
        folder = filedialog.askdirectory(title='选择样本包保存位置', parent=self.app)
        if not folder:
            return
        output = Path(folder) / f'dataset_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
        self._enqueue('export', {'episodes': candidates, 'output': output})

    def _convert(self):
        if self.busy or self._recording_busy() or self._closing:
            self.status_text.set('请先结束录制并等待当前处理完成，再转换。')
            return
        bundle = filedialog.askdirectory(title='选择已导出的样本包目录',
                                          initialdir=str(self._last_bundle or self._recording_root()), parent=self.app)
        if not bundle:
            return
        if not (Path(bundle) / 'bundle.json').is_file():
            messagebox.showinfo('选择样本包', '请选择“导出已检查样本”生成的完整样本包目录。', parent=self.app)
            return
        settings = getattr(self.app, 'settings', None)
        try:
            remembered = settings.get('dataset_python') if settings else ''
        except KeyError:
            remembered = ''
        python = filedialog.askopenfilename(title='选择已经安装 LeRobot 的 Python（不会自动安装或下载模型）',
                                            initialdir=str(Path(remembered).parent) if remembered else None,
                                            filetypes=[('Python', 'python.exe'), ('所有文件', '*')], parent=self.app)
        if not python:
            return
        if not Path(python).is_file():
            messagebox.showerror('Python 不可用', '所选文件不存在。', parent=self.app)
            return
        folder = filedialog.askdirectory(title='选择 LeRobot 数据保存位置', parent=self.app)
        if not folder:
            return
        if settings:
            try:
                settings.save(dataset_python=python)
            except OSError:
                self.status_text.set('无法记住 Python 路径，本次转换仍可继续。')
        self._dataset_python = str(Path(python))
        self.environment_text.set(self._environment_description())
        output = Path(folder) / f'lerobot_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
        self._enqueue('convert', {'bundle': Path(bundle), 'output': output, 'python': Path(python)})

    def _schedule_preview(self):
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(50, self._draw_preview)

    def _draw_preview(self):
        self._preview_after = None
        row = self._rows[min(int(self.slider.get()), len(self._rows) - 1)] if self._rows else None
        for name, canvas in self.previews.items():
            canvas.delete('all')
            width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
            try:
                if row is None or self._view_episode is None:
                    raise ValueError('选择样本后点击“查看与检查”')
                path = safe_image_path(self._view_episode, row['observation.images.' + name])
                with Image.open(path) as source:
                    preview = source.convert('RGB')
                    preview.thumbnail((max(1, width - 4), max(1, height - 4)), Image.Resampling.LANCZOS)
                self._photos[name] = ImageTk.PhotoImage(preview)
                canvas.create_image(width // 2, height // 2, image=self._photos[name], anchor='center')
            except (OSError, ValueError, KeyError):
                canvas.create_text(width // 2, height // 2,
                                   text='选择样本后查看' if row is None else '图片无法读取，请重新整理',
                                   fill=MUTED, width=max(1, width - 20), font=(FONT, 10))
        if row is not None:
            state, action = row['observation.state'], row['action']
            q = '  '.join(f'{math.degrees(float(x)):.1f}' for x in state[:7])
            target = '  '.join(f'{math.degrees(float(x)):.1f}' for x in action[:7])
            self.sample_text.set(f"{float(row['timestamp']):.2f} 秒｜关节角 J1–J7（度）\n当前：{q}\n下一步：{target}\n夹爪位置：当前 {float(state[7]) * 100:.1f}% → 下一步 {float(action[7]) * 100:.1f}%（实测位置重建）")

    def request_close(self, callback):
        self._closing = True
        self._close_callback = callback
        self._set_controls()
        if self.busy:
            self.status_text.set('正在完成已有数据处理后关闭，请稍候；不要强制结束程序。')
        else:
            self._close_callback = None
            callback()

    def destroy(self):
        if self._preview_after is not None:
            self.after_cancel(self._preview_after)
            self._preview_after = None
        super().destroy()
