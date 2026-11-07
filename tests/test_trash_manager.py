"""Recoverable file operations on disposable synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis_camera.trash_manager import (move_episode_to_trash, move_dataset_to_trash,
                                         restore_from_trash, TRASH_DIR, RECEIPT)
from jarvis_camera import trash_manager


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class RecoverableRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.recordings = self.root / 'recordings'
        self.recordings.mkdir()
        self.app = self.root / 'app'
        self.app.mkdir()
        self.mock_app = patch('jarvis_camera.trash_manager.app_directory', return_value=self.app)
        self.mock_app.start()
        self.addCleanup(self.mock_app.stop)

    def episode(self, name='episode_001', status='complete'):
        p = self.recordings / name
        p.mkdir()
        write_json(p / 'metadata.json', {'schema_version': 1, 'recording_status': status})
        (p / 'original.jpg').write_bytes(b'synthetic-image')
        return p

    def dataset(self, name='lerobot_test'):
        p = self.root / name
        p.mkdir()
        write_json(p / 'export.json', {'format': 'lerobot', 'complete': True})
        for split in ('train', 'val', 'test'):
            write_json(p / split / 'meta' / 'info.json', {'total_episodes': 1, 'features': {}})
        (p / 'example.mp4').write_bytes(b'synthetic-video')
        return p

    def test_episode_moves_to_recoverable_area_and_restores_exact_contents(self):
        p = self.episode()
        result = move_episode_to_trash(p, self.recordings)
        self.assertFalse(p.exists())
        self.assertEqual(Path(result['recovery_path']).joinpath('original.jpg').read_bytes(), b'synthetic-image')
        self.assertEqual(Path(result['trash_item']).parent, self.recordings / TRASH_DIR)
        restored = restore_from_trash(result['trash_item'])
        self.assertEqual(Path(restored['original_path']), p)
        self.assertEqual((p / 'original.jpg').read_bytes(), b'synthetic-image')
        self.assertTrue((Path(result['trash_item']) / RECEIPT).is_file())

    def test_dataset_moves_and_restores_with_payload_path_selection(self):
        p = self.dataset()
        result = move_dataset_to_trash(p, self.recordings)
        self.assertEqual(Path(result['recovery_path']).joinpath('example.mp4').read_bytes(), b'synthetic-video')
        restore_from_trash(result['recovery_path'])
        self.assertEqual((p / 'example.mp4').read_bytes(), b'synthetic-video')

    def test_cannot_move_active_or_unknown_recording(self):
        for status in ('starting', 'recording', 'stopping', None):
            with self.subTest(status=status):
                p = self.episode('episode_' + str(status), status)
                with self.assertRaisesRegex(ValueError, '状态未知|未完整停止'):
                    move_episode_to_trash(p, self.recordings)
                self.assertTrue((p / 'original.jpg').is_file())

    def test_incomplete_timeout_and_active_workers_are_refused(self):
        examples = (
            {'warnings': ['stop_timeout'], 'robot_state': 'stopped'},
            {'warnings': ['astra_stop_failed'], 'robot_state': 'stopped'},
            {'warnings': [], 'robot_state': 'recording'},
            {'warnings': [], 'robot_state': 'stopped', 'camera_recording': {'astra': {'state': 'recording'}}},
            {'warnings': [], 'robot_state': None},
        )
        for index, additions in enumerate(examples):
            with self.subTest(example=index):
                p = self.episode('episode_timeout_' + str(index), 'incomplete')
                meta = json.loads((p / 'metadata.json').read_text())
                meta.update(additions)
                write_json(p / 'metadata.json', meta)
                with self.assertRaisesRegex(ValueError, '停止|运行'):
                    move_episode_to_trash(p, self.recordings)
                self.assertTrue((p / 'original.jpg').is_file())

    def test_incomplete_but_stopped_episode_can_be_recovered(self):
        p = self.episode('episode_incomplete', 'incomplete')
        meta = json.loads((p / 'metadata.json').read_text())
        meta.update(robot_state='stopped', warnings=[],
                    camera_recording={'astra': {'state': 'complete'}, 'wrist': {'state': 'error'}})
        write_json(p / 'metadata.json', meta)
        result = move_episode_to_trash(p, self.recordings)
        restore_from_trash(result['trash_item'])
        self.assertTrue((p / 'original.jpg').is_file())

    def test_episode_must_be_direct_child_with_valid_metadata(self):
        p = self.episode()
        for candidate in (self.recordings, p / 'nested', self.root):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                move_episode_to_trash(candidate, self.recordings)
        other = self.root / 'episode_other'
        other.mkdir()
        write_json(other / 'metadata.json', {'schema_version': 1, 'recording_status': 'complete'})
        with self.assertRaises(ValueError):
            move_episode_to_trash(other, self.recordings)
        self.assertTrue(p.exists())

    def test_dataset_requires_complete_export_and_non_recording_directory(self):
        episode = self.episode()
        with self.assertRaises(ValueError):
            move_dataset_to_trash(episode, self.recordings)
        dataset = self.dataset()
        write_json(dataset / 'export.json', {'format': 'lerobot', 'complete': False})
        with self.assertRaises(ValueError):
            move_dataset_to_trash(dataset, self.recordings)
        self.assertTrue((dataset / 'example.mp4').is_file())

    def test_cannot_move_protected_paths_or_their_ancestors(self):
        dataset = self.dataset()
        protected_file = dataset / 'train' / 'something.exe'
        protected_file.write_bytes(b'fake')
        with self.assertRaisesRegex(ValueError, '程序|环境|工作'):
            move_dataset_to_trash(dataset, self.recordings, protected_paths=(protected_file,))
        with self.assertRaises(ValueError):
            move_dataset_to_trash(self.root, self.recordings)
        self.assertTrue(dataset.exists())

    def test_restore_never_overwrites_existing_directory(self):
        p = self.episode()
        result = move_episode_to_trash(p, self.recordings)
        p.mkdir()
        (p / 'new').write_bytes(b'new')
        with self.assertRaises(FileExistsError):
            restore_from_trash(result['trash_item'])
        self.assertEqual((p / 'new').read_bytes(), b'new')
        self.assertTrue(Path(result['recovery_path']).is_dir())

    def test_existing_unrecognized_recovery_area_is_not_modified(self):
        p = self.episode()
        area = self.recordings / TRASH_DIR
        area.mkdir()
        (area / 'user-file').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            move_episode_to_trash(p, self.recordings)
        self.assertEqual((area / 'user-file').read_bytes(), b'keep')
        self.assertTrue(p.exists())

    def test_junction_is_refused_without_following(self):
        p = self.episode()
        with patch.object(Path, 'is_junction', autospec=True, side_effect=lambda x: x == p):
            with self.assertRaisesRegex(ValueError, '目录联接'):
                move_episode_to_trash(p, self.recordings)
        self.assertTrue(p.exists())

    def test_forged_receipt_cannot_escape_parent(self):
        result = move_episode_to_trash(self.episode(), self.recordings)
        p = Path(result['trash_item']) / RECEIPT
        receipt = json.loads(p.read_text())
        receipt['original_name'] = '../outside'
        write_json(p, receipt)
        with self.assertRaisesRegex(ValueError, '恢复记录无效'):
            restore_from_trash(result['trash_item'])
        self.assertTrue(Path(result['recovery_path']).is_dir())

    def test_recording_that_restarts_before_move_is_left_untouched(self):
        p = self.episode()
        original_make_area = trash_manager._make_area
        def concurrent_start(parent):
            area = original_make_area(parent)
            write_json(p / 'metadata.json', {'schema_version': 1, 'recording_status': 'recording'})
            return area
        with patch.object(trash_manager, '_make_area', side_effect=concurrent_start):
            with self.assertRaisesRegex(ValueError, '未完整停止'):
                move_episode_to_trash(p, self.recordings)
        self.assertTrue((p / 'original.jpg').is_file())

    def test_failed_rename_keeps_original_and_other_recovery_entries(self):
        old = move_episode_to_trash(self.episode('episode_old'), self.recordings)
        p = self.episode('episode_new')
        with patch('jarvis_camera.trash_manager.os.rename', side_effect=PermissionError('busy')):
            with self.assertRaises(PermissionError):
                move_episode_to_trash(p, self.recordings)
        self.assertTrue((p / 'original.jpg').is_file())
        self.assertTrue(Path(old['recovery_path']).is_dir())

    def test_concurrent_replacement_is_refused_before_move(self):
        p = self.episode()
        with patch('jarvis_camera.trash_manager._identity', side_effect=[(1, 10, 5), (1, 11, 6)]), \
             patch('jarvis_camera.trash_manager.os.rename') as rename:
            with self.assertRaisesRegex(ValueError, '移动前发生变化'):
                move_episode_to_trash(p, self.recordings)
            rename.assert_not_called()
        self.assertTrue((p / 'original.jpg').is_file())

    def test_concurrent_replacement_is_refused_before_restore(self):
        p = self.episode()
        result = move_episode_to_trash(p, self.recordings)
        with patch('jarvis_camera.trash_manager._identity', side_effect=[(1, 10, 5), (1, 11, 6)]), \
             patch('jarvis_camera.trash_manager.os.rename') as rename:
            with self.assertRaisesRegex(ValueError, '恢复内容在操作前发生变化'):
                restore_from_trash(result['trash_item'])
            rename.assert_not_called()
        self.assertFalse(p.exists())
        self.assertTrue(Path(result['recovery_path']).is_dir())


if __name__ == '__main__':
    unittest.main()
