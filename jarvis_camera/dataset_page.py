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


def run_conversion(bundle: Path, output: Path, python: Path, progress) -> dict:
    """Use an explicitly selected, already prepared interpreter; never install packages."""
    worker = Path(__file__).resolve().with_name('lerobot_export_worker.py')
    if not python.is_file() or not worker.is_file():
        raise ValueError('转换程序或所选 Python 不存在。')
    result = None
    reported_error = None
    process = subprocess.Popen(
        [str(python), str(worker), '--bundle', str(bundle), '--output', str(output)],
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
        self.status_text = tk.StringVar(value='录完一条示范后，会自动整理到这里。')
        self.folder_text = tk.StringVar(value='')
        self.details_text = tk.StringVar(value='选择一条样本，查看两路画面及记录结果。')
        self.sample_text = tk.StringVar(value='')
        self.review_vars = {key: tk.BooleanVar(value=False) for key in REVIEW_KEYS}
        self._build()
        self._set_controls()

    @property
    def busy(self):
        return self._active is not None or bool(self._jobs)

    @property
    def exporting(self):
        return bool((self._active and self._active['kind'] in ('export', 'convert'))
                    or any(job['kind'] in ('export', 'convert') for job in self._jobs))

    def _recording_busy(self):
        record = getattr(self.app, 'pages', {}).get('record')
        return bool(record is not None and getattr(record, 'busy', False))

    def _recording_root(self):
        record = getattr(self.app, 'pages', {}).get('record')
        folder = getattr(record, 'folder', None)
        value = folder.get().strip() if folder is not None else ''
        return Path(value).expanduser() if value else app_directory() / 'recordings'

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
        self._label(self, '示范样本', 21).grid(row=0, column=0, sticky='w')
        toolbar = tk.Frame(self, bg=BG)
        toolbar.grid(row=1, column=0, sticky='ew', pady=(10, 6))
        self.refresh_button = self._button(toolbar, '刷新', self.refresh)
        self.prepare_button = self._button(toolbar, '整理所选', self._prepare_selected)
        self.view_button = self._button(toolbar, '查看与检查', self._load_selected)
        self.export_button = self._button(toolbar, '导出已检查样本', self._export, True)
        self.convert_button = self._button(toolbar, '转为 LeRobot…', self._convert)
        for widget in (self.refresh_button, self.prepare_button, self.view_button,
                       self.export_button, self.convert_button):
            widget.pack(side='left', padx=(0, 7))
        self._label(self, textvariable=self.folder_text, size=9, color=MUTED,
                    anchor='w').grid(row=2, column=0, sticky='ew', pady=(0, 6))

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
                 'convert': '正在转换 LeRobot 数据…'}
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
        elif kind == 'convert':
            self.status_text.set('LeRobot 数据已生成：' + str(result.get('output_dir') or job['payload']['output']))

    def _prepare_selected(self):
        selected = self._selected()
        if selected is None or self.busy or self._recording_busy():
            return
        self.enqueue_episode(selected)

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
