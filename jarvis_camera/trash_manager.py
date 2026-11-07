"""Move explicitly selected data into a same-volume, recoverable holding area.

Nothing in this module permanently deletes data or contacts a robot. The holding
area is not the Windows Recycle Bin: it is a normal directory beside the data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from .settings import app_directory


TRASH_DIR = '.jarvis_trash'
MARKER = 'jarvis-recovery-area.json'
RECEIPT = 'receipt.json'
PAYLOAD = 'payload'
MAGIC = 'jarvis_recovery_area_v1'
KINDS = ('episode', 'lerobot')


def _redirect(path: Path) -> bool:
    """Refuse symbolic links, junctions, and other Windows reparse points."""
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(os.stat_result, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _safe_path(value, *, must_exist=True) -> Path:
    path = Path(value).expanduser().absolute()
    for ancestor in (path, *path.parents):
        if _redirect(ancestor):
            raise ValueError('路径包含符号链接或目录联接，不能安全移动。')
    if must_exist and not path.is_dir():
        raise ValueError('所选目录不存在或不是文件夹。')
    return path.resolve(strict=must_exist)


def _json_file(path: Path) -> dict:
    if _redirect(path) or not path.is_file():
        raise ValueError('数据标识文件缺失或不是普通文件。')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError('数据标识文件无法读取。') from error
    if not isinstance(value, dict):
        raise ValueError('数据标识文件格式不正确。')
    return value


def _protected(target: Path, protected_paths) -> None:
    # A dataset is allowed beside or inside an app folder, but the app folder,
    # environment, workspace, and their ancestors can never be moved.
    protected = [app_directory(), Path.cwd(), *protected_paths]
    for value in protected:
        if not value:
            continue
        path = _safe_path(value, must_exist=False)
        if path == target or path.is_relative_to(target):
            raise ValueError('不能移动程序、环境或工作目录及其上级目录。')


def _check_episode(target: Path, root: Path) -> None:
    if target.parent != root or not target.name.startswith('episode_'):
        raise ValueError('只能移动当前录制目录中的单条示范文件夹。')
    meta = _json_file(target / 'metadata.json')
    if meta.get('schema_version') != 1 or meta.get('recording_status') not in ('complete', 'incomplete'):
        raise ValueError('这条示范未完整停止或状态未知，不能移动。')
    warnings = meta.get('warnings', [])
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError('录制停止状态无法确认，请先关闭其他程序并检查。')
    if any(item == 'stop_timeout' or item.endswith('_stop_failed') for item in warnings):
        raise ValueError('录制停止超时或失败，请先关闭其他程序并检查。')
    robot = meta.get('robot_state')
    if robot in ('connecting', 'recording', 'starting', 'stopping'):
        raise ValueError('机器人记录可能仍在运行，请先关闭其他程序并检查。')
    if meta.get('recording_status') == 'incomplete' and robot not in ('stopped', 'error', 'idle'):
        raise ValueError('不完整录制的机器人停止状态未知，请先关闭其他程序并检查。')
    cameras = meta.get('camera_recording', {})
    if not isinstance(cameras, dict):
        raise ValueError('相机录制停止状态未知，请先关闭其他程序并检查。')
    for camera in cameras.values():
        if not isinstance(camera, dict):
            raise ValueError('相机录制停止状态未知，请先关闭其他程序并检查。')
        if camera.get('state') in ('connecting', 'starting', 'recording', 'stopping'):
            raise ValueError('相机记录可能仍在运行，请先关闭其他程序并检查。')


def _check_dataset(target: Path, root: Path) -> None:
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('数据集目录必须与原始录制目录分开。')
    meta = _json_file(target / 'export.json')
    if meta.get('format') != 'lerobot' or meta.get('complete') is not True:
        raise ValueError('只能移动完整的 JARVIS LeRobot 数据集。')
    for split in ('train', 'val', 'test'):
        split_dir = target / split
        _safe_path(split_dir)
        _safe_path(split_dir / 'meta')
        info = _json_file(split_dir / 'meta' / 'info.json')
        if not isinstance(info.get('features'), dict) or type(info.get('total_episodes')) is not int:
            raise ValueError('数据集目录结构不完整。')


def _make_area(parent: Path) -> Path:
    area = parent / TRASH_DIR
    created = False
    if area.exists() or area.is_symlink():
        if _redirect(area) or not area.is_dir():
            raise ValueError('可恢复区不是普通文件夹。')
    else:
        area.mkdir()
        created = True
    marker = area / MARKER
    if created:
        with marker.open('x', encoding='utf-8') as stream:
            json.dump({'format': MAGIC}, stream)
    elif _json_file(marker).get('format') != MAGIC:
        raise ValueError('已有同名目录不是 JARVIS 可恢复区，不会写入。')
    return area


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat(follow_symlinks=False)
    return stat.st_dev, stat.st_ino, stat.st_ctime_ns


def _move(target: Path, kind: str, recordings_root: Path, protected_paths) -> dict:
    identity = _identity(target)
    area = _make_area(target.parent)
    token = uuid.uuid4().hex
    item = area / token
    item.mkdir()
    receipt = {'schema_version': 1, 'format': MAGIC, 'kind': kind,
               'original_name': target.name, 'token': token,
               'created_at': datetime.now(timezone.utc).isoformat()}
    with (item / RECEIPT).open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    payload = item / PAYLOAD
    if payload.exists() or payload.is_symlink():
        raise FileExistsError('恢复目录已存在，不会覆盖。')
    # Target and area share the same existing parent/volume; rename moves the
    # directory without traversing, copying or deleting its contents.
    if _safe_path(target) != target or _redirect(area) or _redirect(item):
        raise ValueError('移动之前路径发生变化，操作已取消。')
    if _identity(target) != identity or _safe_path(recordings_root) != recordings_root:
        raise ValueError('所选目录在移动前发生变化，操作已取消。')
    _protected(target, protected_paths)
    if kind == 'episode':
        _check_episode(target, recordings_root)
    else:
        _check_dataset(target, recordings_root)
    os.rename(target, payload)
    return {'kind': kind, 'original_path': str(target), 'recovery_path': str(payload),
            'trash_item': str(item), 'message': '已移动到可恢复区，未永久删除。'}


def move_episode_to_trash(episode_dir, recordings_root, *, protected_paths=()) -> dict:
    root = _safe_path(recordings_root)
    target = _safe_path(episode_dir)
    _protected(target, protected_paths)
    _check_episode(target, root)
    return _move(target, 'episode', root, protected_paths)


def move_dataset_to_trash(dataset_dir, recordings_root, *, protected_paths=()) -> dict:
    root = _safe_path(recordings_root)
    target = _safe_path(dataset_dir)
    _protected(target, protected_paths)
    _check_dataset(target, root)
    return _move(target, 'lerobot', root, protected_paths)


def restore_from_trash(trash_item, *, protected_paths=()) -> dict:
    item = _safe_path(trash_item)
    if item.name == PAYLOAD:
        item = item.parent
    area = item.parent
    if area.name != TRASH_DIR or _json_file(area / MARKER).get('format') != MAGIC:
        raise ValueError('请选择 JARVIS 可恢复区中的记录目录。')
    receipt = _json_file(item / RECEIPT)
    name = receipt.get('original_name')
    if (receipt.get('format') != MAGIC or receipt.get('schema_version') != 1
            or receipt.get('kind') not in KINDS or receipt.get('token') != item.name
            or len(item.name) != 32 or any(c not in '0123456789abcdef' for c in item.name)
            or not isinstance(name, str) or not name or name in ('.', '..')
            or Path(name).name != name or '/' in name or '\\' in name or ':' in name):
        raise ValueError('恢复记录无效，不能推断原位置。')
    payload = _safe_path(item / PAYLOAD)
    identity = _identity(payload)
    original = area.parent / name
    _protected(original, protected_paths)
    if original.exists() or original.is_symlink():
        raise FileExistsError('原位置已有同名文件夹，不会覆盖。')
    if _redirect(area.parent) or _redirect(area) or _redirect(item) or _redirect(payload):
        raise ValueError('恢复路径发生变化，操作已取消。')
    if _safe_path(payload) != payload or _identity(payload) != identity:
        raise ValueError('恢复内容在操作前发生变化，操作已取消。')
    os.rename(payload, original)
    return {'kind': receipt['kind'], 'original_path': str(original),
            'recovery_path': str(payload), 'trash_item': str(item), 'message': '已恢复到原位置。'}
