"""Offline tests for read-only recorder. No robot or cameras are contacted."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_camera import robot_recorder as rr


class FakeReader:
    def __init__(self, stop, events=True, fail=False):
        self.stop = stop
        self.events = events
        self.fail = fail
        self.calls = 0
        self.closed = False
        self.password = None

    def connect(self, password):
        self.password = password

    def subscribe_controller(self, callback):
        if not self.events:
            raise RuntimeError("event API unavailable")
        callback({"monotonic_ns": 2, "wall_time_utc": "test",
                  "payload": {"controller_element": {"axis": 1}}})

    def feedback(self):
        self.calls += 1
        if self.calls >= 3:
            self.stop.set()
        if self.fail:
            raise RuntimeError("secret that must never appear in logs")
        return {"actuators": [{"position": 12.5, "velocity": 0.1} for _ in range(7)],
                "interconnect": {"gripper_feedback": {"motor": [{"position": 42}]}}}

    def close(self):
        self.closed = True
        self.password = None
        return []


class RecorderTests(unittest.TestCase):
    def run_capture(self, folder, *, events=True, fail=False):
        stop = threading.Event()
        fake = FakeReader(stop, events=events, fail=fail)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = rr.record(Path(folder), "192.0.2.1", "admin", "secret-password",
                             50.0, True, stop,
                             reader_factory=lambda _ip, _username: fake)
        return code, fake, stdout.getvalue()

    def test_feedback_and_input_are_separate_and_password_not_stored(self):
        with tempfile.TemporaryDirectory() as folder:
            code, fake, out = self.run_capture(folder)
            self.assertEqual(code, 0)
            self.assertTrue(fake.closed)
            feedback = [json.loads(x) for x in Path(folder, rr.FEEDBACK_FILE).read_text(encoding="utf-8").splitlines()]
            events = [json.loads(x) for x in Path(folder, rr.CONTROLLER_FILE).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(feedback), 3)
            self.assertEqual(len(events), 1)
            self.assertIn("gripper_feedback", feedback[0]["feedback"]["interconnect"])
            self.assertTrue(feedback[0]["fields_observed"]["gripper_motor_position_present"])
            self.assertTrue(feedback[0]["fields_observed"]["seven_joint_positions_present"])
            self.assertLessEqual(feedback[0]["request_monotonic_ns"], feedback[0]["received_monotonic_ns"])
            self.assertEqual(feedback[0]["clock"], "python_perf_counter_ns")
            self.assertIn("controller_element", events[0]["payload"])
            self.assertNotIn("action", feedback[0])
            self.assertIn('"state": "stopped"', out)
            self.assertIn('"state": "recording"', out)
            self.assertIn('"training_ready": false', out)
            for path in Path(folder).iterdir():
                self.assertNotIn("secret-password", path.read_text(encoding="utf-8"))
            self.assertNotIn("secret-password", out)

    def test_missing_gripper_is_flagged_not_assumed(self):
        fields = rr.feedback_fields({"actuators": [{"position": 0.0}] * 7,
                                     "interconnect": {"gripper_feedback": {"motor": []}}})
        self.assertTrue(fields["seven_joint_positions_present"])
        self.assertFalse(fields["gripper_motor_position_present"])

    def test_subscription_failure_is_reported_and_feedback_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            code, fake, out = self.run_capture(folder, events=False)
            self.assertEqual(code, 0)
            self.assertTrue(fake.closed)
            self.assertIn("controller_subscription_failed", out)
            self.assertEqual(len(Path(folder, rr.FEEDBACK_FILE).read_text(encoding="utf-8").splitlines()), 3)

    def test_joint_faults_and_warnings_are_separately_reported_without_losing_feedback(self):
        class SafetyReader(FakeReader):
            def feedback(self):
                result = super().feedback()
                for joint in result['actuators']:
                    joint.update(fault_bank_a=0, fault_bank_b=0,
                                 warning_bank_a=0, warning_bank_b=0)
                if self.calls == 1:
                    result['actuators'][1]['warning_bank_a'] = 4096
                if self.calls == 2:
                    result['actuators'][2]['fault_bank_b'] = 16
                    result['actuators'][2]['warning_bank_b'] = 4
                return result

        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = rr.record(Path(folder), '192.0.2.1', 'admin', 'secret',
                                 50.0, False, stop,
                                 reader_factory=lambda *_: SafetyReader(stop))
            self.assertEqual(code, 0)
            self.assertIn('joint_fault_detected', stdout.getvalue())
            self.assertIn('joint_warning_detected', stdout.getvalue())
            feedback = Path(folder, rr.FEEDBACK_FILE).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(feedback), 3)
            diag = [json.loads(line) for line in Path(folder, rr.DIAGNOSTICS_FILE).read_text(encoding='utf-8').splitlines()]
            changes = [row for row in diag if row['kind'] == 'joint_safety_change']
            self.assertEqual(len(changes), 3)
            counts = next(row['counts'] for row in diag if row['kind'] == 'capture_finished')
            self.assertEqual(counts['joint_warning_samples'], 2)
            self.assertEqual(counts['joint_fault_samples'], 1)
            self.assertEqual(counts['joint_safety_observed_masks'][1]['warning_bank_a'], 4096)
            self.assertEqual(counts['joint_safety_observed_masks'][2]['fault_bank_b'], 16)
            self.assertFalse(counts['training_ready'])

    def test_absent_safety_flags_are_unknown_not_clear(self):
        self.assertIsNone(rr.joint_safety_flags({'actuators': [{'position': 0.0}] * 7}))

    def test_zero_feedback_reports_failure_and_no_exception_text(self):
        with tempfile.TemporaryDirectory() as folder:
            code, fake, out = self.run_capture(folder, fail=True)
            self.assertEqual(code, 1)
            self.assertTrue(fake.closed)
            self.assertIn('"state": "error"', out)
            self.assertNotIn("secret", out)
            self.assertNotIn("secret", Path(folder, rr.DIAGNOSTICS_FILE).read_text(encoding="utf-8"))

    def test_write_failure_is_fatal_not_a_sensor_error(self):
        original_write = rr.JsonLines.write

        def fail_feedback_write(target, row):
            if "feedback" in row:
                raise OSError("disk full")
            return original_write(target, row)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(rr.JsonLines, "write", fail_feedback_write):
                code, fake, out = self.run_capture(folder)
            self.assertEqual(code, 1)
            self.assertTrue(fake.closed)
            self.assertIn('"state": "error"', out)
            self.assertNotIn('"state": "recording"', out)
            self.assertNotIn('"code": "feedback_error"', out)

    def test_existing_episode_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, rr.FEEDBACK_FILE)
            path.write_text("existing", encoding="utf-8")
            stop = threading.Event()
            with contextlib.redirect_stdout(io.StringIO()):
                code = rr.record(Path(folder), "192.0.2.1", "admin", "hidden",
                                 20.0, False, stop,
                                 reader_factory=lambda *_: self.fail("should not connect"))
            self.assertEqual(code, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_eof_or_stop_request_stops_worker(self):
        for text in ('{"type":"stop"}\n', ''):
            stop = threading.Event()
            rr.read_control(stop, io.StringIO(text))
            self.assertTrue(stop.is_set())

    def test_start_protocol_passes_unicode_password_without_printing(self):
        observed = []
        def capture_start(_folder, _ip, _user, password, _hz, _events, _stop):
            observed.append(password)
            return 0
        input_stream = io.StringIO('{"type":"start","password":"密钥"}\n')
        output_stream = io.StringIO()
        with patch.object(rr.sys, "argv", ["recorder", "--output-dir", "unused"]), \
             patch.object(rr.sys, "stdin", input_stream), \
             patch.object(rr.sys, "stdout", output_stream), \
             patch.object(rr, "record", capture_start):
            self.assertEqual(rr.main(), 0)
        self.assertEqual(observed, ["密钥"])
        self.assertNotIn("密钥", output_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
