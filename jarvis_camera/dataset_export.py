"""Export reviewed demonstration episodes without importing a model or LeRobot.

Only reviewed derived samples and their referenced pictures enter the bundle.
Raw connection settings, credentials, controller logs and machine paths do not.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable


CONTRACT_KEYS = (
    'action_schema', 'fps', 'state_names', 'action_names', 'units',
    'joint_representation', 'gripper_label_source', 'alignment',
)
CHECK_KEYS = ('alignment', 'position_targets', 'stable_end')
IMAGE_KEYS = ('observation.images.top', 'observation.images.wrist')
LEROBOT_REVISION = '9e5ee9859480e425a97a6836bb3fee479539beb5'
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


def load_prepared_episode(episode_dir, *, require_review=True, include_source_hashes=False):
    """Lazy import keeps the export module independent of Tk and robot SDKs."""
    from .preparation import load_prepared_episode as load
    return load(episode_dir, require_review=require_review,
                include_source_hashes=include_source_hashes)


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('图片路径格式错误，请重新整理该条示范。')
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ('..', '.') for part in path.parts):
        raise ValueError('图片路径不能离开示范目录。')
    resolved = (root / Path(*path.parts)).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError('图片缺失或路径超出了示范目录。')
    return resolved


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _copy_hashed(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            outgoing.write(block)
            digest.update(block)
    return digest.hexdigest()


def _json_write(path: Path, value: dict) -> None:
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def _contract(manifest: dict) -> dict:
    missing = [key for key in CONTRACT_KEYS if key not in manifest]
    if missing:
        raise ValueError('整理结果缺少动作定义，请用当前版本重新整理。')
    contract = {key: manifest[key] for key in CONTRACT_KEYS}
    if contract['joint_representation'] == 'centered_degrees_to_radians_v1':
        reference = manifest.get('joint_reference_degrees')
        if (not isinstance(reference, list) or reference != CENTERED_REFERENCE_DEGREES
                or any(type(value) is not int for value in reference)):
            raise ValueError('关节参考不符合此版本的角度定义，请重新整理。')
        contract['joint_reference_degrees'] = reference
    elif 'joint_reference_degrees' in manifest:
        raise ValueError('关节参考与角度定义不一致，请重新整理。')
    fps = contract['fps']
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 50 or int(fps) != fps:
        raise ValueError('当前导出要求 1–50 之间的整数采样频率。')
    contract['fps'] = int(fps)
    if contract['action_schema'] != 'next_measured_position_v1':
        raise ValueError('此版本不支持该动作标签格式。')
    if contract['units'] != {'joints': 'radian', 'gripper': 'normalized_0_1'}:
        raise ValueError('关节或夹爪单位不符合此版本的导出定义。')
    if (contract['joint_representation'] not in SUPPORTED_JOINT_REPRESENTATIONS
            or any(contract[key] != value for key, value in POSITION_CONTRACT.items()
                   if key != 'joint_representation')):
        raise ValueError('关节顺序、夹爪来源或时间对齐定义不符合此版本，请重新整理。')
    # Reject non-JSON values before any output directory is created.
    json.dumps(contract, allow_nan=False)
    return contract


def _notify(progress, **fields):
    if progress is not None:
        progress({'type': 'progress', **fields})


def _remove_own_staging(staging: Path, parent: Path, prefix: str) -> None:
    resolved = staging.resolve()
    if resolved.parent != parent.resolve() or not resolved.name.startswith(prefix):
        raise RuntimeError('临时目录不在预期位置，已保留以供检查。')
    shutil.rmtree(resolved)


def export_bundle(episode_dirs, output_dir, *, seed=42,
                  progress: Callable[[dict], None] | None = None) -> dict:
    """Create a portable, reviewed train/val/test bundle; never overwrite output.

    Every supplied episode must pass review. Callers may select reviewed episodes
    in the UI, but this function never silently skips invalid ones. Source hashes
    are checked again after copying to detect edits during export.
    """
    directories = [Path(item).expanduser().resolve() for item in episode_dirs]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError('数据划分种子必须是整数。')
    if len(directories) < 3:
        raise ValueError('至少需要 3 条已检查的成功示范，才能分别建立训练、验证和测试集。')
    if len(set(directories)) != len(directories):
        raise ValueError('同一条示范被重复选择，请移除重复项。')
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('导出位置已经存在，请选择一个新的文件夹名称。')
    if any(output.resolve().is_relative_to(ep) or ep.is_relative_to(output.resolve()) for ep in directories):
        raise ValueError('导出目录必须与原始示范目录分开。')

    # Keep each episode's verified snapshot on disk rather than in RAM. This
    # avoids re-reading every original image between validation and copying,
    # while retaining bounded memory even for hundreds of demonstrations.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.jarvis-verified-', dir=output.parent) as spool:
        return _export_scanned(directories, output, seed, progress, Path(spool))


def _export_scanned(directories, output, seed, progress, spool):

    items = []
    canonical = None
    fingerprints = set()
    for index, episode in enumerate(directories, 1):
        manifest, rows, source_hashes = load_prepared_episode(
            episode, require_review=True, include_source_hashes=True)
        if not manifest.get('technical_pass') or manifest.get('status') != 'ready_for_review':
            raise ValueError('存在未通过数据检查的示范，未导出。')
        contract = _contract(manifest)
        if canonical is None:
            canonical = contract
        elif canonical != contract:
            raise ValueError('所选示范的采样频率或动作定义不同，请分开导出。')
        fingerprint = manifest.get('source_fingerprint')
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError('示范缺少来源标识，请重新整理。')
        if fingerprint in fingerprints:
            raise ValueError('发现同一份原始示范的重复副本，不能分到不同数据集。')
        fingerprints.add(fingerprint)
        if not rows or len(rows) != manifest.get('num_samples'):
            raise ValueError('样本数量与整理结果不一致，请重新整理。')
        task = manifest.get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('示范缺少任务说明。')
        # Persist a digest-protected snapshot rather than keeping every
        # episode's arrays and source hashes in memory until copy time.
        snapshot = spool / f'{index:08d}.json'
        snapshot_data = json.dumps({'rows': rows, 'source_hashes': source_hashes},
                                   ensure_ascii=False, allow_nan=False,
                                   separators=(',', ':')).encode('utf-8')
        with snapshot.open('xb') as stream:
            stream.write(snapshot_data)
        items.append({'source': episode, 'manifest': manifest, 'snapshot': snapshot,
                      'snapshot_sha256': hashlib.sha256(snapshot_data).hexdigest()})
        _notify(progress, stage='validate', completed=index, total=len(directories),
                message=f'已检查 {index}/{len(directories)} 条示范')

    # The same episode content always starts from the same order, independent of
    # selection order, names, or where the user's recordings were moved.
    items.sort(key=lambda item: item['manifest']['source_fingerprint'])
    random.Random(seed).shuffle(items)
    count = len(items)
    val_count = max(1, round(count * 0.15))
    test_count = max(1, round(count * 0.15))
    train_count = count - val_count - test_count
    if train_count < 1:
        raise ValueError('示范数量不足，无法建立三个非空数据集。')
    assignments = ['train'] * train_count + ['val'] * val_count + ['test'] * test_count
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve()
    prefix = f'.{output.name}.preparing-'
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    complete = False
    try:
        splits = {name: [] for name in ('train', 'val', 'test')}
        entries = []
        image_shapes = {}
        for index, (item, split) in enumerate(zip(items, assignments), 1):
            episode_id = f'episode_{index:06d}'
            relative_root = f'episodes/{episode_id}'
            destination = staging / relative_root
            destination.mkdir(parents=True)
            source = item['source']
            manifest = item['manifest']
            snapshot_data = item['snapshot'].read_bytes()
            if hashlib.sha256(snapshot_data).hexdigest() != item['snapshot_sha256']:
                raise ValueError('导出期间临时整理数据发生变化，已取消。')
            snapshot = json.loads(snapshot_data)
            rows, source_hashes = snapshot['rows'], snapshot['source_hashes']
            file_map = {}
            hashes = {}
            with (destination / 'samples.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
                for row in rows:
                    sample = {
                        'timestamp': row['timestamp'],
                        'monotonic_ns': row['monotonic_ns'],
                        'observation.state': row['observation.state'],
                        'action': row['action'],
                    }
                    for key in IMAGE_KEYS:
                        original = row[key]
                        source_file = _safe_file(source, original)
                        image_identity = (key, original)
                        if image_identity not in file_map:
                            camera = key.rsplit('.', 1)[-1]
                            local = f'images/{camera}/{len(file_map):08d}.jpg'
                            copied_hash = _copy_hashed(source_file, destination / local)
                            original_key = source_file.relative_to(source).as_posix()
                            if copied_hash != source_hashes.get(original_key):
                                raise ValueError('导出期间原始图片发生变化，已取消；请重新检查后导出。')
                            hashes[local] = copied_hash
                            # Validate one decoded image at a time, never keeping
                            # an episode's images in memory. Pillow is already a
                            # desktop dependency; LeRobot and torch are not.
                            from PIL import Image
                            try:
                                with Image.open(destination / local) as picture:
                                    if picture.format != 'JPEG':
                                        raise ValueError('相机图片不是 JPEG，请检查原始记录。')
                                    shape = (picture.height, picture.width, 3)
                                    picture.load()
                            except OSError as error:
                                raise ValueError('相机图片损坏或无法读取，请重新采集该条示范。') from error
                            if key in image_shapes and image_shapes[key] != shape:
                                raise ValueError('同一路相机的分辨率发生了变化，请分开导出。')
                            image_shapes[key] = shape
                            file_map[image_identity] = local
                        sample[key] = file_map[image_identity]
                    # Retain only numerical provenance used by the reviewer.
                    for key in ('source_ages_ms', 'source_sequences'):
                        if key in row:
                            sample[key] = {name: value for name, value in row[key].items()
                                           if name in ('robot', 'action_robot', 'top', 'wrist')
                                           and not isinstance(value, bool)
                                           and isinstance(value, (int, float)) and math.isfinite(value)}
                    if isinstance(row.get('action_monotonic_ns'), int):
                        sample['action_monotonic_ns'] = row['action_monotonic_ns']
                    stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + '\n')
            hashes['samples.jsonl'] = _hash_file(destination / 'samples.jsonl')
            episode_manifest = {
                'schema_version': 1, 'task': manifest['task'],
                'num_samples': len(rows), 'feature_contract': canonical,
                'source_fingerprint': manifest['source_fingerprint'],
                'prepared_signature': manifest.get('prepared_signature'),
                'review': {'checks': {key: True for key in CHECK_KEYS},
                           'basis': 'human_review_validated_by_console'},
                'files_sha256': hashes,
            }
            _json_write(destination / 'episode.json', episode_manifest)
            entries.append({'id': episode_id, 'path': relative_root, 'split': split,
                            'num_samples': len(rows),
                            'manifest_sha256': _hash_file(destination / 'episode.json')})
            splits[split].append(episode_id)
            _notify(progress, stage='copy', completed=index, total=count,
                    message=f'已打包 {index}/{count} 条示范')

        for item in items:
            current, _ = load_prepared_episode(item['source'], require_review=True)
            if current['source_fingerprint'] != item['manifest']['source_fingerprint'] or current.get('prepared_signature') != item['manifest'].get('prepared_signature'):
                raise ValueError('导出期间示范发生变化，已取消；请重新检查后导出。')
        worker = Path(__file__).resolve().with_name('lerobot_export_worker.py')
        if not worker.is_file():
            raise FileNotFoundError('转换组件缺失，请使用完整的软件目录。')
        shutil.copyfile(worker, staging / 'convert_to_lerobot.py')
        bundle = {
            'schema_version': 1, 'format': 'jarvis_reviewed_demonstrations',
            'complete': True, 'created_utc': datetime.now(timezone.utc).isoformat(),
            'split_seed': seed, 'feature_contract': canonical,
            'counts': {'train': train_count, 'val': val_count, 'test': test_count, 'total': count},
            'splits': splits, 'episodes': entries,
            'lerobot_revision': LEROBOT_REVISION,
            'note': 'Reviewed position-trajectory targets, including continuous measured gripper position; not original controller commands or a real-robot safety certification.',
        }
        _json_write(staging / 'bundle.json', bundle)
        if output.exists() or output.is_symlink():
            raise FileExistsError('导出位置在处理期间已被创建，未覆盖。')
        staging.rename(output)
        complete = True
        return {**bundle, 'output_dir': str(output.resolve())}
    finally:
        if not complete and staging.exists():
            _remove_own_staging(staging, parent, prefix)
