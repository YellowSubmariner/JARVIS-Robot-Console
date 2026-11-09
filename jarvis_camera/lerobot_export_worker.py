"""Convert a reviewed JARVIS bundle into independent LeRobot train/val/test roots.

This file can run standalone with a separately installed LeRobot interpreter.
It never loads policy weights, contacts a robot, or installs dependencies.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


LEROBOT_REVISION = '9e5ee9859480e425a97a6836bb3fee479539beb5'
IMAGE_KEYS = ('observation.images.top', 'observation.images.wrist')
CHECK_KEYS = ('alignment', 'position_targets', 'stable_end')
POSITION_CONTRACT = {
    'state_names': [f'q{i}' for i in range(1, 8)] + ['gripper_measured'],
    'action_names': [f'q{i}_target' for i in range(1, 8)] + ['gripper_position_proxy'],
    'joint_representation': 'raw_degrees_converted_to_radians_no_wrap',
    'gripper_label_source': 'next_measured_position_proxy_not_command',
    'alignment': 'causal_host_receipt_time',
}
SUPPORTED_JOINT_REPRESENTATIONS = (
    'raw_degrees_converted_to_radians_no_wrap',
    'centered_degrees_to_radians_v1',
)
CENTERED_REFERENCE_DEGREES = [0, 340, 180, 214, 0, 310, 90]


def _valid_joint_reference(contract):
    if contract.get('joint_representation') != 'centered_degrees_to_radians_v1':
        return 'joint_reference_degrees' not in contract
    reference = contract.get('joint_reference_degrees')
    return (isinstance(reference, list) and reference == CENTERED_REFERENCE_DEGREES
            and all(type(value) is int for value in reference))


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('数据包中存在无效文件路径。')
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ('.', '..') for part in path.parts):
        raise ValueError('数据包文件不能引用包外路径。')
    result = (root / Path(*path.parts)).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError('数据包文件缺失或指向包外。')
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _check_hash(path: Path, expected) -> None:
    if not isinstance(expected, str) or len(expected) != 64 or _sha256(path) != expected:
        raise ValueError('数据包文件校验失败，可能被修改或未完整复制，请重新导出。')


def _verified_bytes(path: Path, expected) -> bytes:
    data = path.read_bytes()
    if not isinstance(expected, str) or len(expected) != 64 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('数据包文件校验失败，可能被修改或未完整复制，请重新导出。')
    return data


def _load_dataset_factory():
    # All storage used below is local. A missing file must not trigger a download.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_DATASETS_OFFLINE'] = '1'
    try:
        distribution = importlib.metadata.distribution('lerobot')
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as error:
        raise ValueError('此 Python 尚未安装 LeRobot。数据包已保存，请选择已按说明配置的数据转换 Python。') from error
    revision = None
    direct_url = distribution.read_text('direct_url.json')
    if direct_url:
        try:
            revision = json.loads(direct_url).get('vcs_info', {}).get('commit_id')
        except (ValueError, AttributeError):
            pass
    if revision is None:
        # Editable uv installs point to a local checkout. Reading its commit is
        # local and does not fetch anything or change the user's Git settings.
        import inspect
        module_path = Path(inspect.getfile(LeRobotDataset)).resolve()
        for parent in module_path.parents:
            if (parent / '.git').exists():
                try:
                    revision = subprocess.check_output(
                        ['git', '-C', str(parent), 'rev-parse', 'HEAD'],
                        stderr=subprocess.DEVNULL, text=True, timeout=10,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()
                except (OSError, subprocess.SubprocessError):
                    pass
                break
    if revision != LEROBOT_REVISION:
        raise ValueError(f'数据转换环境需要固定 LeRobot 提交 {LEROBOT_REVISION}；当前版本无法确认一致，请按说明配置。')
    return LeRobotDataset.create, {'revision': revision, 'version': distribution.version, 'revision_verified': True}


def _progress(callback, **details):
    if callback is not None:
        callback({'type': 'progress', **details})


def _load_samples(root: Path, manifest: dict):
    path = _safe_file(root, 'samples.jsonl')
    data = _verified_bytes(path, manifest.get('files_sha256', {}).get('samples.jsonl'))
    rows = []
    for line in data.decode('utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows or len(rows) != manifest.get('num_samples'):
        raise ValueError('数据包的样本数量不一致。')
    return rows


def convert_bundle(bundle_dir, output_dir, *, dataset_factory=None, progress=None) -> dict:
    """Stream RGB frames into three datasets, keeping training statistics isolated.

    ``dataset_factory`` is injectable for offline format tests; ordinary callers
    leave it unset to use and verify the fixed LeRobot installation.
    """
    import numpy as np
    from PIL import Image

    bundle_root = Path(bundle_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('转换输出目录已经存在，请选择新的目录。')
    if output.resolve().is_relative_to(bundle_root) or bundle_root.is_relative_to(output.resolve()):
        raise ValueError('转换输出目录必须与输入数据包分开。')
    bundle_path = _safe_file(bundle_root, 'bundle.json')
    bundle_bytes = bundle_path.read_bytes()
    source_bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    bundle = json.loads(bundle_bytes)
    if bundle.get('schema_version') != 1 or bundle.get('format') != 'jarvis_reviewed_demonstrations' or bundle.get('complete') is not True:
        raise ValueError('这不是完整的已检查示范数据包。')
    if bundle.get('lerobot_revision') != LEROBOT_REVISION:
        raise ValueError('数据包指定的 LeRobot 版本与转换组件不同，请使用随包附带的转换组件。')
    contract = bundle.get('feature_contract') or {}
    fps = contract.get('fps')
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or int(fps) != fps or not 1 <= fps <= 50:
        raise ValueError('数据包的采样频率无效。')
    fps = int(fps)
    if contract.get('action_schema') != 'next_measured_position_v1' or contract.get('units') != {'joints': 'radian', 'gripper': 'normalized_0_1'}:
        raise ValueError('数据包动作定义不受此版本支持。')
    if (contract.get('joint_representation') not in SUPPORTED_JOINT_REPRESENTATIONS
            or not _valid_joint_reference(contract)
            or any(contract.get(key) != value for key, value in POSITION_CONTRACT.items()
                   if key != 'joint_representation')):
        raise ValueError('关节顺序、夹爪来源或时间对齐定义不受此版本支持。')
    entries = bundle.get('episodes', [])
    if not isinstance(entries, list) or len(entries) < 3:
        raise ValueError('数据包需要至少三条完整示范。')
    counts = {name: 0 for name in ('train', 'val', 'test')}
    loaded = []
    ids, fingerprints = set(), set()
    shapes = {}
    for index, entry in enumerate(entries, 1):
        if entry.get('split') not in counts or not isinstance(entry.get('id'), str) or entry['id'] in ids:
            raise ValueError('数据集划分无效或同一示范被重复使用。')
        ids.add(entry['id'])
        manifest_path = _safe_file(bundle_root, entry.get('path', '') + '/episode.json')
        episode_root = manifest_path.parent
        manifest = json.loads(_verified_bytes(manifest_path, entry.get('manifest_sha256')))
        if manifest.get('feature_contract') != contract:
            raise ValueError('数据包内的动作定义不一致。')
        checks = (manifest.get('review') or {}).get('checks') or {}
        if any(checks.get(name) is not True for name in CHECK_KEYS):
            raise ValueError('存在尚未完成人工检查的示范。')
        fingerprint = manifest.get('source_fingerprint')
        if not isinstance(fingerprint, str) or not fingerprint or fingerprint in fingerprints:
            raise ValueError('示范来源标识缺失或重复，不能跨集合重复使用。')
        fingerprints.add(fingerprint)
        task = manifest.get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('示范缺少任务说明。')
        rows = _load_samples(episode_root, manifest)
        if entry.get('num_samples') != len(rows):
            raise ValueError('数据包目录中的样本数量不一致。')
        previous = None
        first_monotonic = None
        checked_images = set()
        for frame_index, row in enumerate(rows):
            timestamp = row.get('timestamp')
            if isinstance(timestamp, bool) or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
                raise ValueError('样本时间无效。')
            if previous is not None and not math.isclose(timestamp - previous, 1 / fps, rel_tol=0, abs_tol=1e-6):
                raise ValueError('样本时间不连续，不能按固定采样频率导出。')
            if frame_index == 0 and not math.isclose(timestamp, 0, rel_tol=0, abs_tol=1e-9):
                raise ValueError('每条示范的相对时间必须从零开始。')
            monotonic = row.get('monotonic_ns')
            if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < 0:
                raise ValueError('样本缺少有效的单调时钟记录。')
            if first_monotonic is None:
                first_monotonic = monotonic
            if abs((monotonic - first_monotonic) / 1e9 - timestamp) > 1e-6:
                raise ValueError('样本相对时间与单调时钟不一致。')
            previous = timestamp
            for key in ('observation.state', 'action'):
                values = np.asarray(row.get(key), dtype=np.float32)
                if values.shape != (8,) or not np.isfinite(values).all() or not 0 <= values[7] <= 1:
                    raise ValueError('关节或夹爪样本格式错误。')
            for key in IMAGE_KEYS:
                relative = row.get(key)
                path = _safe_file(episode_root, relative)
                if (key, relative) in checked_images:
                    continue
                picture_bytes = _verified_bytes(path, manifest.get('files_sha256', {}).get(relative))
                with Image.open(io.BytesIO(picture_bytes)) as picture:
                    shape = (picture.height, picture.width, 3)
                    if key in shapes and shapes[key] != shape:
                        raise ValueError('同一路相机的分辨率在示范间发生了变化，请分开导出。')
                    shapes[key] = shape
                    picture.verify()
                checked_images.add((key, relative))
        counts[entry['split']] += 1
        loaded.append((entry, manifest, episode_root))
        _progress(progress, stage='validate', completed=index, total=len(entries), message=f'已校验 {index}/{len(entries)} 条示范')
    if not all(counts.values()):
        raise ValueError('训练、验证、测试三个集合都必须包含完整示范。')
    expected_splits = {name: [e['id'] for e in entries if e['split'] == name] for name in counts}
    if bundle.get('splits') != expected_splits:
        raise ValueError('数据包中的划分清单不一致。')
    if bundle.get('counts') != {**counts, 'total': len(entries)}:
        raise ValueError('数据包中的示范计数不一致。')

    if dataset_factory is None:
        dataset_factory, software = _load_dataset_factory()
    else:
        software = {'revision': None, 'revision_verified': False, 'factory': 'injected'}
    features = {
        'observation.state': {'dtype': 'float32', 'shape': (8,), 'names': contract['state_names']},
        'action': {'dtype': 'float32', 'shape': (8,), 'names': contract['action_names']},
    }
    for key, shape in shapes.items():
        features[key] = {'dtype': 'video', 'shape': shape, 'names': ['height', 'width', 'channels']}
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve()
    prefix = f'.{output.name}.converting-'
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    complete = False
    converted = 0
    try:
        for split in ('train', 'val', 'test'):
            dataset = dataset_factory(repo_id=f'local/jarvis_{split}', root=staging / split,
                                      fps=fps, robot_type='kinova_gen3', features=copy.deepcopy(features),
                                      use_videos=True, image_writer_threads=2)
            try:
                for entry, manifest, episode_root in loaded:
                    if entry['split'] != split:
                        continue
                    rows = _load_samples(episode_root, manifest)
                    for row in rows:
                        frame = {'observation.state': np.asarray(row['observation.state'], dtype=np.float32),
                                 'action': np.asarray(row['action'], dtype=np.float32),
                                 'task': manifest['task']}
                        for key in IMAGE_KEYS:
                            path = _safe_file(episode_root, row[key])
                            picture_bytes = _verified_bytes(path, manifest['files_sha256'].get(row[key]))
                            with Image.open(io.BytesIO(picture_bytes)) as picture:
                                frame[key] = np.array(picture.convert('RGB'), dtype=np.uint8)
                        dataset.add_frame(frame)
                    dataset.save_episode()
                    converted += 1
                    _progress(progress, stage='convert', completed=converted, total=len(entries),
                              message=f'已转换 {converted}/{len(entries)} 条示范')
            finally:
                dataset.finalize()
        _check_hash(bundle_path, source_bundle_hash)
        result = {'schema_version': 1, 'complete': True, 'format': 'lerobot',
                  'counts': {**counts, 'total': len(entries)}, 'feature_contract': contract,
                  'source_bundle_sha256': source_bundle_hash,
                  'splits': expected_splits, 'software': software,
                  'statistics_note': 'Each split is written independently; train statistics are computed from train episodes only. Load saved training processors during evaluation.'}
        with (staging / 'export.json').open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        if output.exists() or output.is_symlink():
            raise FileExistsError('输出目录在转换期间已被创建，未覆盖。')
        staging.rename(output)
        complete = True
        return {**result, 'output_dir': str(output.resolve())}
    finally:
        if not complete and staging.exists():
            resolved = staging.resolve()
            if resolved.parent != parent or not resolved.name.startswith(prefix):
                raise RuntimeError('临时目录不在预期位置，已保留以供检查。')
            shutil.rmtree(resolved)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='将已检查示范数据包转换成 LeRobot 数据集；无需模型或 GPU。')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)

    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    try:
        result = convert_bundle(args.bundle, args.output, progress=emit)
    except Exception as error:
        if isinstance(error, ImportError):
            message = '数据转换 Python 缺少必要依赖，请按说明安装固定版本 LeRobot、NumPy 和 Pillow。'
        elif isinstance(error, (ValueError, OSError, RuntimeError)):
            message = str(error)
        else:
            message = f'数据转换未完成（{type(error).__name__}），请检查数据包与转换环境。'
        emit({'type': 'error', 'message': message})
        return 1
    emit({'type': 'result', **result})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
