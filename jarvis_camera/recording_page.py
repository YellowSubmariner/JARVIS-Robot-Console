"""Tk page for collecting raw demonstrations, without robot motion commands.

The camera and robot workers own their own I/O. This page coordinates their
asynchronous states. The robot password is stored only by the Windows
Credential Manager when configured, never in an episode or plain settings.
This page never marks data as training-ready.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import cv2
from PIL import Image, ImageTk

from .credentials import CredentialStoreError, WindowsCredentialStore
from .settings import app_directory
from .typography import FONT

BG = '#0a101b'
CARD = '#111e30'
BORDER = '#24354d'
TEXT = '#e7eef8'
MUTED = '#91a3bc'
CYAN = '#5ddac6'
GREEN = '#7cddb0'
AMBER = '#f4c779'


def _label(parent, text='', size=10, color=TEXT, bg=BG, bold=False, **kwargs):
    return tk.Label(parent, text=text, fg=color, bg=bg,
                    font=(FONT, size, 'bold' if bold else 'normal'), **kwargs)


def _button(parent, text, command, primary=False, **kwargs):
    return tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                     bg=CYAN if primary else '#21334d', fg='#08231f' if primary else TEXT,
                     activebackground='#88e8d9' if primary else '#2a4263',
                     disabledforeground='#63748b', font=(FONT, 10, 'bold'),
                     cursor='hand2', padx=13, pady=8, **kwargs)


def _clock():
    return {'wall_time_utc': datetime.now(timezone.utc).isoformat(),
            'monotonic_ns': time.perf_counter_ns(), 'clock': 'python_perf_counter_ns'}


class RecordingPage(tk.Frame):
    """Coordinate one raw recording using existing single-owner camera streams."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.state = 'idle'
        self.episode = None
        self.manifest = None
        self.robot_process = None
        self.robot_events = queue.Queue()
        self.robot_state = 'idle'
        self.robot_counts = {}
        self.robot_eof = False
        self.camera_started = set()
        self.camera_infos = {}
        self.warnings = []
        self.start_clock = 0.0
        self.stop_clock = 0.0
        self.pending_outcome = None
        self.close_callback = None
        self._photos = {}
        self._last_preview = 0.0

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.task = tk.StringVar(value='')
        settings = getattr(app, 'settings', None)
        self.folder = tk.StringVar(value=(settings.get('recordings_dir') if settings is not None
                                         else '') or str(app_directory() / 'recordings'))
        self.python_path = tk.StringVar(value=settings.get('kortex_python') if settings is not None else '')
        self.robot_ip = tk.StringVar(value='192.168.1.10')
        self.robot_user = tk.StringVar(value='admin')
        self.candidate_hz = tk.StringVar(value='20')
        self.controller_events = tk.BooleanVar(value=True)
        self.status_text = tk.StringVar(value='准备录制：先连接两路相机，再输入任务。')
        self.details_text = tk.StringVar(value='')
        self.path_text = tk.StringVar(value='数据目录：尚未开始')
        self._build()
        self._set_controls()

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        title_area = tk.Frame(header, bg=BG)
        title_area.pack(side='left', fill='x', expand=True)
        _label(title_area, '示范录制', 21, bold=True).pack(anchor='w')
        credential_controls = tk.Frame(header, bg=BG)
        credential_controls.pack(side='right', anchor='n')
        self.password_button = _button(credential_controls, '设置密码', self._set_password)
        self.password_button.pack(side='left')
        self.clear_password_button = _button(credential_controls, '清除密码', self._clear_password)
        self.clear_password_button.pack(side='left', padx=(8, 0))

        settings = tk.Frame(self, bg=CARD, padx=12, pady=10,
                            highlightbackground=BORDER, highlightthickness=1)
        settings.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        settings.columnconfigure(1, weight=1)
        _label(settings, '短任务', 10, MUTED, bg=CARD).grid(row=0, column=0, sticky='w', padx=(0, 8), pady=3)
        self.task_entry = tk.Entry(settings, textvariable=self.task, bg='#0b1524', fg=TEXT,
                                   insertbackground=CYAN, relief='flat', font=(FONT, 10), bd=6)
        self.task_entry.grid(row=0, column=1, columnspan=4, sticky='ew', pady=3)
        _label(settings, '保存位置', 10, MUTED, bg=CARD).grid(row=1, column=0, sticky='w', padx=(0, 8), pady=3)
        self.folder_entry = tk.Entry(settings, textvariable=self.folder, bg='#0b1524', fg=TEXT,
                                     insertbackground=CYAN, relief='flat', font=(FONT, 9), bd=6)
        self.folder_entry.grid(row=1, column=1, columnspan=3, sticky='ew', pady=3)
        self.folder_button = _button(settings, '选择', self._choose_folder)
        self.folder_button.grid(row=1, column=4, padx=(8, 0))
        _label(settings, '机器人地址', 10, MUTED, bg=CARD).grid(row=2, column=0, sticky='w', pady=3)
        self.ip_entry = tk.Entry(settings, textvariable=self.robot_ip, width=18, bg='#0b1524', fg=TEXT,
                                 insertbackground=CYAN, relief='flat', font=(FONT, 9), bd=6)
        self.ip_entry.grid(row=2, column=1, sticky='w', pady=3)
        _label(settings, '用户名', 9, MUTED, bg=CARD).grid(row=2, column=2, sticky='e', padx=5)
        self.user_entry = tk.Entry(settings, textvariable=self.robot_user, width=12, bg='#0b1524', fg=TEXT,
                                   insertbackground=CYAN, relief='flat', font=(FONT, 9), bd=6)
        self.user_entry.grid(row=2, column=3, sticky='w')
        _label(settings, 'Kortex Python', 9, MUTED, bg=CARD).grid(row=3, column=0, sticky='w', pady=3)
        self.python_entry = tk.Entry(settings, textvariable=self.python_path, bg='#0b1524', fg=TEXT,
                                     insertbackground=CYAN, relief='flat', font=(FONT, 9), bd=6)
        self.python_entry.grid(row=3, column=1, columnspan=3, sticky='ew', pady=3)
        self.python_button = _button(settings, '选择', self._choose_python)
        self.python_button.grid(row=3, column=4, padx=(8, 0))

        previews = tk.Frame(self, bg=BG)
        previews.grid(row=2, column=0, sticky='nsew')
        previews.columnconfigure((0, 1), weight=1, uniform='camera')
        previews.rowconfigure(1, weight=1)
        self.previews = {}
        for column, (name, title) in enumerate((('astra', '外部相机'), ('wrist', '腕部相机'))):
            _label(previews, title, 10, MUTED).grid(row=0, column=column, sticky='w', padx=5, pady=(0, 4))
            canvas = tk.Canvas(previews, bg='#050a12', bd=0, highlightbackground=BORDER, highlightthickness=1,
                               height=150)
            canvas.grid(row=1, column=column, sticky='nsew', padx=4)
            self.previews[name] = canvas

        controls = tk.Frame(self, bg=BG)
        controls.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        self.start_button = _button(controls, '开始录制', self.start, primary=True)
        self.start_button.pack(side='left')
        self.success_button = _button(controls, '结束：成功', lambda: self.stop('success'))
        self.success_button.pack(side='left', padx=(8, 0))
        self.failed_button = _button(controls, '结束：失败', lambda: self.stop('failed'))
        self.failed_button.pack(side='left', padx=(8, 0))
        self.intervention_button = _button(controls, '中止／人工接管', lambda: self.stop('intervention'))
        self.intervention_button.pack(side='left', padx=(8, 0))
        _label(self, textvariable=self.status_text, size=10, color=GREEN, anchor='w', justify='left',
               wraplength=900).grid(row=5, column=0, sticky='ew', pady=(5, 0))
        _label(self, textvariable=self.details_text, size=9, color=AMBER, anchor='w', justify='left',
               wraplength=900).grid(row=6, column=0, sticky='ew', pady=(5, 0))
        _label(self, textvariable=self.path_text, size=9, color=MUTED, anchor='w', justify='left',
               wraplength=900).grid(row=7, column=0, sticky='ew', pady=(5, 0))

    def _choose_folder(self):
        selected = filedialog.askdirectory(parent=self.app, title='选择示范数据保存目录')
        if selected:
            self.folder.set(selected)
            settings = getattr(self.app, 'settings', None)
            if settings is not None:
                try:
                    settings.save(recordings_dir=selected)
                except OSError:
                    self.status_text.set('本次保存位置已选择，但未能记住该位置。')

    def _choose_python(self):
        selected = filedialog.askopenfilename(parent=self.app, title='选择 Kortex 环境的 python.exe',
                                              filetypes=[('Python', 'python.exe'), ('All files', '*')])
        if selected:
            self.python_path.set(selected)
            self._remember_python_path()

    def _remember_python_path(self):
        """Remember only the interpreter path, never a robot credential."""
        settings = getattr(self.app, 'settings', None)
        if settings is not None:
            try:
                settings.save(kortex_python=self.python_path.get().strip())
            except OSError:
                self.status_text.set('Kortex Python 路径未能保存，下次打开时需要重新选择。')

    def _credential_identity(self):
        ip, username = self.robot_ip.get().strip(), self.robot_user.get().strip()
        if not ip or not username:
            messagebox.showinfo('检查连接', '请先填写机器人地址和用户名。', parent=self.app)
            return None
        return ip, username

    def _prompt_and_save_password(self, store, identity):
        password = simpledialog.askstring(
            '设置机械臂密码',
            '请输入此机械臂账号的密码。密码将由当前 Windows 账户安全保存，之后录制无需再次输入。',
            show='*', parent=self.app)
        if not password:
            return None
        try:
            store.save(*identity, password)
        except CredentialStoreError:
            messagebox.showerror('无法保存密码', 'Windows 凭据管理器不可用，未开始录制。请检查账户或稍后重试。', parent=self.app)
            return None
        return password

    def _set_password(self):
        identity = self._credential_identity()
        if identity is None:
            return
        try:
            store = WindowsCredentialStore()
            password = self._prompt_and_save_password(store, identity)
            if password is not None:
                self.status_text.set('密码已保存在当前 Windows 账户，后续录制无需再次输入。')
                del password
        except CredentialStoreError:
            messagebox.showerror('无法保存密码', 'Windows 凭据管理器不可用，请检查当前 Windows 账户。', parent=self.app)

    def _clear_password(self):
        identity = self._credential_identity()
        if identity is None:
            return
        try:
            WindowsCredentialStore().delete(*identity)
            self.status_text.set('当前机器人账号保存的密码已清除。')
        except CredentialStoreError:
            messagebox.showerror('无法清除密码', '无法访问 Windows 凭据管理器，请检查当前 Windows 账户。', parent=self.app)

    def _recording_password(self, identity):
        try:
            store = WindowsCredentialStore()
            password = store.load(*identity)
            return password if password is not None else self._prompt_and_save_password(store, identity)
        except CredentialStoreError:
            messagebox.showerror('无法读取密码', 'Windows 凭据管理器不可用，未开始录制。', parent=self.app)
            return None

    def mark_dirty(self):
        self._last_preview = 0.0

    @property
    def busy(self):
        return self.state != 'idle'

    def _set_controls(self):
        available = self.state == 'idle'
        self.start_button.configure(state='normal' if available else 'disabled')
        for widget in (self.success_button, self.failed_button, self.intervention_button):
            widget.configure(state='normal' if self.state == 'recording' else 'disabled')
        for widget in (self.task_entry, self.folder_entry, self.folder_button, self.ip_entry,
                       self.user_entry, self.python_entry, self.python_button,
                       self.password_button, self.clear_password_button):
            widget.configure(state='normal' if available else 'disabled')

    def _write_manifest(self):
        if self.episode is None or self.manifest is None:
            return
        temporary = self.episode / 'metadata.json.tmp'
        temporary.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.episode / 'metadata.json')

    def _event(self, name, **details):
        if self.episode is None:
            return
        item = {'event': name, **_clock(), **details}
        with (self.episode / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')

    def start(self):
        if self.busy:
            return
        library = getattr(self.app, 'pages', {}).get('library')
        if library is not None and getattr(library, 'exporting', False):
            messagebox.showinfo('数据正在处理', '请等待导出、删除或恢复完成后再开始录制，避免文件操作相互影响。',
                                parent=self.app)
            return
        task = self.task.get().strip()
        if not task or len(task) > 500:
            messagebox.showinfo('检查任务', '请填写不超过 500 字的本次短任务。', parent=self.app)
            return
        for key, title in (('astra', '外部相机'), ('wrist', '腕部相机')):
            info = self.app.pages[key].snapshot_info
            if info.get('state') != 'streaming' or time.time() - (info.get('last_frame_at') or 0) > 3:
                messagebox.showinfo('相机尚未准备好', f'请先连接并检查{title}实时画面。', parent=self.app)
                return
        python_text = self.python_path.get().strip()
        if not python_text:
            messagebox.showinfo('选择 Kortex Python', '请先点击“选择”，指定安装了 Kortex SDK 的 python.exe。', parent=self.app)
            return
        python = Path(python_text).expanduser()
        worker = Path(__file__).resolve().with_name('robot_recorder.py')
        if not python.is_file() or not worker.is_file():
            messagebox.showerror('录制组件不可用', '请检查 Kortex Python 路径和录制程序是否存在。', parent=self.app)
            return
        self._remember_python_path()
        identity = self._credential_identity()
        if identity is None:
            return
        password = self._recording_password(identity)
        if password is None:
            return
        self.robot_events = queue.Queue()
        self.robot_state = 'connecting'
        self.robot_counts = {}
        self.robot_eof = False
        self.camera_started = set()
        self.warnings = []
        self.pending_outcome = None
        self.camera_infos = {}
        try:
            if not self.folder.get().strip():
                raise ValueError('Recording directory is empty.')
            root = Path(self.folder.get().strip()).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            episode_name = f"episode_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
            self.episode = root / episode_name
            self.episode.mkdir(exist_ok=False)
            self.manifest = {
                'schema_version': 1, 'task': task, 'recording_status': 'starting',
                'user_outcome': None, 'training_ready': False,
                'quality_review': 'not_reviewed', 'created': _clock(),
                'camera_names': {'astra': 'top', 'wrist': 'wrist'},
                'controller_events_requested': bool(self.controller_events.get()),
                'state_action_contract': 'not_yet_validated',
                'clock_note': 'Host perf_counter_ns and wall clocks; camera receive time is not exposure time.',
            }
            self._write_manifest()
            self._event('episode_requested')
            cmd = [str(python), str(worker), '--output-dir', str(self.episode),
                   '--ip', self.robot_ip.get().strip(), '--username', self.robot_user.get().strip(),
                   '--frequency-hz', self.candidate_hz.get()]
            if self.controller_events.get():
                cmd.append('--controller-events')
            self.robot_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            threading.Thread(target=self._read_robot_output, args=(self.robot_process, self.robot_events),
                             daemon=True).start()
            self.robot_process.stdin.write(json.dumps({'type': 'start', 'password': password}, ensure_ascii=False) + '\n')
            self.robot_process.stdin.flush()
            self.state = 'connecting_robot'
            self.start_clock = time.monotonic()
            self.status_text.set('正在建立机器人只读会话，请暂时不要开始示范。')
            self.details_text.set('连接和录制准备完成前，请勿开始操作。')
            self.path_text.set('数据目录：' + str(self.episode))
            self._set_controls()
        except (OSError, ValueError, BrokenPipeError):
            if self.robot_process is not None and self.robot_process.poll() is None:
                self.robot_process.terminate()
            if self.manifest is not None:
                self.manifest.update(recording_status='incomplete', failure_reason='start_failed')
                try:
                    self._write_manifest()
                except OSError:
                    pass
            self.robot_process = None
            self.state = 'idle'
            self._set_controls()
            messagebox.showerror('无法开始', '未能创建记录或启动机器人只读程序，请检查路径和权限。', parent=self.app)
        finally:
            del password

    @staticmethod
    def _read_robot_output(process, events):
        try:
            for line in process.stdout:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    events.put(item)
        finally:
            events.put({'type': '_eof'})

    def _start_cameras(self):
        try:
            self._event('robot_ready')
            for key, name in (('astra', 'top'), ('wrist', 'wrist')):
                # Include a partially failed start in rollback/timeout handling.
                self.camera_started.add(key)
                self.app.streams[key].start_recording(self.episode, name)
            self.state = 'starting_cameras'
            self.status_text.set('正在准备两路相机录制，请暂时不要开始示范。')
        except (OSError, RuntimeError, ValueError, AttributeError):
            self._begin_stop('incomplete', 'camera_start_failed')

    def stop(self, outcome):
        if self.state == 'recording':
            self._begin_stop(outcome)

    def _begin_stop(self, outcome, reason=None):
        if self.state in ('idle', 'stopping'):
            return
        self.pending_outcome = outcome
        self.state = 'stopping'
        self.stop_clock = time.monotonic()
        self.manifest.update(recording_status='stopping', user_outcome=outcome)
        if reason:
            self.manifest['failure_reason'] = reason
        try:
            self._event('stop_requested', outcome=outcome, reason=reason)
            self._write_manifest()
        except OSError:
            self.warnings.append('metadata_write_failed')
        for key in self.camera_started:
            try:
                self.app.streams[key].stop_recording()
            except (OSError, RuntimeError, ValueError, AttributeError):
                self.warnings.append(key + '_stop_failed')
        if self.robot_process is not None and self.robot_process.poll() is None:
            try:
                self.robot_process.stdin.write('{"type":"stop"}\n')
                self.robot_process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError):
                self.warnings.append('robot_stop_signal_failed')
        self.status_text.set('正在结束并保存数据，请不要关闭窗口。')
        self._set_controls()

    def request_close(self, callback):
        if not self.busy:
            callback()
            return
        self.close_callback = callback
        self._begin_stop('incomplete', 'application_closed')

    def _finalize(self, forced=False):
        if self.state != 'stopping':
            return
        if forced and self.robot_process is not None and self.robot_process.poll() is None:
            self.robot_process.terminate()
        cameras = {key: dict(self.camera_infos.get(key, {}).get('recording') or {})
                   for key in ('astra', 'wrist')}
        complete = (not forced and self.pending_outcome != 'incomplete'
                    and self.robot_state == 'stopped'
                    and self.robot_counts.get('feedback_samples', 0) > 0
                    and not self.robot_counts.get('feedback_errors', 0)
                    and not self.robot_counts.get('missing_joint_samples', 0)
                    and not self.robot_counts.get('missing_gripper_samples', 0)
                    and all(cameras[k].get('state') == 'complete' for k in ('astra', 'wrist'))
                    and all(cameras[k].get('written', 0) > 0 for k in ('astra', 'wrist'))
                    and not any(w.endswith('_stop_failed') for w in self.warnings))
        # Completed means the raw streams closed, never that labels or
        # synchronization have been validated for training.
        if self.robot_counts.get('missing_joint_samples', 0):
            self.warnings.append('missing_joint_feedback')
        if self.robot_counts.get('missing_gripper_samples', 0):
            self.warnings.append('missing_gripper_feedback')
        if self.robot_counts.get('feedback_errors', 0):
            self.warnings.append('feedback_error')
        if self.robot_counts.get('joint_fault_samples', 0):
            self.warnings.append('joint_fault_detected')
        if self.robot_counts.get('joint_warning_samples', 0):
            self.warnings.append('joint_warning_detected')
        if self.robot_counts.get('joint_safety_unavailable_samples', 0):
            self.warnings.append('joint_safety_unavailable')
        if self.manifest.get('controller_events_requested') and (
                self.robot_counts.get('controller_subscription') != 'subscribed'
                or not self.robot_counts.get('controller_events', 0)):
            self.warnings.append('controller_input_unverified')
        if any(cameras[k].get('dropped', 0) or cameras[k].get('read_failures', 0)
               for k in ('astra', 'wrist')):
            self.warnings.append('camera_frames_incomplete')
        self.manifest.update(recording_status='complete' if complete else 'incomplete',
                             finished=_clock(), robot_state=self.robot_state,
                             robot_counts=self.robot_counts, camera_recording=cameras,
                             warnings=list(dict.fromkeys(self.warnings)), training_ready=False,
                             quality_review='not_reviewed',
                             action_labels='not_verified', synchronization='not_verified')
        try:
            self._event('recording_finalized', status=self.manifest['recording_status'])
            self._write_manifest()
        except OSError:
            complete = False
        self.status_text.set('已保存，正在自动整理；任务已保留，可以继续录下一条。'
                             if complete else '录制未完整结束，原始数据已保留，请在样本库检查。')
        details = ''
        if self.manifest.get('failure_reason') in ('robot_recorder_error', 'robot_connection_timeout'):
            details += ' 未能连接机器人，请检查地址、网络及密码；可点击“设置密码”更新。'
        if 'missing_gripper_feedback' in self.warnings:
            details += ' 未读到部分夹爪反馈。'
        if 'missing_joint_feedback' in self.warnings or 'feedback_error' in self.warnings:
            details += ' 机器人反馈存在缺失或错误。'
        if 'camera_frames_incomplete' in self.warnings:
            details += ' 相机记录存在掉帧或读帧错误。'
        details += self._joint_safety_message()
        self.details_text.set(details)
        # Repeated demonstrations share a task. The immutable episode manifest
        # already captured its text at start; changing the next task cannot edit it.
        self.state = 'idle'
        self.robot_process = None
        self._set_controls()
        # Notify on the Tk thread only after all raw workers have finished.
        # A close request preserves raw files and leaves preparation for next launch.
        on_saved = getattr(self.app, 'on_episode_saved', None)
        if on_saved is not None and not forced and self.close_callback is None:
            try:
                on_saved(self.episode)
            except (OSError, ValueError, RuntimeError):
                self.status_text.set('录制已保存，可在样本库中重新整理。')
        if self.close_callback is not None:
            callback, self.close_callback = self.close_callback, None
            callback()

    def _joint_safety_message(self):
        details = ''
        if 'joint_fault_detected' in self.warnings:
            details += ' 关节故障，请停止示范。'
        if 'joint_warning_detected' in self.warnings:
            details += ' 关节警告。'
        if 'joint_safety_unavailable' in self.warnings:
            details += ' 关节状态未知。'
        return details

    def update_recording(self, camera_infos, visible):
        self.camera_infos = camera_infos
        if visible and time.monotonic() - self._last_preview >= 0.15:
            self._draw_previews()
            self._last_preview = time.monotonic()
        while True:
            try:
                event = self.robot_events.get_nowait()
            except queue.Empty:
                break
            event_type = event.get('type')
            if event_type == 'status':
                self.robot_state = event.get('state', 'unknown')
                if isinstance(event.get('counts'), dict):
                    self.robot_counts = event['counts']
                if self.state == 'connecting_robot' and self.robot_state == 'recording':
                    self._start_cameras()
                elif self.robot_state == 'error' and self.state not in ('idle', 'stopping'):
                    self._begin_stop('incomplete', 'robot_recorder_error')
            elif event_type == 'warning':
                self.warnings.append(str(event.get('code', 'robot_warning')))
                self.details_text.set('录制异常，请检查数据。' + self._joint_safety_message())
            elif event_type == '_eof':
                self.robot_eof = True
                if self.robot_state != 'stopped' and self.state not in ('idle', 'stopping'):
                    self.robot_state = 'error'
                    self._begin_stop('incomplete', 'robot_recorder_disconnected')

        if self.state == 'connecting_robot' and time.monotonic() - self.start_clock > 25:
            self._begin_stop('incomplete', 'robot_connection_timeout')
        if self.state == 'starting_cameras':
            states = [(camera_infos.get(key, {}).get('recording') or {}).get('state', 'idle')
                      for key in ('astra', 'wrist')]
            if 'error' in states:
                self._begin_stop('incomplete', 'camera_recording_error')
            elif states == ['recording', 'recording']:
                self.state = 'recording'
                self.manifest.update(recording_status='recording', active=_clock())
                try:
                    self._event('recording_active')
                    self._write_manifest()
                except OSError:
                    self._begin_stop('incomplete', 'metadata_write_failed')
                if self.state == 'recording':
                    self.status_text.set('录制中：两路相机和机器人反馈均已启动，可以开始示范。')
                    self.details_text.set('完成后根据实物结果选择成功、失败或人工接管。'
                                          + self._joint_safety_message())
                    self._set_controls()
            elif time.monotonic() - self.start_clock > 35:
                self._begin_stop('incomplete', 'camera_start_timeout')
        elif self.state == 'recording':
            for key in ('astra', 'wrist'):
                recording_state = (camera_infos.get(key, {}).get('recording') or {}).get('state')
                if recording_state in ('error', 'complete', 'idle'):
                    self._begin_stop('incomplete', key + '_recording_interrupted')
                    break
        if self.state == 'stopping':
            camera_done = all(
                key not in self.camera_started or
                (camera_infos.get(key, {}).get('recording') or {}).get('state') in ('complete', 'error')
                for key in ('astra', 'wrist'))
            robot_done = self.robot_process is None or self.robot_eof
            if camera_done and robot_done:
                self._finalize()
            elif time.monotonic() - self.stop_clock > 15:
                self.warnings.append('stop_timeout')
                self._finalize(forced=True)

    def _draw_previews(self):
        for key, canvas in self.previews.items():
            frame = self.app.pages[key].last_frame
            width, height = canvas.winfo_width(), canvas.winfo_height()
            if width < 10 or height < 10:
                continue
            canvas.delete('all')
            info = self.camera_infos.get(key, {})
            fresh = info.get('state') == 'streaming' and time.time() - (info.get('last_frame_at') or 0) <= 3
            if frame is not None and fresh:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb)
                scale = min((width - 2) / image.width, (height - 2) / image.height)
                image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                                     Image.Resampling.BILINEAR)
                self._photos[key] = ImageTk.PhotoImage(image)
                canvas.create_image(width // 2, height // 2, image=self._photos[key], anchor='center')
            else:
                canvas.create_text(width // 2, height // 2, text='等待实时画面', fill=MUTED,
                                   font=(FONT, 11))
