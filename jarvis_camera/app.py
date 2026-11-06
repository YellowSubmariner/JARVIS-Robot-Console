"""Native desktop interface. Camera capture lives entirely outside the Tk thread."""

import ctypes
import logging
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
from PIL import Image, ImageEnhance, ImageTk

from .capture import CameraStream, enumerate_usb_cameras
from .recording_page import RecordingPage
from .settings import Settings, app_directory, validate_rtsp_url

BG = '#0a101b'
SIDE = '#101a29'
CARD = '#111e30'
BORDER = '#24354d'
TEXT = '#e7eef8'
MUTED = '#91a3bc'
CYAN = '#5ddac6'
GREEN = '#7cddb0'
AMBER = '#f4c779'
FONT = 'Segoe UI'

PAGE_DEFINITIONS = (
    {'key': 'wrist', 'title': 'Wrist Camera', 'subtitle': 'See the workspace from the gripper perspective.', 'tag': 'KINOVA GEN3', 'kind': 'rtsp'},
    {'key': 'astra', 'title': 'Astra Global Camera', 'subtitle': 'View the full worktable and its layout.', 'tag': 'EXTERNAL CAMERA', 'kind': 'usb'},
)


def label(parent, text='', size=10, color=TEXT, bg=BG, bold=False, **kwargs):
    return tk.Label(parent, text=text, fg=color, bg=bg,
                    font=(FONT, size, 'bold' if bold else 'normal'), **kwargs)


def button(parent, text, command, primary=False, **kwargs):
    return tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                     bg=CYAN if primary else '#21334d', fg='#08231f' if primary else TEXT,
                     activebackground='#88e8d9' if primary else '#2a4263',
                     activeforeground='#08231f' if primary else TEXT,
                     disabledforeground='#63748b', font=(FONT, 10, 'bold'),
                     cursor='hand2', padx=16, pady=9, **kwargs)


class CameraPage(tk.Frame):
    def __init__(self, parent, app, definition):
        super().__init__(parent, bg=BG)
        self.app = app
        self.definition = definition
        self.key = definition['key']
        self.last_frame = None
        self.photo = None
        self.last_state = 'stopped'
        self.snapshot_info = {}
        self._dirty = True
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 20))
        label(header, definition['tag'], 9, CYAN, bold=True).pack(anchor='w')
        label(header, definition['title'], 23, bold=True).pack(anchor='w', pady=(8, 4))
        label(header, definition['subtitle'], 10, MUTED).pack(anchor='w')

        source_card = tk.Frame(self, bg=CARD, padx=16, pady=12,
                               highlightbackground=BORDER, highlightthickness=1)
        source_card.grid(row=1, column=0, sticky='ew', pady=(0, 14))
        source_card.columnconfigure(1, weight=1)
        label(source_card, 'Stream URL' if self.key == 'wrist' else 'Camera device',
              10, MUTED, bg=CARD).grid(row=0, column=0, padx=(0, 14))
        self.source_value = tk.StringVar(value=app.settings.get('wrist_url' if self.key == 'wrist' else 'astra_name'))
        if self.key == 'wrist':
            self.source_input = tk.Entry(source_card, textvariable=self.source_value, bg='#0b1524', fg=TEXT,
                insertbackground=CYAN, relief='flat', font=('Consolas', 11), bd=8)
            self.source_input.bind('<Return>', lambda _event: self.app.connect(self.key))
        else:
            self.source_input = ttk.Combobox(source_card, textvariable=self.source_value,
                                            state='readonly', values=[self.source_value.get()], style='Source.TCombobox')
        self.source_input.grid(row=0, column=1, sticky='ew')
        self.source_input.bind('<Control-Key-1>', lambda _event: (app.show_page('wrist'), 'break')[1])
        self.source_input.bind('<Control-Key-2>', lambda _event: (app.show_page('astra'), 'break')[1])
        button(source_card, 'Connect', lambda: app.connect(self.key), primary=True).grid(row=0, column=2, padx=(14, 0))
        if self.key == 'astra':
            self.refresh_button = button(source_card, 'Refresh devices', app.discover_cameras)
            self.refresh_button.grid(row=0, column=3, padx=(8, 0))

        toolbar = tk.Frame(self, bg=BG)
        toolbar.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        self.status_label = label(toolbar, '●  Not connected', 10, MUTED, bold=True)
        self.status_label.pack(side='left')
        self.capture_button = button(toolbar, 'Save snapshot', self.save_snapshot)
        self.capture_button.pack(side='right')
        self.capture_button.configure(state='disabled')
        button(toolbar, 'Reconnect', lambda: app.connect(self.key)).pack(side='right', padx=8)
        button(toolbar, 'Disconnect', lambda: app.disconnect(self.key)).pack(side='right')

        self.canvas = tk.Canvas(self, bg='#050a12', bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.canvas.grid(row=3, column=0, sticky='nsew')
        self.canvas.bind('<Configure>', lambda _event: self.mark_dirty())

        footer = tk.Frame(self, bg=BG)
        footer.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        self.stats = label(footer, 'Waiting for video   ·   — × —   ·   — FPS', 9, MUTED)
        self.stats.pack(side='left')
        self.timestamp = label(footer, 'Last frame: —', 9, MUTED)
        self.timestamp.pack(side='right')
        self.notice = label(self, 'Connection status and video update automatically.', 9, MUTED, anchor='w', justify='left', wraplength=950)
        self.notice.grid(row=5, column=0, sticky='ew', pady=(8, 0))
        self.bind('<Configure>', lambda event: self.notice.configure(wraplength=max(320, event.width - 4)))

    def mark_dirty(self):
        self._dirty = True

    def update_stream(self, info, visible):
        self.snapshot_info = info
        state = info.get('state', 'stopped')
        if state != self.last_state:
            self._dirty = True
            self.last_state = state
        if info.get('frame') is not None:
            self.last_frame = info['frame']
            self._dirty = True
        last_time = info.get('last_frame_at') or 0
        stale = state == 'streaming' and (not last_time or time.time() - last_time > 3)
        display_state = 'reconnecting' if stale else state
        title = {'streaming': 'Live video', 'connecting': 'Connecting', 'reconnecting': 'Waiting to reconnect',
                 'error': 'Not connected', 'stopped': 'Disconnected'}.get(display_state, 'Not connected')
        color = GREEN if display_state == 'streaming' else AMBER if display_state in ('connecting', 'reconnecting', 'error') else MUTED
        self.status_label.configure(text=f'●  {title}', fg=color)
        width, height = info.get('width', 0), info.get('height', 0)
        self.stats.configure(text=f"{'Live' if display_state == 'streaming' else 'Waiting for video'}   ·   {width or '—'} × {height or '—'}   ·   {info.get('fps', 0):.1f} FPS")
        self.timestamp.configure(text='Last frame: ' + (datetime.fromtimestamp(last_time).strftime('%H:%M:%S') if last_time else '—'))
        self.capture_button.configure(state='normal' if display_state == 'streaming' and self.last_frame is not None else 'disabled')
        message = info.get('message') or ''
        if self.key == 'wrist' and display_state in ('error', 'reconnecting'):
            message = 'No wrist video. Check that the vision module is online in Kortex.'
        elif self.key == 'astra' and display_state in ('error', 'reconnecting'):
            message = 'Check the USB connection or close other apps using the camera.'
        elif display_state == 'streaming':
            message = 'Receiving live video. Switching pages keeps both camera connections active.'
        elif display_state == 'stopped':
            message = 'Video is disconnected. Click Connect to resume.'
        if display_state == 'reconnecting':
            message += ' Retrying automatically.'
        elif display_state == 'error':
            message += ' Click Reconnect to try again.'
        self.notice.configure(text=message)
        if stale:
            self._dirty = True
        if visible and self._dirty:
            self.render(display_state)

    def render(self, state=None):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 10 or height < 10:
            return
        state = state or self.last_state
        self.canvas.delete('all')
        if self.last_frame is not None:
            rgb = cv2.cvtColor(self.last_frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            ratio = min((width - 2) / image.width, (height - 2) / image.height)
            image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))), Image.Resampling.BILINEAR)
            if state != 'streaming':
                image = ImageEnhance.Brightness(image).enhance(0.28)
            self.photo = ImageTk.PhotoImage(image)
            self.canvas.create_image(width // 2, height // 2, image=self.photo, anchor='center')
        if state != 'streaming':
            cx, cy = width // 2, height // 2 - 25
            self.canvas.create_rectangle(cx - 28, cy - 22, cx + 28, cy + 18, outline='#42607e', width=2)
            self.canvas.create_oval(cx - 10, cy - 12, cx + 10, cy + 8, outline=CYAN, width=2)
            self.canvas.create_line(cx - 13, cy - 23, cx - 7, cy - 30, cx + 9, cy - 30, cx + 15, cy - 23, fill='#42607e', width=2)
            headline = {'connecting': 'Connecting to camera', 'reconnecting': 'Waiting for video', 'error': 'Camera unavailable', 'stopped': 'Camera disconnected'}.get(state, 'Waiting for camera video')
            detail = ('Live video will appear when the connection recovers.' if state in ('connecting', 'reconnecting')
                      else 'Check the connection, then click Reconnect.' if state == 'error'
                      else 'Click Connect above to start viewing.')
            self.canvas.create_text(cx, cy + 55, text=headline, fill=TEXT, font=(FONT, 15, 'bold'))
            self.canvas.create_text(cx, cy + 86, text=detail, fill=MUTED, font=(FONT, 10))
            if self.last_frame is not None:
                self.canvas.create_text(18, height - 20, text='Last received frame shown. Video is not updating.', fill=AMBER, anchor='w', font=(FONT, 9))
        else:
            self.canvas.create_rectangle(16, 16, 86, 42, fill='#10382d', outline='')
            self.canvas.create_text(51, 29, text='● LIVE', fill=GREEN, font=('Segoe UI', 10, 'bold'))
        self._dirty = False

    def save_snapshot(self):
        info = self.snapshot_info
        if self.last_frame is None or info.get('state') != 'streaming' or time.time() - (info.get('last_frame_at') or 0) > 3:
            return
        try:
            folder = app_directory() / 'screenshots'
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{self.key}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
            image = Image.fromarray(cv2.cvtColor(self.last_frame, cv2.COLOR_BGR2RGB))
            image.save(folder / name, quality=94)
            self.app.toast(f'Snapshot saved: screenshots / {name}')
        except OSError as exc:
            logging.exception('snapshot failed')
            messagebox.showerror('Snapshot not saved', f'Could not save the snapshot: {exc}', parent=self.app)


class CameraConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('JARVIS Robot Console')
        try:
            self.iconbitmap(str(Path(__file__).resolve().parent.parent / 'assets' / 'jarvis.ico'))
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.geometry('1320x860')
        self.minsize(1160, 740)
        self.settings = Settings()
        logging.basicConfig(filename=str(self.settings.directory / 'app.log'), level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
        self._closing = False
        self._close_pending = False
        self.active_key = self.settings.get('active_page')
        if self.active_key not in ('wrist', 'astra'):
            self.active_key = 'astra'
        self.streams = {
            'wrist': CameraStream({'kind': 'rtsp', 'url': self.settings.get('wrist_url')}),
            'astra': CameraStream({'kind': 'usb', 'name': self.settings.get('astra_name')}),
        }
        self.discovery_results = queue.Queue()
        self.discovering = False
        self._init_style()
        self._build_shell()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.bind('<Control-Key-1>', lambda _e: self.show_page('wrist'))
        self.bind('<Control-Key-2>', lambda _e: self.show_page('astra'))
        self.show_page(self.active_key, persist=False)
        self.after(200, self.start_streams)
        self.after(400, self.discover_cameras)
        self.after(40, self.tick)

    def _init_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Source.TCombobox', fieldbackground='#0b1524', background='#243b56',
                        foreground=TEXT, arrowcolor=TEXT, borderwidth=0, padding=8, font=(FONT, 10))
        style.map('Source.TCombobox', fieldbackground=[('readonly', '#0b1524')],
                  foreground=[('readonly', TEXT)], selectbackground=[('readonly', '#0b1524')],
                  selectforeground=[('readonly', TEXT)])
        self.option_add('*TCombobox*Listbox.background', CARD)
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', '#284966')
        self.option_add('*TCombobox*Listbox.font', (FONT, 10))

    def _build_shell(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        side = tk.Frame(self, bg=SIDE, width=230)
        side.grid(row=0, column=0, sticky='nsew')
        side.grid_propagate(False)
        brand = tk.Frame(side, bg=SIDE)
        brand.pack(fill='x', padx=25, pady=(33, 0))
        label(brand, 'J', 24, '#08231f', bg=CYAN, bold=True, width=2).pack(side='left')
        wordmark = tk.Frame(brand, bg=SIDE)
        wordmark.pack(side='left', padx=12)
        label(wordmark, 'JARVIS', 15, bg=SIDE, bold=True).pack(anchor='w')
        label(wordmark, 'CAMERA CONSOLE', 7, MUTED, bg=SIDE).pack(anchor='w', pady=(3, 0))
        label(side, 'CAMERA VIEWS', 9, MUTED, bg=SIDE).pack(anchor='w', padx=25, pady=(45, 12))
        self.nav_buttons = {}
        self.nav_status = {}
        for definition in PAGE_DEFINITIONS:
            key = definition['key']
            frame = tk.Frame(side, bg=SIDE)
            frame.pack(fill='x', padx=14, pady=5)
            nav = tk.Button(frame, text=definition['title'], anchor='w', relief='flat', bd=0,
                            bg=SIDE, fg=MUTED, activebackground='#1a3046', activeforeground=TEXT,
                            font=(FONT, 11, 'bold'), padx=17, pady=14, cursor='hand2',
                            command=lambda k=key: self.show_page(k))
            nav.pack(fill='x')
            self.nav_buttons[key] = nav
            status = label(frame, '○  Waiting to connect', 8, MUTED, bg=SIDE, anchor='w')
            status.pack(fill='x', padx=17, pady=(4, 7))
            self.nav_status[key] = status
        record_frame = tk.Frame(side, bg=SIDE)
        record_frame.pack(fill='x', padx=14, pady=5)
        record_nav = tk.Button(record_frame, text='示范录制', anchor='w', relief='flat', bd=0,
                               bg=SIDE, fg=MUTED, activebackground='#1a3046', activeforeground=TEXT,
                               font=(FONT, 11, 'bold'), padx=17, pady=14, cursor='hand2',
                               command=lambda: self.show_page('record'))
        record_nav.pack(fill='x')
        self.nav_buttons['record'] = record_nav
        bottom = tk.Frame(side, bg=SIDE)
        bottom.pack(side='bottom', fill='x', padx=25, pady=28)
        label(bottom, 'Local camera workspace', 9, bg=SIDE).pack(anchor='w')
        label(bottom, 'JARVIS  /  v0.1.0', 8, MUTED, bg=SIDE).pack(anchor='w', pady=(6, 0))

        main = tk.Frame(self, bg=BG, padx=30, pady=25)
        main.grid(row=0, column=1, sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        top = tk.Frame(main, bg=BG)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 22))
        label(top, 'WORKSPACE  /  CAMERA MONITORING', 9, MUTED).pack(side='left')
        self.clock = label(top, '', 9, MUTED)
        self.clock.pack(side='right')
        container = tk.Frame(main, bg=BG)
        container.grid(row=1, column=0, sticky='nsew')
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.pages = {}
        for definition in PAGE_DEFINITIONS:
            page = CameraPage(container, self, definition)
            page.grid(row=0, column=0, sticky='nsew')
            self.pages[definition['key']] = page
        self.pages['record'] = RecordingPage(container, self)
        self.pages['record'].grid(row=0, column=0, sticky='nsew')
        self.toast_label = label(main, 'Independent camera connections. Switch views with Ctrl+1 / Ctrl+2.', 8, MUTED, anchor='w')
        self.toast_label.grid(row=2, column=0, sticky='ew', pady=(20, 0))

    def show_page(self, key, persist=True):
        self.active_key = key
        self.pages[key].tkraise()
        self.pages[key].mark_dirty()
        for name, nav in self.nav_buttons.items():
            nav.configure(bg='#20374c' if name == key else SIDE, fg=TEXT if name == key else MUTED)
        if persist and key in ('wrist', 'astra'):
            try:
                self.settings.save(active_page=key)
            except OSError:
                self.toast('View changed, but preferences could not be saved.')

    def start_streams(self):
        for stream in self.streams.values():
            stream.start()

    def connect(self, key):
        if self.pages['record'].busy:
            self.toast('录制期间不能重连或更换相机，请先结束录制。')
            return
        value = self.pages[key].source_value.get().strip()
        if key == 'wrist' and not validate_rtsp_url(value):
            messagebox.showinfo('Check the stream URL', 'Enter a valid RTSP URL, for example:\nrtsp://192.168.1.10/color', parent=self)
            return
        if not value:
            self.toast('Select a camera device first.')
            return
        config = {'kind': 'rtsp', 'url': value} if key == 'wrist' else {'kind': 'usb', 'name': value}
        try:
            self.settings.save(**{'wrist_url' if key == 'wrist' else 'astra_name': value})
        except OSError:
            self.toast('Connecting, but preferences could not be saved.')
        self.streams[key].restart(config)
        self.pages[key].last_frame = None
        self.pages[key].mark_dirty()

    def disconnect(self, key):
        if self.pages['record'].busy:
            self.toast('录制期间不能断开相机，请先结束录制。')
            return
        self.streams[key].stop()

    def discover_cameras(self):
        if self.discovering or self._closing:
            return
        self.discovering = True
        self.pages['astra'].refresh_button.configure(state='disabled', text='Scanning…')
        def task():
            try:
                self.discovery_results.put((True, enumerate_usb_cameras()))
            except Exception:
                logging.exception('camera discovery failed')
                self.discovery_results.put((False, []))
        threading.Thread(target=task, daemon=True).start()

    def toast(self, text):
        self.toast_label.configure(text=text, fg=CYAN)

    def tick(self):
        if self._closing:
            return
        self.clock.configure(text=datetime.now().strftime('%Y.%m.%d   %H:%M:%S'))
        try:
            ok, names = self.discovery_results.get_nowait()
            self.discovering = False
            self.pages['astra'].refresh_button.configure(state='normal', text='Refresh devices')
            current = self.pages['astra'].source_value.get()
            choices = list(dict.fromkeys([current] + names))
            self.pages['astra'].source_input.configure(values=choices)
            if not ok:
                self.toast('Device discovery is unavailable. Existing connections remain active.')
        except queue.Empty:
            pass
        camera_infos = {}
        for key, stream in self.streams.items():
            info = stream.poll()
            camera_infos[key] = info
            self.pages[key].update_stream(info, key == self.active_key)
            state = info.get('state', 'stopped')
            status = '●  Connected' if state == 'streaming' else '○  Disconnected' if state == 'stopped' else '○  Connecting' if state == 'connecting' else '○  Waiting to connect'
            self.nav_status[key].configure(text=status, fg=GREEN if state == 'streaming' else MUTED)
        self.pages['record'].update_recording(camera_infos, self.active_key == 'record')
        if not self._closing:
            self.after(40, self.tick)

    def on_close(self):
        if self._closing or self._close_pending:
            return
        self._close_pending = True
        self.pages['record'].request_close(self._finish_close)

    def _finish_close(self):
        if self._closing:
            return
        self._closing = True
        self.withdraw()
        for stream in self.streams.values():
            stream.stop()
        for stream in self.streams.values():
            stream.close()
        self.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    app = CameraConsole()
    app.mainloop()
