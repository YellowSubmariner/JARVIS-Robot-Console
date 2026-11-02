"""Independent USB/RTSP camera capture processes for the desktop viewer.

No robot control SDK or robot control connection is used here. Frames returned
by ``CameraStream.poll`` are OpenCV BGR arrays; ``None`` means no new frame.
The UI may call poll every 30–50 ms. stop() never waits for a camera driver.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


class _CameraProblem(Exception):
    """A short, user-facing message which contains no stream credentials."""


def _camera_infos():
    try:
        import cv2
        from cv2_enumerate_cameras import enumerate_cameras
    except ImportError as exc:
        raise _CameraProblem("Camera discovery component is missing. Reinstall the app.") from exc
    try:
        return list(enumerate_cameras(cv2.CAP_DSHOW))
    except Exception as exc:
        raise _CameraProblem("Cannot list cameras. Check the device connection.") from exc


def enumerate_usb_cameras() -> list[str]:
    """Return DirectShow device names without opening any camera.

    A missing enumerator raises RuntimeError. It never guesses an index or
    substitutes the laptop's integrated camera.
    """
    try:
        return list(dict.fromkeys(info.name for info in _camera_infos()))
    except _CameraProblem as exc:
        raise RuntimeError(str(exc)) from exc


def _validate_config(config: dict) -> str | None:
    if config.get("kind") == "usb":
        if not str(config.get("name", "")).strip():
            return "Select an external camera first."
    elif config.get("kind") == "rtsp":
        try:
            address = urlsplit(str(config.get("url", "")).strip())
            if address.scheme not in ("rtsp", "rtsps") or not address.hostname:
                return "Enter a valid RTSP camera URL."
            if address.port is not None and not 1 <= address.port <= 65535:
                return "The camera port must be between 1 and 65535."
        except ValueError:
            return "Enter a valid RTSP camera URL."
    else:
        return "Unsupported camera type."
    return None


def _put_latest(target_queue, value) -> None:
    """Drop an old item instead of letting video or status queues grow."""
    try:
        target_queue.put_nowait(value)
        return
    except queue.Full:
        pass
    try:
        target_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        target_queue.put_nowait(value)
    except queue.Full:
        pass


def _capture_worker(config: dict, stop_event, frame_queue, status_queue) -> None:
    # FFmpeg options are process-local: a slow/unavailable wrist camera cannot
    # block the independent USB process or the Tk event loop.
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    os.environ["OPENCV_FFMPEG_DEBUG"] = "0"
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    retry_seconds = max(2.0, float(config.get("retry_seconds", 5.0)))
    maximum_fps = max(1.0, min(60.0, float(config.get("max_fps", 24.0))))

    def status(state: str, message: str) -> None:
        _put_latest(status_queue, {"state": state, "message": message, "at": time.time()})

    try:
        import cv2
    except Exception:
        status("error", "Cannot load the video component. Reinstall the app.")
        return
    set_log_level = getattr(cv2, "setLogLevel", None)
    if callable(set_log_level):
        set_log_level(0)

    frame_count = 0
    first_attempt = True
    capture = None
    try:
        while not stop_event.is_set():
            status("connecting" if first_attempt else "reconnecting", "Connecting to camera…")
            first_attempt = False
            try:
                if config["kind"] == "usb":
                    matches = [info for info in _camera_infos() if info.name == config["name"]]
                    if not matches:
                        raise _CameraProblem("Selected camera not found. Check the USB connection.")
                    if len(matches) != 1:
                        raise _CameraProblem("Multiple cameras have this name. Disconnect the extra devices.")
                    info = matches[0]
                    capture = cv2.VideoCapture(info.index, info.backend)
                    if capture.isOpened():
                        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(config.get("width", 640)))
                        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(config.get("height", 480)))
                        capture.set(cv2.CAP_PROP_FPS, 30)
                else:
                    url = str(config["url"]).strip()
                    address = urlsplit(url)
                    try:
                        with socket.create_connection((address.hostname, address.port or 554), timeout=2.0):
                            pass
                    except socket.gaierror as exc:
                        raise _CameraProblem("Cannot resolve the camera address. Check the URL.") from exc
                    except OSError as exc:
                        raise _CameraProblem("Camera video service is unavailable. Check the camera status.") from exc
                    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000,
                        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000,
                    ])
                if stop_event.is_set():
                    break
                if capture is None or not capture.isOpened():
                    raise _CameraProblem("Cannot open the video stream. Check the connection or URL.")

                recent_times: deque[float] = deque(maxlen=30)
                next_send = 0.0
                sent_streaming_status = False
                missed_frames = 0
                while not stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        missed_frames += 1
                        if config["kind"] == "rtsp" or missed_frames >= 6:
                            raise _CameraProblem("Video was interrupted. Check the camera connection.")
                        if stop_event.wait(0.04):
                            break
                        continue
                    missed_frames = 0
                    now = time.monotonic()
                    recent_times.append(now)
                    if now < next_send:
                        continue
                    next_send = (now + 1.0 / maximum_fps if next_send == 0.0
                                 else max(next_send + 1.0 / maximum_fps, now))
                    encoded_ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if not encoded_ok:
                        continue
                    frame_count += 1
                    if not sent_streaming_status:
                        status("streaming", "Live video")
                        sent_streaming_status = True
                    fps = ((len(recent_times) - 1) / (recent_times[-1] - recent_times[0])
                           if len(recent_times) > 1 and recent_times[-1] > recent_times[0] else 0.0)
                    _put_latest(frame_queue, {
                        # Small encoded packets avoid large pipe reads holding
                        # up Tk while a raw NumPy frame is still being written.
                        "jpeg": encoded.tobytes(),
                        "last_frame_at": time.time(),
                        "fps": fps,
                        "width": int(frame.shape[1]),
                        "height": int(frame.shape[0]),
                        "frames": frame_count,
                    })
            except _CameraProblem as exc:
                status("reconnecting", f"{exc} Retrying in {retry_seconds:g} seconds.")
            except Exception:
                # In particular, never put an exception containing an RTSP URL
                # (possibly with credentials) into application status text.
                status("reconnecting", f"Camera connection failed. Retrying in {retry_seconds:g} seconds.")
            finally:
                if capture is not None:
                    capture.release()
                    capture = None
            if stop_event.wait(retry_seconds):
                break
    finally:
        if capture is not None:
            capture.release()
        # A large unread frame must not keep a child alive during shutdown.
        frame_queue.cancel_join_thread()


@dataclass
class _ProcessHandle:
    process: Any
    stop_event: Any
    frame_queue: Any
    status_queue: Any


def _shutdown_handle(handle: _ProcessHandle) -> None:
    """Run in a background thread; bound driver shutdown even if read() hangs."""
    try:
        handle.stop_event.set()
        if handle.process.pid is not None:
            handle.process.join(0.35)
            if handle.process.is_alive():
                handle.process.terminate()
                handle.process.join(0.35)
            if handle.process.is_alive():
                handle.process.kill()
                handle.process.join(0.35)
    finally:
        for target in (handle.frame_queue, handle.status_queue):
            try:
                target.cancel_join_thread()
                target.close()
            except (OSError, ValueError):
                pass
        try:
            if not handle.process.is_alive():
                handle.process.close()
        except (OSError, ValueError):
            pass


class CameraStream:
    """One camera process with nonblocking GUI-facing start/stop/poll methods."""

    def __init__(self, config: dict):
        self.config = dict(config)
        self._context = mp.get_context("spawn")
        self._handle: _ProcessHandle | None = None
        self._reapers: list[threading.Thread] = []
        self._closed = False
        self._values: dict[str, Any] = {}
        self._state_changed_at = 0.0
        self._set_stopped()

    def _set_stopped(self) -> None:
        self._values = {
            "state": "stopped", "message": "Camera disconnected", "frame": None,
            "fps": 0.0, "width": 0, "height": 0,
            "last_frame_at": None, "frames": 0,
        }
        self._state_changed_at = time.time()

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Camera service has been closed.")
        if self._handle is not None:
            return
        error = _validate_config(self.config)
        if error:
            self._values.update(state="error", message=error)
            return
        self._set_stopped()
        self._values.update(state="connecting", message="Connecting to camera…")
        frame_queue = self._context.Queue(maxsize=2)
        status_queue = self._context.Queue(maxsize=8)
        stop_event = self._context.Event()
        process = self._context.Process(
            target=_capture_worker,
            args=(dict(self.config), stop_event, frame_queue, status_queue),
            daemon=True,
            name=f"jarvis-camera-{self.config['kind']}",
        )
        handle = _ProcessHandle(process, stop_event, frame_queue, status_queue)
        try:
            process.start()
        except Exception:
            self._values.update(state="error", message="Cannot start camera capture. Try reconnecting.")
            _shutdown_handle(handle)
            return
        self._handle = handle

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        self._set_stopped()
        self._reapers = [thread for thread in self._reapers if thread.is_alive()]
        if handle is not None:
            handle.stop_event.set()
            thread = threading.Thread(target=_shutdown_handle, args=(handle,), daemon=True)
            self._reapers.append(thread)
            thread.start()

    def restart(self, config: dict | None = None) -> None:
        self.stop()
        if config is not None:
            self.config = dict(config)
        self.start()

    def poll(self) -> dict:
        """Drain queues and return current status plus at most one new BGR frame."""
        self._values["frame"] = None
        handle = self._handle
        if handle is None:
            return dict(self._values)
        broken_pipe = False
        while True:
            try:
                status = handle.status_queue.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError, ValueError):
                broken_pipe = True
                break
            self._values.update(state=status["state"], message=status["message"])
            self._state_changed_at = status["at"]
        latest = None
        while True:
            try:
                latest = handle.frame_queue.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError, ValueError):
                broken_pipe = True
                break
        if broken_pipe:
            self.stop()
            self._values.update(state="error", message="Video transfer was interrupted. Try reconnecting.")
            return dict(self._values)
        if latest is not None and (self._values["state"] == "streaming"
                                   or latest["last_frame_at"] >= self._state_changed_at):
            try:
                import cv2
                import numpy as np
                image = cv2.imdecode(np.frombuffer(latest.pop("jpeg"), dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    self._values.update(latest, frame=image, state="streaming", message="Live video")
            except Exception:
                # A malformed/partially delivered image is dropped; it must
                # never escape into the GUI's scheduled polling callback.
                pass
        if not handle.process.is_alive() and self._values["state"] != "error":
            self._values.update(state="error", message="The capture process stopped. Try reconnecting.")
        return dict(self._values)

    def close(self) -> None:
        self.stop()
        self._closed = True
        deadline = time.monotonic() + 1.9
        for thread in self._reapers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        self._reapers = [thread for thread in self._reapers if thread.is_alive()]
