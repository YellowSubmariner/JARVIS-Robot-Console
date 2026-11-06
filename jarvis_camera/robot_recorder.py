"""Read-only Kinova state recorder, run with the separate Kortex interpreter.

The GUI sends one JSON line containing a session password through stdin and
may then send {"type":"stop"}. Passwords never appear in arguments, logs or
output. The only robot-side changes are the temporary API session and optional
controller-event subscription; this module never sends motion/configuration
commands. Times stamped on receipt are host times, not sensor exposure times.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
CLOCK_NAME = "python_perf_counter_ns"
FEEDBACK_FILE = "robot_feedback.jsonl"
CONTROLLER_FILE = "controller_events.jsonl"
DIAGNOSTICS_FILE = "robot_diagnostics.jsonl"


def wall_time_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def now() -> dict:
    # On Windows Python 3.11 and 3.13 may use different backends for
    # monotonic_ns(); perf_counter_ns() uses a shared QPC-based clock.
    return {"monotonic_ns": time.perf_counter_ns(), "wall_time_utc": wall_time_utc(),
            "clock": CLOCK_NAME}


def feedback_fields(feedback: dict) -> dict:
    """Report fields observed, without asserting sensor truth or known units."""
    joints = feedback.get("actuators")
    interconnect = feedback.get("interconnect") or {}
    gripper = interconnect.get("gripper_feedback") or {}
    motors = gripper.get("motor") or []
    return {
        "joint_count": len(joints) if isinstance(joints, list) else 0,
        "seven_joint_positions_present": isinstance(joints, list) and len(joints) == 7
            and all(isinstance(item, dict) and isinstance(item.get("position"), (float, int))
                    for item in joints),
        "gripper_motor_position_present": isinstance(motors, list) and bool(motors)
            and isinstance(motors[0], dict)
            and isinstance(motors[0].get("position"), (float, int)),
    }


def joint_safety_flags(feedback: dict) -> list | None:
    """Return the raw safety banks, or None when they cannot be determined.

    These are controller-reported bits, not an independent safety assessment.
    Preserve both banks rather than attributing meanings across SDK versions.
    """
    joints = feedback.get("actuators")
    names = ("fault_bank_a", "fault_bank_b", "warning_bank_a", "warning_bank_b")
    if not isinstance(joints, list) or len(joints) != 7:
        return None
    if not all(isinstance(j, dict) and all(type(j.get(n)) is int and j[n] >= 0
                                           for n in names) for j in joints):
        return None
    return [{name: joint[name] for name in names} for joint in joints]


def emit(message_type: str, **fields) -> None:
    """Only progress/control messages go to stdout; never include credentials."""
    print(json.dumps({"type": message_type, **fields}, ensure_ascii=False), flush=True)


class JsonLines:
    def __init__(self, path: Path):
        # Exclusive creation prevents a second run from mixing two episodes.
        self._file = path.open("x", encoding="utf-8", buffering=1, newline="\n")

    def write(self, value: dict) -> None:
        self._file.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class KortexReader:
    """Small adapter around read-only SDK calls, imported in the child process."""

    def __init__(self, ip: str, username: str):
        # Import here so the GUI interpreter need not have the old SDK installed.
        from kortex_api.RouterClient import RouterClient, RouterClientSendOptions
        from kortex_api.SessionManager import SessionManager
        from kortex_api.TCPTransport import TCPTransport
        from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
        from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
        from kortex_api.autogen.messages import Base_pb2, Session_pb2
        from google.protobuf.json_format import MessageToDict

        self._router_type = RouterClient
        self._session_type = SessionManager
        self._transport_type = TCPTransport
        self._base_type = BaseClient
        self._cyclic_type = BaseCyclicClient
        self._base_pb = Base_pb2
        self._session_pb = Session_pb2
        self._message_to_dict = MessageToDict
        self.ip, self.username = ip, username
        self.options = RouterClientSendOptions()
        self.options.timeout_ms = 2500
        self.transport = None
        self.router = None
        self.manager = None
        self.base = None
        self.cyclic = None
        self.handle = None
        self.connected = False
        self.session_open = False

    def _dict(self, message) -> dict:
        return self._message_to_dict(message, preserving_proto_field_name=True,
                                     including_default_value_fields=True)

    def connect(self, password: str) -> None:
        self.transport = self._transport_type()
        # Avoid SDK error callbacks printing uncontrolled values into stdout.
        self.router = self._router_type(self.transport, lambda _error: None)
        self.manager = self._session_type(self.router)
        self.transport.sock.settimeout(5)
        self.transport.connect(self.ip, 10000)
        self.connected = True
        self.transport.sock.settimeout(None)
        session = self._session_pb.CreateSessionInfo()
        session.username = self.username
        session.password = password
        session.session_inactivity_timeout = 10000
        session.connection_inactivity_timeout = 2000
        try:
            self.manager.CreateSession(session, options=self.options)
            self.session_open = True
        finally:
            session.ClearField("password")
        self.base = self._base_type(self.router)
        self.cyclic = self._cyclic_type(self.router)

    def feedback(self) -> dict:
        return self._dict(self.cyclic.RefreshFeedback(options=self.options))

    def subscribe_controller(self, callback) -> None:
        def on_notification(notification):
            # Timestamp on arrival. Robot's own timestamp remains inside payload.
            received = now()
            try:
                callback({**received, "payload": self._dict(notification)})
            except Exception:
                # An SDK receive thread must never terminate on recording errors.
                callback({**received, "conversion_error": True})

        self.handle = self.base.OnNotificationControllerTopic(
            on_notification, self._base_pb.NotificationOptions(), options=self.options)

    def close(self) -> list[str]:
        errors = []
        if self.handle is not None:
            try:
                self.base.Unsubscribe(self.handle, options=self.options)
            except Exception as exc:
                errors.append("unsubscribe:" + type(exc).__name__)
        if self.session_open:
            try:
                self.manager.CloseSession(options=self.options)
            except Exception as exc:
                errors.append("close_session:" + type(exc).__name__)
        if self.connected:
            try:
                self.transport.disconnect()
            except Exception as exc:
                errors.append("disconnect:" + type(exc).__name__)
        return errors


def read_control(stop_event: threading.Event, stream) -> None:
    """Treat closed stdin as stop to avoid an orphan recording indefinitely."""
    try:
        for line in stream:
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(message, dict) and message.get("type") == "stop":
                stop_event.set()
                return
    finally:
        stop_event.set()


def record(output_dir: Path, ip: str, username: str, password: str,
           frequency_hz: float, controller_events: bool, stop_event: threading.Event,
           reader_factory=KortexReader) -> int:
    """Record raw feedback and optional input events without moving the robot."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    names = [FEEDBACK_FILE, DIAGNOSTICS_FILE]
    if controller_events:
        names.append(CONTROLLER_FILE)
    existing = [name for name in names if (output_dir / name).exists()]
    if existing:
        emit("status", state="error", message="Recorder output already exists; refusing to overwrite.")
        return 1

    files = {}
    reader = None
    stats = {"feedback_samples": 0, "feedback_errors": 0, "controller_events": 0,
             "controller_events_dropped": 0, "controller_conversion_errors": 0,
             "missed_poll_periods": 0, "controller_subscription": "not_requested",
             "missing_joint_samples": 0, "missing_gripper_samples": 0,
             "joint_fault_samples": 0, "joint_warning_samples": 0,
             "joint_safety_unavailable_samples": 0,
             "joint_safety_observed_masks": [
                 {"fault_bank_a": 0, "fault_bank_b": 0,
                  "warning_bank_a": 0, "warning_bank_b": 0} for _ in range(7)],
             "training_ready": False}
    try:
        for name in names:
            files[name] = JsonLines(output_dir / name)
        diagnostics = files[DIAGNOSTICS_FILE]
        def diagnostic(kind, **kwargs):
            diagnostics.write({"schema_version": SCHEMA_VERSION, "kind": kind, **now(), **kwargs})

        diagnostic("start", frequency_hz=frequency_hz, controller_events_requested=controller_events,
                   training_ready=False,
                   note="Raw, unverified recording: host times are receipt times, not sensor exposure times; "
                        "camera alignment, gripper semantics and action labels require independent validation.")
        emit("status", state="connecting", message="Connecting to the robot for read-only recording.")
        reader = reader_factory(ip, username)
        reader.connect(password)
        # No additional copy survives this point in the recording function.
        password = None
        events = queue.Queue(maxsize=4096)
        dropped = [0]
        dropped_lock = threading.Lock()

        def on_controller(event):
            try:
                events.put_nowait(event)
            except queue.Full:
                with dropped_lock:
                    dropped[0] += 1

        if controller_events:
            try:
                reader.subscribe_controller(on_controller)
                stats["controller_subscription"] = "subscribed"
                diagnostic("controller_subscribed", note="Raw input events are not verified motion commands.")
            except Exception as exc:
                stats["controller_subscription"] = "failed"
                diagnostic("controller_subscription_failed", error_type=type(exc).__name__)
                emit("warning", code="controller_subscription_failed",
                     message="Controller events unavailable; joint feedback will still be recorded.")

        def drain_events():
            if not controller_events:
                return
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                stats["controller_events"] += 1
                if event.get("conversion_error"):
                    stats["controller_conversion_errors"] += 1
                files[CONTROLLER_FILE].write({"schema_version": SCHEMA_VERSION,
                                               "sequence": stats["controller_events"], **event})
            with dropped_lock:
                stats["controller_events_dropped"] = dropped[0]

        emit("status", state="connecting", message="Session connected; waiting for joint feedback.")
        diagnostic("session_connected_waiting_for_feedback")
        recording_announced = False
        previous_joint_safety = None
        notified_joint_safety = set()
        period_ns = int(1_000_000_000 / frequency_hz)
        next_poll = time.perf_counter_ns()
        while not stop_event.is_set():
            request = now()
            try:
                feedback = reader.feedback()
            except Exception as exc:
                stats["feedback_errors"] += 1
                diagnostic("feedback_error", error_type=type(exc).__name__,
                           request_monotonic_ns=request["monotonic_ns"])
                if stats["feedback_errors"] == 1:
                    emit("warning", code="feedback_error",
                         message="Robot feedback failed; this recording needs review.")
            else:
                received = now()
                fields = feedback_fields(feedback)
                safety = joint_safety_flags(feedback)
                if safety is None:
                    stats["joint_safety_unavailable_samples"] += 1
                    if "unavailable" not in notified_joint_safety:
                        notified_joint_safety.add("unavailable")
                        diagnostic("joint_safety_unavailable", feedback_sequence=stats["feedback_samples"] + 1)
                        emit("warning", code="joint_safety_unavailable",
                             message="Joint safety flags are unavailable; robot condition is unknown.")
                else:
                    faults = any(j["fault_bank_a"] or j["fault_bank_b"] for j in safety)
                    warnings = any(j["warning_bank_a"] or j["warning_bank_b"] for j in safety)
                    stats["joint_fault_samples"] += int(faults)
                    stats["joint_warning_samples"] += int(warnings)
                    for observed, current in zip(stats["joint_safety_observed_masks"], safety):
                        for bank in observed:
                            observed[bank] |= current[bank]
                    if safety != previous_joint_safety:
                        diagnostic("joint_safety_change", feedback_sequence=stats["feedback_samples"] + 1,
                                   joints=safety)
                        previous_joint_safety = safety
                    for present, code, message in (
                        (faults, "joint_fault_detected", "Joint fault reported; stop demonstration and consult the operator."),
                        (warnings, "joint_warning_detected", "Joint warning reported; operator review is required."),
                    ):
                        if present and code not in notified_joint_safety:
                            notified_joint_safety.add(code)
                            emit("warning", code=code, message=message)
                if not fields["seven_joint_positions_present"]:
                    stats["missing_joint_samples"] += 1
                    if stats["missing_joint_samples"] == 1:
                        emit("warning", code="missing_joint_feedback",
                             message="Seven joint position fields were not all observed; review this episode.")
                if not fields["gripper_motor_position_present"]:
                    stats["missing_gripper_samples"] += 1
                    if stats["missing_gripper_samples"] == 1:
                        emit("warning", code="missing_gripper_feedback",
                             message="Gripper position feedback was not observed; review this episode.")
                # A disk or JSON error is fatal, not a transient feedback error.
                files[FEEDBACK_FILE].write({
                    "schema_version": SCHEMA_VERSION,
                    "sequence": stats["feedback_samples"] + 1,
                    "request_monotonic_ns": request["monotonic_ns"],
                    "received_monotonic_ns": received["monotonic_ns"],
                    "received_wall_time_utc": received["wall_time_utc"],
                    "clock": CLOCK_NAME,
                    "fields_observed": fields,
                    "feedback": feedback,
                })
                stats["feedback_samples"] += 1
                if not recording_announced and fields["seven_joint_positions_present"]:
                    recording_announced = True
                    diagnostic("recording", counts=dict(stats))
                    emit("status", state="recording", message="Read-only joint feedback received.",
                         counts=dict(stats))
            drain_events()
            next_poll += period_ns
            current = time.perf_counter_ns()
            if current > next_poll:
                missed = (current - next_poll) // period_ns + 1
                stats["missed_poll_periods"] += missed
                next_poll += missed * period_ns
            stop_event.wait(max(0.0, (next_poll - time.perf_counter_ns()) / 1e9))
        emit("status", state="stopping", message="Closing the read-only session.")
        # Stop further callbacks before the final queue drain.
        try:
            cleanup = reader.close()
            reader = None
        except Exception as exc:
            cleanup = ["cleanup:" + type(exc).__name__]
        if cleanup:
            diagnostic("cleanup_error", errors=cleanup)
            emit("warning", code="cleanup_error", message="Robot session cleanup needs review.")
        drain_events()
        if stats["controller_events_dropped"] or stats["controller_conversion_errors"]:
            emit("warning", code="controller_events_incomplete",
                 message="Some controller events could not be recorded; review this episode.")
        diagnostic("capture_finished", counts=dict(stats))
        if not recording_announced:
            raise RuntimeError("No seven-joint feedback was recorded")
    except Exception as exc:
        # Do not include exception messages: SDK errors can contain credentials.
        if DIAGNOSTICS_FILE in files:
            try:
                files[DIAGNOSTICS_FILE].write({"schema_version": SCHEMA_VERSION,
                    "kind": "fatal_error", **now(), "error_type": type(exc).__name__})
            except Exception:
                pass
        emit("status", state="error", message="Robot recording could not continue.",
             error_type=type(exc).__name__, counts=dict(stats))
        return_code = 1
    else:
        return_code = 0
    finally:
        password = None
        if reader is not None:
            try:
                cleanup = reader.close()
            except Exception as exc:
                cleanup = ["cleanup:" + type(exc).__name__]
            if cleanup and DIAGNOSTICS_FILE in files:
                try:
                    files[DIAGNOSTICS_FILE].write({"schema_version": SCHEMA_VERSION,
                        "kind": "cleanup_error", **now(), "errors": cleanup})
                except Exception:
                    pass
        for output in files.values():
            try:
                output.close()
            except Exception:
                pass
    if return_code == 0:
        emit("status", state="stopped", message="Robot recording stopped.", counts=stats)
    return return_code


def main() -> int:
    # The GUI pipes UTF-8 JSON even on Windows systems whose console code page
    # is not UTF-8; configure both ends before reading any password text.
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ip", default="192.168.1.10")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--frequency-hz", type=float, default=20.0)
    parser.add_argument("--controller-events", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.frequency_hz <= 50:
        emit("status", state="error", message="Sampling frequency must be between 1 and 50 Hz.")
        return 2
    try:
        start = json.loads(sys.stdin.readline())
        if not isinstance(start, dict) or start.get("type") != "start" or not isinstance(start.get("password"), str):
            raise ValueError("invalid start message")
        password = start.pop("password")
        del start
    except (ValueError, TypeError):
        emit("status", state="error", message="A valid start message is required on standard input.")
        return 2
    stop_event = threading.Event()
    threading.Thread(target=read_control, args=(stop_event, sys.stdin), daemon=True).start()
    return record(args.output_dir, args.ip, args.username, password, args.frequency_hz,
                  args.controller_events, stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
