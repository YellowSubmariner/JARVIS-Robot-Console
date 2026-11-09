"""Immutable, incremental LeRobot export for reviewed, unchanged episodes.

Run in the separately installed LeRobot Python. Prior exports are copied before
appending, never modified in place. Old-format exports lack source provenance and
cannot be safely imported as an incremental baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile

try:
    from . import lerobot_export_worker as worker
except ImportError:
    import lerobot_export_worker as worker

MANIFEST = 'jarvis_incremental_manifest.json'
FORMAT = 'jarvis_incremental_lerobot_v1'
SPLITS = ('train', 'val', 'test')


def _emit(progress, **kwargs):
    if progress is not None:
        progress({'type': 'progress', **kwargs})


def _json(path):
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('数据清单格式错误。')
    return value


def _digest_file(path):
    return worker._sha256(path)


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _manifest_checksum(value):
    payload = {key: content for key, content in value.items() if key != 'manifest_sha256'}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _redirect(path):
    """Reject Windows junctions as well as POSIX/Windows symbolic links."""
    junction = getattr(path, 'is_junction', None)
    return path.is_symlink() or bool(junction and junction())


def _inventory(root):
    """Hash every regular exported file, rejecting links and special files."""
    found = {}
    if _redirect(root):
        raise ValueError('此前数据集是链接或目录联接，不能安全复用。')
    directories = [root]
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            # Test before descending: a junction may report itself as a dir.
            if _redirect(path):
                raise ValueError('此前数据集含符号链接或目录联接，不能安全复用。')
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
            elif entry.is_file(follow_symlinks=False):
                name = path.relative_to(root).as_posix()
                if name != MANIFEST:
                    found[name] = _digest_file(path)
            else:
                raise ValueError('此前数据集含无法校验的文件。')
    return found


def _bundle(bundle_root, progress=None, *, verify_images=True):
    """Verify source and review; the first full conversion verifies images itself."""
    import numpy as np
    from PIL import Image
    bundle_file = worker._safe_file(bundle_root, 'bundle.json')
    bundle_hash = _digest_file(bundle_file)
    bundle = _json(bundle_file)
    if (bundle.get('schema_version') != 1 or bundle.get('format') != 'jarvis_reviewed_demonstrations'
            or bundle.get('complete') is not True or bundle.get('lerobot_revision') != worker.LEROBOT_REVISION):
        raise ValueError('数据包不完整或版本不兼容。')
    contract = bundle.get('feature_contract') or {}
    fps = contract.get('fps')
    if (type(fps) is not int or not 1 <= fps <= 50
            or contract.get('action_schema') != 'next_measured_position_v1'
            or contract.get('units') != {'joints': 'radian', 'gripper': 'normalized_0_1'}
            or contract.get('joint_representation') not in worker.SUPPORTED_JOINT_REPRESENTATIONS
            or not worker._valid_joint_reference(contract)
            or any(contract.get(k) != v for k, v in worker.POSITION_CONTRACT.items()
                   if k != 'joint_representation')):
        raise ValueError('数据包的动作或采样定义不受支持。')
    entries = bundle.get('episodes')
    if not isinstance(entries, list) or len(entries) < 3:
        raise ValueError('至少需要三条已检查的示范。')
    items, ids, fingerprints, shapes = [], set(), set(), {}
    counts = {s: 0 for s in SPLITS}
    for index, entry in enumerate(entries, 1):
        if (not isinstance(entry, dict) or entry.get('split') not in counts
                or not isinstance(entry.get('id'), str) or entry['id'] in ids):
            raise ValueError('数据包的示范或划分无效。')
        ids.add(entry['id'])
        path = worker._safe_file(bundle_root, entry.get('path', '') + '/episode.json')
        episode_root = path.parent
        manifest = json.loads(worker._verified_bytes(path, entry.get('manifest_sha256')))
        if manifest.get('feature_contract') != contract:
            raise ValueError('示范之间的动作定义不一致。')
        checks = (manifest.get('review') or {}).get('checks') or {}
        if any(checks.get(key) is not True for key in worker.CHECK_KEYS):
            raise ValueError('示范尚未完成检查。')
        fingerprint, signature = manifest.get('source_fingerprint'), manifest.get('prepared_signature')
        if not _digest(fingerprint) or not _digest(signature) or fingerprint in fingerprints:
            raise ValueError('示范来源签名缺失或重复。')
        fingerprints.add(fingerprint)
        task = manifest.get('task')
        if not isinstance(task, str) or not task.strip():
            raise ValueError('示范缺少任务文字。')
        rows = worker._load_samples(episode_root, manifest)
        if len(rows) != entry.get('num_samples'):
            raise ValueError('示范样本数量不一致。')
        start_ns = None
        checked_images = set()
        for i, row in enumerate(rows):
            ts, mono = row.get('timestamp'), row.get('monotonic_ns')
            if (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts)
                    or not math.isclose(ts, i / fps, abs_tol=1e-6, rel_tol=0)
                    or type(mono) is not int or mono < 0):
                raise ValueError('样本时间或时钟不连续。')
            if start_ns is None:
                start_ns = mono
            if abs((mono - start_ns) / 1e9 - ts) > 1e-6:
                raise ValueError('样本时间与记录时钟不一致。')
            for key in ('observation.state', 'action'):
                v = np.asarray(row.get(key), dtype=np.float32)
                if v.shape != (8,) or not np.isfinite(v).all() or not 0 <= v[7] <= 1:
                    raise ValueError('关节或夹爪数据格式错误。')
            for key in worker.IMAGE_KEYS:
                if not verify_images:
                    continue  # convert_bundle will hash and verify every image.
                relative = row.get(key)
                if (key, relative) in checked_images:
                    continue
                data = worker._verified_bytes(worker._safe_file(episode_root, relative),
                                              manifest.get('files_sha256', {}).get(relative))
                with Image.open(io.BytesIO(data)) as image:
                    shape = [image.height, image.width, 3]
                    if key in shapes and shapes[key] != shape:
                        raise ValueError('相机分辨率不一致，请分开导出。')
                    shapes[key] = shape
                    image.verify()
                checked_images.add((key, relative))
        items.append({'entry': entry, 'manifest': manifest, 'root': episode_root,
                      'source_fingerprint': fingerprint, 'prepared_signature': signature,
                      'split': entry['split'], 'num_samples': len(rows)})
        counts[entry['split']] += 1
        _emit(progress, stage='validate', completed=index, total=len(entries), message=f'已校验 {index}/{len(entries)} 条示范')
    expected = {s: [e['id'] for e in entries if e['split'] == s] for s in SPLITS}
    if bundle.get('splits') != expected or bundle.get('counts') != {**counts, 'total': len(entries)} or not all(counts.values()):
        raise ValueError('数据包划分或计数不一致。')
    return bundle, bundle_hash, items, shapes


def _record(item):
    return {key: item[key] for key in ('source_fingerprint', 'prepared_signature', 'split', 'episode_index', 'num_samples')}


def _check_prior(prior, bundle, items, shapes):
    path = prior / MANIFEST
    if not path.is_file():
        raise ValueError('旧导出没有可验证的逐条来源清单，不能安全复用；请先重新生成一次。')
    meta = _json(path)
    if (meta.get('schema_version') != 1 or meta.get('format') != FORMAT or meta.get('complete') is not True
            or meta.get('revision') != worker.LEROBOT_REVISION or meta.get('feature_contract') != bundle['feature_contract']
            or meta.get('image_shapes') != shapes or meta.get('manifest_sha256') != _manifest_checksum(meta)):
        raise ValueError('旧数据集版本、分辨率或动作定义不同，不能复用。')
    inventory = meta.get('files_sha256')
    if not isinstance(inventory, dict) or not inventory or _inventory(prior) != inventory:
        raise ValueError('旧数据集文件缺失或已改变，不能复用；请重新生成。')
    records = meta.get('episodes')
    if not isinstance(records, list) or not records:
        raise ValueError('旧数据集没有有效的示范来源清单。')
    current = {i['source_fingerprint']: i for i in items}
    used = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('旧数据集来源清单损坏。')
        fp = record.get('source_fingerprint')
        item = current.get(fp)
        if (not _digest(fp) or fp in used or item is None or record.get('split') not in SPLITS
                or type(record.get('episode_index')) is not int or record['episode_index'] < 0
                or record.get('prepared_signature') != item['prepared_signature']
                or record.get('num_samples') != item['num_samples']):
            raise ValueError('旧数据集有示范被移除、修改或重新审核，不能继续复用；请重新生成。')
        used.add(fp)
        item['split'] = record['split']
        item['episode_index'] = record['episode_index']
    for split in SPLITS:
        indices = sorted(record['episode_index'] for record in records if record['split'] == split)
        if indices != list(range(len(indices))):
            raise ValueError('旧数据集的示范索引不连续或重复，不能复用。')
    return meta, used


def _allocate(items, used):
    """Keep existing splits fixed, assign additions deterministically."""
    counts = {s: sum(x['split'] == s for x in items if x['source_fingerprint'] in used) for s in SPLITS}
    for item in sorted((x for x in items if x['source_fingerprint'] not in used), key=lambda x: x['source_fingerprint']):
        total = sum(counts.values()) + 1
        desired = {'train': total * .70, 'val': total * .15, 'test': total * .15}
        split = max(SPLITS, key=lambda s: (desired[s] - counts[s], -SPLITS.index(s)))
        item['split'] = split
        counts[split] += 1
    return counts


def _assign_indices(items, used):
    """Match LeRobot write order; preserve every already-exported index."""
    next_index = {s: sum(x['split'] == s for x in items if x['source_fingerprint'] in used) for s in SPLITS}
    for item in items:
        if item['source_fingerprint'] not in used:
            item['episode_index'] = next_index[item['split']]
            next_index[item['split']] += 1


def _append(staging, additions, progress):
    import numpy as np
    from PIL import Image
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    converted = 0
    for split in SPLITS:
        selected = [x for x in additions if x['split'] == split]
        if not selected:
            continue
        dataset = LeRobotDataset.resume(repo_id=f'local/jarvis_{split}', root=staging / split,
                                        video_backend='pyav', image_writer_threads=2)
        try:
            for item in selected:
                manifest, root = item['manifest'], item['root']
                rows = worker._load_samples(root, manifest)
                for row in rows:
                    frame = {'observation.state': np.asarray(row['observation.state'], dtype=np.float32),
                             'action': np.asarray(row['action'], dtype=np.float32), 'task': manifest['task']}
                    for key in worker.IMAGE_KEYS:
                        data = worker._verified_bytes(worker._safe_file(root, row[key]), manifest['files_sha256'].get(row[key]))
                        with Image.open(io.BytesIO(data)) as image:
                            frame[key] = np.array(image.convert('RGB'), dtype=np.uint8)
                    dataset.add_frame(frame)
                dataset.save_episode()
                converted += 1
                _emit(progress, stage='convert', completed=converted, total=len(additions),
                      message=f'已转换新增示范 {converted}/{len(additions)} 条')
        finally:
            dataset.finalize()


def _verify_counts(root, items):
    for split in SPLITS:
        info = _json(root / split / 'meta' / 'info.json')
        expected = [x for x in items if x['split'] == split]
        if sorted(x['episode_index'] for x in expected) != list(range(len(expected))):
            raise ValueError('导出后示范索引不连续或重复。')
        frame_count = sum(x['num_samples'] for x in expected)
        if info.get('total_episodes') != len(expected) or info.get('total_frames') != frame_count:
            raise ValueError('导出后数据集的示范或帧数校验失败。')
        stats = _json(root / split / 'meta' / 'stats.json')
        for key in ('observation.state', 'action'):
            feature = stats.get(key) or {}
            if feature.get('count') != [frame_count]:
                raise ValueError('导出后状态或动作统计数量不一致。')
            for quantile in ('q01', 'q99'):
                values = feature.get(quantile)
                if (not isinstance(values, list) or len(values) != 8
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
                    raise ValueError('导出后状态或动作统计不完整。')


def incremental_export(bundle_dir, output_dir, prior_dir=None, *, progress=None):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_DATASETS_OFFLINE'] = '1'
    bundle_root = Path(bundle_dir).expanduser().resolve(strict=True)
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('输出目录已存在，不会覆盖。')
    if output.resolve().is_relative_to(bundle_root) or bundle_root.is_relative_to(output.resolve()):
        raise ValueError('输出目录必须与输入数据包分开。')
    prior_input = Path(prior_dir).expanduser().absolute() if prior_dir else None
    if prior_input and _redirect(prior_input):
        raise ValueError('旧数据集目录不能是符号链接或目录联接。')
    prior = prior_input.resolve(strict=True) if prior_input else None
    if prior and (not prior.is_dir() or output.resolve().is_relative_to(prior)
                  or prior.is_relative_to(output.resolve()) or prior.is_relative_to(bundle_root)
                  or bundle_root.is_relative_to(prior)):
        raise ValueError('旧数据集、输入包与输出目录必须分开。')
    bundle, bundle_hash, items, shapes = _bundle(bundle_root, progress, verify_images=prior is not None)
    meta, used = _check_prior(prior, bundle, items, shapes) if prior else (None, set())
    counts = _allocate(items, used) if prior else {s: sum(x['split'] == s for x in items) for s in SPLITS}
    _assign_indices(items, used)
    additions = [x for x in items if x['source_fingerprint'] not in used]
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve()
    # Keep the temporary path short enough for Windows video/image writers.
    prefix = '.ji-'
    temporary = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    staging = temporary / 'result'
    try:
        if prior is None:
            result = worker.convert_bundle(bundle_root, staging, progress=progress)
            features = _json(staging / 'train' / 'meta' / 'info.json').get('features') or {}
            shapes = {key: features.get(key, {}).get('shape') for key in worker.IMAGE_KEYS}
            if any(not isinstance(shape, list) or len(shape) != 3 or shape[2] != 3 for shape in shapes.values()):
                raise ValueError('导出后的相机图像格式不正确。')
        else:
            _emit(progress, stage='reuse', completed=len(used), total=len(items), message=f'复用 {len(used)} 条已转换示范，正在校验复制…')
            shutil.copytree(prior, staging)
            if _inventory(staging) != meta['files_sha256']:
                raise ValueError('旧数据集在复制时发生变化，已取消。')
            (staging / MANIFEST).unlink()
            worker._load_dataset_factory()  # verify installed fixed revision before writing
            _append(staging, additions, progress)
            result = _json(staging / 'export.json')
        _verify_counts(staging, items)
        result.pop('output_dir', None)
        if _digest_file(worker._safe_file(bundle_root, 'bundle.json')) != bundle_hash:
            raise ValueError('转换期间数据包发生变化，已取消。')
        result.update({'schema_version': 1, 'complete': True, 'format': 'lerobot',
                       'counts': {**counts, 'total': len(items)},
                       'feature_contract': bundle['feature_contract'],
                       'source_bundle_sha256': bundle_hash,
                       'splits': {s: [i['entry']['id'] for i in sorted(items, key=lambda x: x['episode_index'])
                                     if i['split'] == s] for s in SPLITS},
                       'incremental': {'reused': len(used), 'converted': len(additions)}})
        (staging / 'export.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = {'schema_version': 1, 'format': FORMAT, 'complete': True,
                    'revision': worker.LEROBOT_REVISION, 'feature_contract': bundle['feature_contract'],
                    'image_shapes': shapes, 'episodes': [_record(item) for item in items],
                    'files_sha256': _inventory(staging)}
        manifest['manifest_sha256'] = _manifest_checksum(manifest)
        (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        if output.exists() or output.is_symlink():
            raise FileExistsError('输出目录在转换期间被创建，不会覆盖。')
        staging.rename(output)
        return {**result, 'output_dir': str(output.resolve())}
    finally:
        if temporary.exists():
            resolved = temporary.resolve()
            if resolved.parent != parent or not resolved.name.startswith(prefix):
                raise RuntimeError('临时目录位置异常，已保留。')
            shutil.rmtree(resolved)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='安全复用已有示范并增量导出 LeRobot。')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior', type=Path)
    args = parser.parse_args(argv)
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    try:
        result = incremental_export(args.bundle, args.output, args.prior, progress=emit)
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, OSError, RuntimeError)) else f'增量导出未完成（{type(error).__name__}）。'
        emit({'type': 'error', 'message': message})
        return 1
    emit({'type': 'result', **result})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
