"""Offline, causal preparation of read-only demonstration recordings.

This module never connects to a robot or changes raw recordings. Receive-time
alignment is not exposure synchronization. The eight position targets are
future *measurements*, including continuous gripper position, not recovered
controller commands. Every successful preparation still needs human review.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import uuid


CLOCK = 'python_perf_counter_ns'
ACTION_SCHEMA = 'next_measured_position_v1'
REVIEW_CHECKS = ('alignment', 'position_targets', 'stable_end')
RAW_FILES = ('metadata.json', 'events.jsonl', 'robot_feedback.jsonl',
             'top_frames.jsonl', 'wrist_frames.jsonl',
             'top_recording.json', 'wrist_recording.json')
OPTIONAL_FILES = ('robot_diagnostics.jsonl', 'controller_events.jsonl',
                  'top_events.jsonl', 'wrist_events.jsonl')


class PreparationError(ValueError):
    """The recording cannot currently be prepared, reviewed or exported."""


class _Invalid(PreparationError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PreparationConfig:
    """Data-quality thresholds, not robot limits or a safety certificate."""

    fps: int = 20
    max_camera_age_ms: float = 200.0
    max_feedback_age_ms: float = 150.0
    max_joint_speed_deg_s: float = 120.0
    stable_end_seconds: float = 0.5
    stable_joint_range_deg: float = 1.0
    stable_gripper_range: float = 0.02
    min_samples: int = 10

    def validate(self):
        if type(self.fps) is not int or not 1 <= self.fps <= 50:
            raise PreparationError('整理频率必须是 1–50 Hz 的整数。')
        if type(self.min_samples) is not int or self.min_samples < 2:
            raise PreparationError('最少样本数必须是至少 2 的整数。')
        for field in fields(self):
            if field.name in ('fps', 'min_samples'):
                continue
            value = getattr(self, field.name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value <= 0):
                raise PreparationError('整理参数必须是有限正数：' + field.name)


def _config(value) -> PreparationConfig:
    if value is None:
        value = PreparationConfig()
    elif isinstance(value, dict):
        try:
            value = PreparationConfig(**value)
        except TypeError as error:
            raise PreparationError('整理参数名称不正确。') from error
    if not isinstance(value, PreparationConfig):
        raise PreparationError('整理参数格式不正确。')
    value.validate()
    return value


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(episode_dir) -> Path:
    path = Path(episode_dir).expanduser()
    if not path.is_dir():
        raise PreparationError('示范目录不存在。')
    return path.resolve()


def _within(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _Invalid('invalid_path', '记录中的文件路径为空或格式错误。')
    posix = PurePosixPath(relative.replace('\\', '/'))
    if posix.is_absolute() or '..' in posix.parts or ':' in relative:
        raise _Invalid('invalid_path', '记录中的文件路径越出了示范目录。')
    path = root.joinpath(*posix.parts)
    if not path.resolve().is_relative_to(root):
        raise _Invalid('invalid_path', '记录中的文件链接指向示范目录之外。')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise _Invalid('invalid_path', '示范文件不能通过符号链接引用其他位置。')
    return path


def _parse(raw: bytes, name: str):
    try:
        text = raw.decode('utf-8')
        reject_constant = lambda _value: (_ for _ in ()).throw(ValueError('non-finite JSON'))
        if name.endswith('.jsonl'):
            items = [json.loads(line, parse_constant=reject_constant)
                     for line in text.splitlines() if line.strip()]
            if not all(isinstance(row, dict) for row in items):
                raise ValueError('not object rows')
            return items
        value = json.loads(text, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError('not object')
        return value
    except (UnicodeError, ValueError, TypeError) as error:
        raise _Invalid('invalid_json', f'{name} 内容不完整或格式错误。') from error


def _stamp(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _snapshot(root: Path):
    """Read source metadata and hash images incrementally, without decoding them."""
    documents, hashes, stamps = {}, {}, {}
    for name in RAW_FILES + OPTIONAL_FILES:
        path = _within(root, name)
        if not path.is_file():
            if name in OPTIONAL_FILES:
                continue
            raise _Invalid('missing_file', f'缺少原始记录：{name}')
        before = _stamp(path)
        data = path.read_bytes()
        if _stamp(path) != before:
            raise _Invalid('source_changed', '原始记录正在变化，请结束录制后再整理。')
        stamps[name] = before
        hashes[name] = hashlib.sha256(data).hexdigest()
        documents[name] = _parse(data, name)
    for camera in ('top', 'wrist'):
        seen = set()
        for row in documents[camera + '_frames.jsonl']:
            relative = row.get('file')
            path = _within(root, relative)
            normalized = path.relative_to(root).as_posix()
            if path.parent != root / (camera + '_frames') or path.suffix.lower() != '.jpg':
                raise _Invalid('invalid_path', f'{camera} 图片路径与相机目录不一致。')
            if normalized in seen:
                raise _Invalid('duplicate_frame_path', f'{camera} 有重复的图片文件名。')
            seen.add(normalized)
            if not path.is_file() or path.stat().st_size == 0:
                raise _Invalid('missing_image', f'{camera} 图片缺失或为空。')
            before = _stamp(path)
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if _stamp(path) != before:
                raise _Invalid('source_changed', '图片正在写入，请结束录制后再整理。')
            hashes[normalized] = digest.hexdigest()
            stamps[normalized] = before
    fingerprint = hashlib.sha256(_json_bytes(hashes)).hexdigest()
    return documents, fingerprint, stamps


def _clock_value(row: dict, key: str) -> int:
    value = row.get(key)
    if row.get('clock') != CLOCK or type(value) is not int or value < 0:
        raise _Invalid('invalid_clock', '时间戳缺失、格式错误，或不是统一的主机时钟。')
    return value


def _times(rows: list, key: str, name: str) -> list[int]:
    if not rows:
        raise _Invalid('empty_stream', f'{name} 没有记录。')
    values = [_clock_value(row, key) for row in rows]
    if any(b <= a for a, b in zip(values, values[1:])):
        raise _Invalid('non_monotonic_time', f'{name} 时间重复或倒退，不能自动整理。')
    sequences = [row.get('sequence') for row in rows]
    if any(type(value) is not int or value < 0 for value in sequences):
        raise _Invalid('invalid_sequence', f'{name} 序号缺失或格式错误。')
    if any(b != a + 1 for a, b in zip(sequences, sequences[1:])):
        raise _Invalid('sequence_gap', f'{name} 序号不连续，可能存在缺失。')
    return values


def _finite(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _Invalid('invalid_feedback', name + ' 缺失或不是有限数值。')
    return float(value)


def _states(rows: list, issues: list) -> tuple[list, list]:
    states, degrees = [], []
    saw_warning = False
    banks = ('fault_bank_a', 'fault_bank_b', 'warning_bank_a', 'warning_bank_b')
    for row in rows:
        received = _clock_value(row, 'received_monotonic_ns')
        requested = row.get('request_monotonic_ns')
        if type(requested) is not int or requested < 0 or requested > received:
            raise _Invalid('invalid_request_time', '机器人反馈请求与接收时间不正确。')
        feedback = row.get('feedback')
        if not isinstance(feedback, dict):
            raise _Invalid('invalid_feedback', '机器人反馈格式错误。')
        joints = feedback.get('actuators')
        if not isinstance(joints, list) or len(joints) != 7 or not all(isinstance(j, dict) for j in joints):
            raise _Invalid('missing_joints', '需要完整的七个关节反馈。')
        if any(any(type(j.get(k)) is not int or j[k] < 0 for k in banks) for j in joints):
            raise _Invalid('safety_unknown', '关节故障与警告字段不完整，需先检查原始记录。')
        if any(j['fault_bank_a'] or j['fault_bank_b'] for j in joints):
            raise _Invalid('joint_fault', '示范中存在关节故障，保留原始数据但不自动导出训练样本。')
        saw_warning |= any(j['warning_bank_a'] or j['warning_bank_b'] for j in joints)
        q = [_finite(j.get('position'), '关节角') for j in joints]
        try:
            motors = feedback['interconnect']['gripper_feedback']['motor']
        except (KeyError, TypeError):
            motors = None
        if not isinstance(motors, list) or len(motors) != 1 or not isinstance(motors[0], dict):
            raise _Invalid('missing_gripper', '需要明确的单夹爪位置反馈，不能猜测多个电机的含义。')
        grip = _finite(motors[0].get('position'), '夹爪位置')
        if not 0 <= grip <= 100:
            raise _Invalid('gripper_range', '夹爪反馈不在已约定的 0–100 百分比范围。')
        degrees.append(q)
        states.append([math.radians(v) for v in q] + [grip / 100.0])
    if saw_warning:
        issues.append({'code': 'joint_warning', 'severity': 'warning',
                       'message': '记录中有机器人关节警告，检查现场含义后再决定是否使用。'})
    return states, degrees


def _prepare(documents: dict, config: PreparationConfig, manifest: dict) -> list:
    metadata = documents['metadata.json']
    if metadata.get('recording_status') != 'complete':
        raise _Invalid('incomplete_recording', '本次录制未完整结束，原始数据已保留。')
    if metadata.get('user_outcome') != 'success':
        raise _Invalid('not_success', '失败或人工接管示范单独保留，首版不自动混入成功模仿数据。')
    task = metadata.get('task')
    if not isinstance(task, str) or not task.strip():
        raise _Invalid('missing_task', '缺少本次示范的短任务文字。')
    manifest['task'] = task.strip()
    active = metadata.get('active')
    if not isinstance(active, dict):
        raise _Invalid('missing_active_time', '缺少录制就绪时间，请用新版程序重新录制。')
    start = _clock_value(active, 'monotonic_ns')
    events = documents['events.jsonl']
    ends = [row for row in events if row.get('event') == 'stop_requested']
    if len(ends) != 1:
        raise _Invalid('missing_stop_time', '需要唯一的结束录制时间，不能把复位或下一段混入。')
    stop = _clock_value(ends[0], 'monotonic_ns')
    if stop <= start:
        raise _Invalid('invalid_episode_range', '示范起止时间不正确。')
    feedback = documents['robot_feedback.jsonl']
    robot_times = _times(feedback, 'received_monotonic_ns', '机器人反馈')
    states, degrees = _states(feedback, manifest['issues'])
    camera_rows, camera_times = {}, {}
    for name in ('top', 'wrist'):
        recording = documents[name + '_recording.json']
        rows = documents[name + '_frames.jsonl']
        if (recording.get('state') != 'complete' or recording.get('complete') is not True
                or recording.get('dropped', 0) or recording.get('read_failures', 0)
                or recording.get('error')):
            raise _Invalid('incomplete_camera', f'{name} 相机记录不完整，不能自动补成正常样本。')
        if recording.get('clock') != CLOCK:
            raise _Invalid('invalid_clock', f'{name} 相机没有共同主机时钟。')
        if recording.get('written') != len(rows):
            raise _Invalid('frame_count_mismatch', f'{name} 图片数量与索引不一致。')
        camera_rows[name] = rows
        camera_times[name] = _times(rows, 'monotonic_ns', name + ' 相机')
        if any(row.get('source_segment_id', 0) != 0 for row in rows):
            raise _Invalid('camera_reconnected', f'{name} 相机录制期间重新连接，需要拆开检查。')

    counts = metadata.get('robot_counts') or {}
    if not isinstance(counts, dict):
        raise _Invalid('invalid_feedback_counts', '机器人记录统计格式错误，请检查原始记录。')
    if any(counts.get(key, 0) for key in ('feedback_errors', 'missing_joint_samples', 'missing_gripper_samples')):
        raise _Invalid('feedback_errors', '机器人反馈有读取错误或缺失，请检查原始记录。')
    if 'feedback_samples' in counts and counts['feedback_samples'] != len(feedback):
        raise _Invalid('feedback_count_mismatch', '机器人反馈数量与录制记录不一致。')

    # Only the beginning/end can be trimmed. Internal holes reject the whole
    # sequence, so the remaining rows never compress missing time into fake fps.
    period = 1_000_000_000 / config.fps
    first_available = max(start, robot_times[0], *(ts[0] for ts in camera_times.values()))
    end = min(stop, robot_times[-1], *(ts[-1] for ts in camera_times.values()))
    first_k = math.ceil((first_available - start) / period)
    last_k = math.floor((end - start) / period)
    grid = [start + round(k * period) for k in range(first_k, last_k + 1)]
    if len(grid) - 1 < config.min_samples:
        raise _Invalid('too_short', '共同有效记录太短，请录制完整取放并在结束前稳定停留。')
    robot_indices = [bisect_right(robot_times, t) - 1 for t in grid]
    image_indices = {name: [bisect_right(ts, t) - 1 for t in grid]
                     for name, ts in camera_times.items()}
    robot_ages = [(t - robot_times[i]) / 1e6 for t, i in zip(grid, robot_indices)]
    camera_ages = {name: [(t - camera_times[name][i]) / 1e6
                          for t, i in zip(grid, indices)]
                   for name, indices in image_indices.items()}
    if max(robot_ages) > config.max_feedback_age_ms:
        raise _Invalid('stale_feedback', '中间有过旧或缺失的机器人反馈；不会删帧压缩时间。')
    if any(max(ages) > config.max_camera_age_ms for ages in camera_ages.values()):
        raise _Invalid('stale_camera', '中间有过旧的相机画面；不会把缺失时间拼成连续示范。')

    used_start = max(0, robot_indices[0])
    used_end = robot_indices[-1]
    for index in range(used_start + 1, used_end + 1):
        differences = [abs(a - b) for a, b in zip(degrees[index], degrees[index - 1])]
        if max(differences) > 180:
            raise _Invalid('joint_angle_wrap', '关节角跨越了角度边界，需要明确关节参考后再处理；没有盲目展开角度。')
        dt = (robot_times[index] - robot_times[index - 1]) / 1e9
        if max(differences) / dt > config.max_joint_speed_deg_s:
            raise _Invalid('joint_jump', '关节角变化超过本次数据检查阈值，请检查时间和角度单位。')

    tail_start = grid[-1] - int(config.stable_end_seconds * 1e9)
    if tail_start < grid[0]:
        raise _Invalid('too_short_for_terminal_check', '末尾稳定观察时间不足。')
    tail_indices = range(bisect_right(robot_times, tail_start) - 1, robot_indices[-1] + 1)
    tail_indices = [i for i in tail_indices if used_start <= i <= used_end]
    if len(tail_indices) < 2:
        raise _Invalid('too_short_for_terminal_check', '末尾反馈不足，不能判断是否稳定。')
    joint_ranges = [max(degrees[i][j] for i in tail_indices) - min(degrees[i][j] for i in tail_indices)
                    for j in range(7)]
    gripper_range = max(states[i][7] for i in tail_indices) - min(states[i][7] for i in tail_indices)
    if max(joint_ranges) > config.stable_joint_range_deg or gripper_range > config.stable_gripper_range:
        raise _Invalid('unstable_terminal_state', '结束前机械臂或夹爪仍在变化；请放稳后停留片刻再结束录制。')

    manifest['metrics'] = {
        'duration_seconds': (grid[-1] - grid[0]) / 1e9,
        'trimmed_start_ms': (grid[0] - start) / 1e6,
        'trimmed_end_ms': (stop - grid[-1]) / 1e6,
        'max_feedback_age_ms': max(robot_ages),
        'max_camera_age_ms': {name: max(ages) for name, ages in camera_ages.items()},
        'reused_camera_frames': {name: sum(a == b for a, b in zip(indices, indices[1:]))
                                 for name, indices in image_indices.items()},
        'terminal_joint_range_deg': joint_ranges,
        'terminal_gripper_range': gripper_range,
    }
    result = []
    for k, t in enumerate(grid[:-1]):
        row = {'timestamp': k / config.fps, 'monotonic_ns': t,
               'observation.state': states[robot_indices[k]],
               'action': states[robot_indices[k + 1]],
               'source_ages_ms': {'robot': robot_ages[k], 'action_robot': robot_ages[k + 1]},
               'source_sequences': {'robot': feedback[robot_indices[k]]['sequence'],
                                    'action_robot': feedback[robot_indices[k + 1]]['sequence']},
               'action_monotonic_ns': grid[k + 1]}
        for name in ('top', 'wrist'):
            frame = camera_rows[name][image_indices[name][k]]
            row['observation.images.' + name] = frame['file'].replace('\\', '/')
            row['source_ages_ms'][name] = camera_ages[name][k]
            row['source_sequences'][name] = frame['sequence']
        result.append(row)
    return result


def _signature(manifest: dict, samples_bytes: bytes) -> str:
    content = dict(manifest)
    content.pop('prepared_signature', None)
    return hashlib.sha256(_json_bytes(content) + b'\n' + samples_bytes).hexdigest()


def _atomic_bytes(path: Path, content: bytes):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(content)
            stream.flush()
        temp.replace(path)
    except OSError as error:
        raise PreparationError('整理结果未能完整写入，请检查磁盘空间和目录权限。') from error


def prepare_episode(episode_dir, config=None) -> dict:
    """Generate candidate samples. Invalid raw data returns a blocked manifest.

    Does not set training_ready or modify metadata.json. A new generation
    invalidates every earlier human review, even if the same data is used.
    """
    root = _root(episode_dir)
    config = _config(config)
    # Invalidate the earlier human decision before any reprocessing attempt,
    # including one interrupted by a full disk before the new manifest commits.
    review_path = _within(root, 'prepared/review.json')
    try:
        review_path.unlink(missing_ok=True)
    except OSError as error:
        raise PreparationError('无法使旧检查记录失效，请检查目录权限。') from error
    manifest = {
        'schema_version': 1, 'generation_id': uuid.uuid4().hex, 'created_at': _utc(),
        'status': 'blocked', 'technical_pass': False, 'training_ready': False,
        'task': '', 'fps': config.fps, 'num_samples': 0,
        'action_schema': ACTION_SCHEMA,
        'state_names': [f'q{i}' for i in range(1, 8)] + ['gripper_measured'],
        'action_names': [f'q{i}_target' for i in range(1, 8)] + ['gripper_position_proxy'],
        'units': {'joints': 'radian', 'gripper': 'normalized_0_1'},
        'joint_representation': 'raw_degrees_converted_to_radians_no_wrap',
        'gripper_label_source': 'next_measured_position_proxy_not_command',
        'alignment': 'causal_host_receipt_time', 'source_fingerprint': None,
        'configuration': asdict(config), 'metrics': {},
        'issues': [
            {'code': 'exposure_sync_unverified', 'severity': 'warning',
             'message': '仅按同一电脑的接收时间对齐，不能自动证明相机曝光同步；请回放检查延迟。'},
            {'code': 'measured_position_targets', 'severity': 'warning',
             'message': '标签来自下一时刻实测关节和连续夹爪位置，不是原控制命令；夹持目标需验证。'},
        ],
    }
    samples = []
    try:
        documents, fingerprint, stamps = _snapshot(root)
        manifest['source_fingerprint'] = fingerprint
        samples = _prepare(documents, config, manifest)
        if any(_stamp(_within(root, name)) != before for name, before in stamps.items()):
            raise _Invalid('source_changed', '原始记录在整理时发生变化，请重新整理。')
        manifest.update(status='ready_for_review', technical_pass=True, num_samples=len(samples))
    except _Invalid as error:
        samples = []
        manifest['issues'].append({'code': error.code, 'severity': 'error', 'message': str(error)})
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        samples = []
        manifest['issues'].append({'code': 'source_read_error', 'severity': 'error',
                                   'message': '原始记录无法完整读取或字段格式错误，请检查文件。',
                                   'error_type': type(error).__name__})
    directory = _within(root, 'prepared')
    try:
        directory.mkdir(exist_ok=True)
    except OSError as error:
        raise PreparationError('无法建立整理结果目录。') from error
    samples_bytes = b''.join(_json_bytes(row) + b'\n' for row in samples)
    manifest['prepared_signature'] = _signature(manifest, samples_bytes)
    # Manifest goes last: concurrent readers reject a half-written generation.
    _atomic_bytes(_within(root, 'prepared/samples.jsonl'), samples_bytes)
    _atomic_bytes(_within(root, 'prepared/manifest.json'), _json_bytes(manifest))
    return manifest


def load_prepared_episode(episode_dir, require_review=False) -> tuple[dict, list]:
    """Read a valid unchanged generation; optionally require explicit review."""
    root = _root(episode_dir)
    try:
        manifest = _parse(_within(root, 'prepared/manifest.json').read_bytes(), 'manifest.json')
        data = _within(root, 'prepared/samples.jsonl').read_bytes()
        if manifest.get('prepared_signature') != _signature(manifest, data):
            raise PreparationError('整理结果已变化或尚未写完，请重新整理与检查。')
        if manifest.get('technical_pass') is not True or manifest.get('status') != 'ready_for_review':
            messages = [item.get('message', '') for item in manifest.get('issues', [])
                        if item.get('severity') == 'error']
            raise PreparationError('本次示范未通过整理检查。' + (' ' + messages[0] if messages else ''))
        if manifest.get('schema_version') != 1 or manifest.get('action_schema') != ACTION_SCHEMA:
            raise PreparationError('整理格式版本不受支持，请重新整理。')
        source, fingerprint, stamps = _snapshot(root)
        if any(_stamp(_within(root, name)) != before for name, before in stamps.items()):
            raise PreparationError('原始数据正在变化，请结束录制后重新整理。')
        if fingerprint != manifest.get('source_fingerprint'):
            raise PreparationError('原始数据已经变化，请重新整理与检查。')
        if (source['metadata.json'].get('recording_status') != 'complete'
                or source['metadata.json'].get('user_outcome') != 'success'):
            raise PreparationError('仅能导出完整的成功示范。')
        samples = _parse(data, 'samples.jsonl')
        if len(samples) != manifest.get('num_samples') or not samples:
            raise PreparationError('整理样本数不正确，请重新整理。')
        if require_review:
            review = _parse(_within(root, 'prepared/review.json').read_bytes(), 'review.json')
            checks = review.get('checks')
            if (review.get('source_fingerprint') != fingerprint
                    or review.get('prepared_signature') != manifest['prepared_signature']
                    or review.get('reviewed_for_export') is not True
                    or not isinstance(checks, dict)
                    or any(checks.get(key) is not True for key in REVIEW_CHECKS)):
                raise PreparationError('检查记录不完整或已经过期，请重新回放确认。')
        return manifest, samples
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PreparationError('整理结果或检查记录不可用，请先整理并完成检查。') from error


def approve_episode(episode_dir, checks: dict) -> dict:
    """Record human review for exactly this source and preparation generation."""
    if not isinstance(checks, dict) or any(checks.get(key) is not True for key in REVIEW_CHECKS):
        raise PreparationError('请明确确认画面时序、位置目标适用性和稳定终态三项检查。')
    root = _root(episode_dir)
    manifest, _samples = load_prepared_episode(root)
    review = {
        'schema_version': 1, 'reviewed_at': _utc(), 'reviewed_for_export': True,
        'source_fingerprint': manifest['source_fingerprint'],
        'prepared_signature': manifest['prepared_signature'],
        'checks': {key: True for key in REVIEW_CHECKS},
        'review_kind': 'human_confirmation_not_automatic_robot_validation',
        'action_schema': ACTION_SCHEMA,
        'gripper_label_source': manifest['gripper_label_source'],
    }
    _atomic_bytes(_within(root, 'prepared/review.json'), _json_bytes(review))
    return review
