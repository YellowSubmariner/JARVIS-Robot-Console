import json
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from jarvis_camera import capture
from jarvis_camera.frame_recorder import FrameRecorder


class Frame:
    def __init__(self, value):
        self.value = value
        self.shape = (2, 3, 3)

    def copy(self):
        return Frame(self.value)


def line_data(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FrameRecorderTests(unittest.TestCase):
    def test_writes_every_received_frame_and_truthful_timestamps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            updates = []
            recorder = FrameRecorder(root, "top", updates.append,
                                     encoder=lambda frame: str(frame.value).encode())
            self.assertTrue(recorder.submit(Frame(11), 1_000_000_000, "2026-01-01T00:00:00.000000+00:00"))
            self.assertTrue(recorder.submit(Frame(12), 1_100_000_000, "2026-01-01T00:00:00.100000+00:00"))
            recorder.finish()
            recorder.join(5)
            self.assertFalse(recorder.running)
            frames = line_data(root / "top_frames.jsonl")
            self.assertEqual([f["sequence"] for f in frames], [0, 1])
            self.assertEqual(frames[1]["monotonic_ns"], 1_100_000_000)
            self.assertEqual(frames[1]["host_gap_ns"], 100_000_000)
            self.assertEqual(frames[1]["source_segment_id"], 0)
            self.assertIsNone(frames[1]["source_timestamp"])
            self.assertEqual((root / frames[1]["file"]).read_bytes(), b"12")
            manifest = json.loads((root / "top_recording.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["written"], 2)
            self.assertFalse(manifest["source_dropped_frames_known"])
            self.assertEqual(manifest["encoding"]["frame_shape_hwc"], [2, 3, 3])
            self.assertEqual(manifest["encoding"]["format"], "JPEG")
            self.assertTrue(manifest["encoding"]["lossy"])
            self.assertEqual(updates[-1]["state"], "complete")

    def test_capture_read_failure_marks_recording_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            updates = []
            recorder = FrameRecorder(root, "wrist", updates.append, encoder=lambda _: b"x")
            recorder.note_read_failure(42, "2026-01-01T00:00:00.000000+00:00")
            recorder.join(5)
            manifest = json.loads((root / "wrist_recording.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["read_failures"], 1)
            self.assertEqual(updates[-1]["state"], "error")
            self.assertEqual(line_data(root / "wrist_events.jsonl")[0]["event"], "capture_read_failure")

    def test_clock_discontinuity_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            updates = []
            recorder = FrameRecorder(root, "top", updates.append, encoder=lambda _: b"x")
            recorder.submit(Frame(1), 2_000_000_000, "2026-01-01T00:00:00.000000+00:00")
            self.assertFalse(recorder.submit(Frame(2), 1_000_000_000, "2026-01-01T00:00:00.100000+00:00"))
            recorder.join(5)
            manifest = json.loads((root / "top_recording.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["dropped"], 1)
            self.assertEqual(line_data(root / "top_events.jsonl")[0]["event"], "clock_or_capture_gap")

    def test_overload_is_visible_and_invalidates_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            unblock = threading.Event()
            updates = []
            def blocked_encoder(_frame):
                entered.set()
                unblock.wait(3)
                return b"x"
            recorder = FrameRecorder(root, "top", updates.append, queue_size=1, encoder=blocked_encoder)
            self.assertTrue(recorder.submit(Frame(1), 1_000_000_000, "2026-01-01T00:00:00.000000+00:00"))
            self.assertTrue(entered.wait(3))
            self.assertTrue(recorder.submit(Frame(2), 1_100_000_000, "2026-01-01T00:00:00.100000+00:00"))
            self.assertFalse(recorder.submit(Frame(3), 1_200_000_000, "2026-01-01T00:00:00.200000+00:00"))
            unblock.set()
            recorder.join(5)
            manifest = json.loads((root / "top_recording.json").read_text(encoding="utf-8"))
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["dropped"], 1)
            self.assertEqual(updates[-1]["state"], "error")

    def test_existing_camera_recording_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = FrameRecorder(root, "top", lambda _: None, encoder=lambda _: b"x")
            recorder.finish()
            recorder.join(5)
            with self.assertRaises(FileExistsError):
                FrameRecorder(root, "top", lambda _: None, encoder=lambda _: b"x")

    def test_stopping_does_not_regress_to_recording_on_late_start_ack(self):
        class Alive:
            def is_alive(self):
                return True
        stream = capture.CameraStream({"kind": "usb", "name": "test-camera"})
        commands, record_events = queue.Queue(), queue.Queue()
        stream._handle = types.SimpleNamespace(
            process=Alive(), record_command_queue=commands,
            record_status_queue=record_events, status_queue=queue.Queue(),
            frame_queue=queue.Queue())
        stream._values["state"] = "streaming"
        stream.start_recording("episode", "top")
        self.assertEqual(commands.get_nowait()["command"], "start")
        stream.stop_recording()
        self.assertEqual(commands.get_nowait()["command"], "stop")
        record_events.put({"state": "recording", "message": "late"})
        self.assertEqual(stream.poll()["recording"]["state"], "stopping")
        record_events.put({"state": "complete", "message": "saved", "written": 1, "dropped": 0})
        self.assertEqual(stream.poll()["recording"]["state"], "complete")

    def test_recording_branches_before_lossy_preview_queue(self):
        """Simulated driver proves recording isn't sampled from the preview queue."""
        class Encoded:
            def tobytes(self):
                return b"jpeg"
        class FakeCapture:
            def __init__(self, *_args):
                self.count = 0
            def isOpened(self):
                return True
            def set(self, *_args):
                return True
            def read(self):
                time.sleep(0.01)
                self.count += 1
                return True, Frame(self.count)
            def release(self):
                pass
        class FrameQueue(queue.Queue):
            def cancel_join_thread(self):
                pass
        fake_cv2 = types.SimpleNamespace(
            VideoCapture=FakeCapture, CAP_PROP_FRAME_WIDTH=1, CAP_PROP_FRAME_HEIGHT=2,
            CAP_PROP_FPS=3, IMWRITE_JPEG_QUALITY=4,
            imencode=lambda *_args: (True, Encoded()))
        info = types.SimpleNamespace(name="test-camera", index=0, backend=0)
        stop = threading.Event()
        preview = FrameQueue(maxsize=2)
        statuses, commands, recording_statuses = queue.Queue(), queue.Queue(), queue.Queue()
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(sys.modules, {"cv2": fake_cv2}), \
             mock.patch.object(capture, "_camera_infos", return_value=[info]):
            worker = threading.Thread(target=capture._capture_worker,
                                      args=({"kind": "usb", "name": "test-camera", "max_fps": 2},
                                            stop, preview, statuses, commands, recording_statuses))
            worker.start()
            try:
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    try:
                        if statuses.get(timeout=0.1)["state"] == "streaming":
                            break
                    except queue.Empty:
                        pass
                else:
                    self.fail("Simulated camera did not start")
                commands.put({"command": "start", "episode_dir": temp, "camera_name": "top"})
                self.assertEqual(recording_statuses.get(timeout=3)["state"], "recording")
                time.sleep(0.2)
                commands.put({"command": "stop"})
                result = recording_statuses.get(timeout=5)
                self.assertEqual(result["state"], "complete", (result, line_data(Path(temp) / "top_events.jsonl")))
                frames = line_data(Path(temp) / "top_frames.jsonl")
                preview_messages = []
                while not preview.empty():
                    preview_messages.append(preview.get_nowait())
                self.assertGreater(len(frames), 5)
                self.assertGreater(len(frames), max(x["frames"] for x in preview_messages))
            finally:
                stop.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
