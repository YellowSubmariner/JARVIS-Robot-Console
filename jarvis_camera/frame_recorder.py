"""Loss-visible per-camera recording from an already-open capture process.

Host timestamps denote when OpenCV returned a frame to this computer; they are
not camera exposure times. The timestamped JPEG sequence is raw source data,
not an aligned or training-ready dataset.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class FrameRecorder:
    """Write the captured stream on a worker thread with a bounded queue.

    Failed writes and overloads invalidate the recording instead of silently
    dropping a frame. A manifest is written before recording starts so a crash
    or forced shutdown leaves an explicit incomplete result on disk.
    """

    def __init__(self, episode_dir, name: str, on_status: Callable[[dict], None],
                 *, queue_size: int = 32, encoder=None):
        if name not in ("top", "wrist"):
            raise ValueError("Camera name must be 'top' or 'wrist'.")
        self.root = Path(episode_dir)
        if not self.root.is_dir():
            raise ValueError("Episode directory does not exist.")
        self.name = name
        self.on_status = on_status
        self.frames_dir = self.root / f"{name}_frames"
        self.frames_path = self.root / f"{name}_frames.jsonl"
        self.events_path = self.root / f"{name}_events.jsonl"
        self.manifest_path = self.root / f"{name}_recording.json"
        if any(path.exists() for path in (self.frames_dir, self.frames_path,
                                           self.events_path, self.manifest_path)):
            raise FileExistsError("Camera recording already exists in this episode.")
        self.queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self.events = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._last_monotonic_ns = None
        self._last_wall_time_utc = None
        self._frame_shape = None
        self.received = 0
        self.written = 0
        self.dropped = 0
        self.read_failures = 0
        self._encoder = encoder
        self._started_utc = _utc_now()
        self._started_monotonic_ns = time.perf_counter_ns()
        self._thread = threading.Thread(target=self._run, name=f"record-{name}", daemon=True)
        self.frames_dir.mkdir()
        self._write_manifest("recording")
        self._thread.start()

    def _status(self, state: str, message: str) -> None:
        self.on_status({"state": state, "message": message,
                        "received": self.received, "written": self.written,
                        "dropped": self.dropped, "read_failures": self.read_failures})

    def _write_manifest(self, state: str) -> None:
        data = {"camera": self.name, "state": state, "complete": state == "complete",
                "clock": "python_perf_counter_ns",
                "clock_implementation": time.get_clock_info("perf_counter").implementation,
                "clock_resolution_seconds": time.get_clock_info("perf_counter").resolution,
                "timestamp_meaning": "host_return_from_capture_read_not_exposure_time",
                "source_exposure_time": None, "source_clock_available": False,
                "source_sequence_available": False, "source_dropped_frames_known": False,
                "encoding": {"format": "JPEG", "quality": 95, "lossy": True,
                             "capture_array_color_order": "BGR", "frame_shape_hwc": self._frame_shape},
                "start_monotonic_ns": self._started_monotonic_ns,
                "start_wall_time_utc": self._started_utc,
                "received": self.received, "written": self.written,
                "dropped": self.dropped, "read_failures": self.read_failures,
                "error": self._error}
        if state != "recording":
            data.update(end_monotonic_ns=time.perf_counter_ns(), end_wall_time_utc=_utc_now())
        temp = self.manifest_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.manifest_path)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    @property
    def accepting(self) -> bool:
        return not self.stop_event.is_set() and self.running

    def submit(self, frame, monotonic_ns: int, wall_time_utc: str) -> bool:
        if not self.accepting:
            return False
        self.received += 1
        sequence = self.received - 1
        shape = tuple(frame.shape)
        if len(shape) != 3 or shape[2] != 3:
            self.dropped += 1
            self.events.put({"event": "unsupported_frame_shape", "sequence": sequence,
                             "monotonic_ns": monotonic_ns, "wall_time_utc": wall_time_utc,
                             "shape": shape})
            self.abort("Unexpected camera frame format; recording is incomplete.")
            return False
        if self._frame_shape is not None and shape != self._frame_shape:
            self.dropped += 1
            self.events.put({"event": "frame_shape_changed", "sequence": sequence,
                             "monotonic_ns": monotonic_ns, "wall_time_utc": wall_time_utc,
                             "shape": shape})
            self.abort("Camera resolution changed; recording is incomplete.")
            return False
        self._frame_shape = shape
        gap_ns = None
        if self._last_monotonic_ns is not None:
            gap_ns = monotonic_ns - self._last_monotonic_ns
            wall_delta_ns = int((datetime.fromisoformat(wall_time_utc) -
                                 datetime.fromisoformat(self._last_wall_time_utc)).total_seconds() * 1e9)
            if gap_ns <= 0 or abs(wall_delta_ns - gap_ns) > 2_000_000_000 or gap_ns > 1_000_000_000:
                self.dropped += 1
                self.events.put({"event": "clock_or_capture_gap", "monotonic_ns": monotonic_ns,
                                 "wall_time_utc": wall_time_utc, "host_gap_ns": gap_ns,
                                 "wall_gap_ns": wall_delta_ns})
                self.abort("Camera timing discontinuity or capture gap; recording is incomplete.")
                return False
        self._last_monotonic_ns = monotonic_ns
        self._last_wall_time_utc = wall_time_utc
        try:
            # OpenCV may reuse buffers; the writer owns its independent copy.
            self.queue.put_nowait((sequence, frame.copy(), monotonic_ns, wall_time_utc, gap_ns))
            return True
        except queue.Full:
            self.dropped += 1
            self.events.put({"event": "writer_queue_full", "sequence": sequence,
                             "monotonic_ns": monotonic_ns, "wall_time_utc": wall_time_utc})
            self.abort("Recording could not keep up; frame dropped.")
            return False

    def note_read_failure(self, monotonic_ns: int, wall_time_utc: str) -> None:
        self.read_failures += 1
        self.events.put({"event": "capture_read_failure", "monotonic_ns": monotonic_ns,
                         "wall_time_utc": wall_time_utc})
        self.abort("Camera read failed; recording is incomplete.")

    def finish(self) -> None:
        self.stop_event.set()

    def abort(self, message: str) -> None:
        first_error = False
        with self._lock:
            if self._error is None:
                self._error = message
                first_error = True
        self.stop_event.set()
        if first_error:
            # Surface disconnect/overload even if a disk write is stuck.
            self._status("error", message)

    def join(self, timeout=None) -> None:
        self._thread.join(timeout)

    def _encode(self, frame) -> bytes:
        if self._encoder is not None:
            return self._encoder(frame)
        import cv2
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise OSError("Could not encode frame.")
        return encoded.tobytes()

    def _drain_events(self, event_file) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            event_file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _run(self) -> None:
        try:
            with self.frames_path.open("x", encoding="utf-8") as frames_file, \
                 self.events_path.open("x", encoding="utf-8") as events_file:
                self._status("recording", "Recording camera frames")
                while True:
                    self._drain_events(events_file)
                    try:
                        item = self.queue.get(timeout=0.1)
                    except queue.Empty:
                        if self.stop_event.is_set():
                            break
                        continue
                    sequence, frame, monotonic_ns, wall_time_utc, gap_ns = item
                    filename = f"frame_{sequence:08d}.jpg"
                    (self.frames_dir / filename).write_bytes(self._encode(frame))
                    entry = {"sequence": sequence, "file": f"{self.frames_dir.name}/{filename}",
                             "monotonic_ns": monotonic_ns, "wall_time_utc": wall_time_utc,
                             "clock": "python_perf_counter_ns",
                             "host_gap_ns": gap_ns, "source_segment_id": 0,
                             "source_timestamp": None,
                             "source_timestamp_note": "unavailable_from_capture_read"}
                    frames_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    frames_file.flush()
                    self.written += 1
                self._drain_events(events_file)
        except Exception:
            self.abort("Could not write camera frames; check disk space and permissions.")
        try:
            state = "error" if self._error else "complete"
            self._write_manifest(state)
        except Exception:
            self._error = self._error or "Could not finish camera recording manifest."
            state = "error"
        self._status(state, self._error or "Camera recording saved")
