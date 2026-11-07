"""CPU-only export and conversion checks. No model, SDK, camera or network."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from jarvis_camera.dataset_export import export_bundle
from jarvis_camera.lerobot_export_worker import convert_bundle, main


CONTRACT = {
    'action_schema': 'next_measured_position_v1', 'fps': 20,
    'state_names': [f'q{i}' for i in range(1, 8)] + ['gripper_measured'],
    'action_names': [f'q{i}_target' for i in range(1, 8)] + ['gripper_position_proxy'],
    'units': {'joints': 'radian', 'gripper': 'normalized_0_1'},
    'joint_representation': 'raw_degrees_converted_to_radians_no_wrap',
    'gripper_label_source': 'next_measured_position_proxy_not_command',
    'alignment': 'causal_host_receipt_time',
}


class FakeDataset:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.frames = []
        self.episodes = []
        self.finalized = False
        self.kwargs['root'].mkdir()

    def add_frame(self, frame):
        self.frames.append({key: value.copy() if isinstance(value, np.ndarray) else value
                            for key, value in frame.items()})

    def save_episode(self):
        self.episodes.append(self.frames)
        self.frames = []

    def finalize(self):
        self.finalized = True
        (self.kwargs['root'] / 'test-written.json').write_text('{}', encoding='utf-8')


class DatasetExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Windows TEMP may use an 8.3 alias; match the canonical keys used below.
        self.root = Path(self.temp.name).resolve()
        self.prepared = {}
        self.episodes = [self.make_episode(i) for i in range(3)]
        self.loader = patch('jarvis_camera.dataset_export.load_prepared_episode', side_effect=self.load)
        self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.temp.cleanup()

    def make_episode(self, number):
        root = self.root / f'raw_{number}'
        root.mkdir()
        for camera, color in (('top', (255, 0, 0)), ('wrist', (0, 0, 255))):
            folder = root / f'{camera}_frames'
            folder.mkdir()
            for frame in range(3):
                Image.new('RGB', (4, 3), color).save(folder / f'{frame}.jpg', quality=100, subsampling=0)
        (root / 'connection.json').write_text('{"password":"must-not-be-copied","robot_ip":"private-host"}')
        rows = []
        for frame in range(3):
            rows.append({'timestamp': frame / 20, 'monotonic_ns': 1_000_000_000 + frame * 50_000_000,
                         'observation.images.top': f'top_frames/{frame}.jpg',
                         'observation.images.wrist': f'wrist_frames/{frame}.jpg',
                         'observation.state': [float(number)] * 7 + [0.37],
                         'action': [float(number) + .01] * 7 + [0.43],
                         'source_ages_ms': {'top': 5.0, 'wrist': 9.0, 'robot': 2.0,
                                            'password': 'must-not-be-copied'},
                         'source_sequences': {'top': frame, 'wrist': frame, 'robot': frame}})
        manifest = {**copy.deepcopy(CONTRACT), 'status': 'ready_for_review', 'technical_pass': True,
                    'training_ready': False, 'task': f'任务 {number}', 'num_samples': 3,
                    'source_fingerprint': hashlib.sha256(f'source-{number}'.encode()).hexdigest(),
                    'prepared_signature': hashlib.sha256(f'prepared-{number}'.encode()).hexdigest(),
                    'password': 'must-not-be-copied', 'robot_ip': 'private-host'}
        self.prepared[root.resolve()] = (manifest, rows)
        return root

    def load(self, episode, *, require_review, include_source_hashes=False):
        self.assertTrue(require_review)
        root = Path(episode).resolve()
        manifest, rows = copy.deepcopy(self.prepared[root])
        if include_source_hashes:
            hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.glob('*_frames/*.jpg')}
            return manifest, rows, hashes
        return manifest, rows

    def export(self, name='bundle'):
        return export_bundle(self.episodes, self.root / name)

    def test_bundle_is_reviewed_portable_and_split_by_whole_episode(self):
        result = self.export()
        root = Path(result['output_dir'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['counts'], {'train': 1, 'val': 1, 'test': 1, 'total': 3})
        self.assertEqual(len({item for items in result['splits'].values() for item in items}), 3)
        self.assertTrue((root / 'convert_to_lerobot.py').is_file())
        for path in root.rglob('*.json*'):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('must-not-be-copied', text)
            self.assertNotIn('private-host', text)
            self.assertNotIn(str(self.root), text)
        for entry in result['episodes']:
            metadata = json.loads((root / entry['path'] / 'episode.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['review']['checks'], {'alignment': True, 'position_targets': True, 'stable_end': True})
            self.assertEqual(metadata['feature_contract']['gripper_label_source'], CONTRACT['gripper_label_source'])
        # Continuous measured-position targets remain continuous; no binary close inference.
        entry = result['episodes'][0]
        rows = (root / entry['path'] / 'samples.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(json.loads(rows[0])['action'][-1], .43)

    def test_selection_order_does_not_change_split(self):
        first = self.export('first')
        second = export_bundle(list(reversed(self.episodes)), self.root / 'second')
        def mapping(result):
            root = Path(result['output_dir'])
            return {json.loads((root / e['path'] / 'episode.json').read_text())['source_fingerprint']: e['split']
                    for e in result['episodes']}
        self.assertEqual(mapping(first), mapping(second))

    def test_too_few_duplicate_unreviewed_and_failed_episodes_are_not_exported(self):
        with self.assertRaisesRegex(ValueError, '至少'):
            export_bundle(self.episodes[:2], self.root / 'few')
        with self.assertRaisesRegex(ValueError, '重复'):
            export_bundle([self.episodes[0]] * 3, self.root / 'duplicate')
        with patch('jarvis_camera.dataset_export.load_prepared_episode', side_effect=ValueError('尚未检查')):
            with self.assertRaisesRegex(ValueError, '尚未检查'):
                self.export('unreviewed')
        self.prepared[self.episodes[0]][0]['technical_pass'] = False
        with self.assertRaisesRegex(ValueError, '未通过'):
            self.export('failed')
        for name in ('few', 'duplicate', 'unreviewed', 'failed'):
            self.assertFalse((self.root / name).exists())

    def test_different_contracts_and_duplicate_source_copies_rejected(self):
        original = self.prepared[self.episodes[0]][0]['fps']
        self.prepared[self.episodes[0]][0]['fps'] = 10
        with self.assertRaisesRegex(ValueError, '不同'):
            self.export('mixed')
        self.prepared[self.episodes[0]][0]['fps'] = original
        self.prepared[self.episodes[1]][0]['source_fingerprint'] = self.prepared[self.episodes[0]][0]['source_fingerprint']
        with self.assertRaisesRegex(ValueError, '重复副本'):
            self.export('copies')

    def test_path_escape_and_missing_picture_leave_no_partial_output(self):
        rows = self.prepared[self.episodes[0]][1]
        rows[0]['observation.images.top'] = '../outside.jpg'
        with self.assertRaisesRegex(ValueError, '离开'):
            self.export('escape')
        self.assertFalse((self.root / 'escape').exists())
        self.assertEqual(list(self.root.glob('.escape.preparing-*')), [])
        rows[0]['observation.images.top'] = 'top_frames/missing.jpg'
        with self.assertRaisesRegex(ValueError, '缺失'):
            self.export('missing')
        self.assertFalse((self.root / 'missing').exists())

    def test_symlink_outside_episode_is_rejected(self):
        outside = self.root / 'outside.jpg'
        Image.new('RGB', (4, 3)).save(outside)
        link = self.episodes[0] / 'linked.jpg'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks is unavailable for this Windows account')
        self.prepared[self.episodes[0]][1][0]['observation.images.top'] = 'linked.jpg'
        with self.assertRaisesRegex(ValueError, '超出'):
            self.export('linked')

    def test_corrupt_images_and_inconsistent_dimensions_do_not_publish_bundle(self):
        picture = self.episodes[0] / 'top_frames/0.jpg'
        picture.write_bytes(b'not a jpeg')
        with self.assertRaisesRegex(ValueError, '损坏'):
            self.export('corrupt')
        self.assertFalse((self.root / 'corrupt').exists())
        Image.new('RGB', (8, 3)).save(picture)
        with self.assertRaisesRegex(ValueError, '分辨率'):
            self.export('dimensions')
        self.assertFalse((self.root / 'dimensions').exists())

    def test_existing_destination_and_raw_sources_are_preserved(self):
        output = self.root / 'existing'
        output.mkdir()
        keep = output / 'keep.txt'
        keep.write_text('keep')
        with self.assertRaises(FileExistsError):
            export_bundle(self.episodes, output)
        self.assertEqual(keep.read_text(), 'keep')
        self.assertIn('must-not-be-copied', (self.episodes[0] / 'connection.json').read_text())

    def test_source_changes_during_export_abort_instead_of_certifying_old_review(self):
        calls = {}
        def changing(episode, *, require_review, include_source_hashes=False):
            key = Path(episode).resolve()
            calls[key] = calls.get(key, 0) + 1
            result = self.load(episode, require_review=require_review,
                               include_source_hashes=include_source_hashes)
            manifest = result[0]
            if calls[key] > 1:
                manifest['source_fingerprint'] = 'changed'
            return result
        with patch('jarvis_camera.dataset_export.load_prepared_episode', side_effect=changing):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.export('changed')
        self.assertFalse((self.root / 'changed').exists())

    def test_copied_image_matches_reviewed_snapshot_even_if_source_is_restored(self):
        from jarvis_camera.dataset_export import _copy_hashed

        changed = False
        def transient_change(source, destination):
            nonlocal changed
            if changed:
                return _copy_hashed(source, destination)
            changed = True
            original = source.read_bytes()
            try:
                source.write_bytes(b'temporarily changed while copying')
                return _copy_hashed(source, destination)
            finally:
                source.write_bytes(original)
        with patch('jarvis_camera.dataset_export._copy_hashed', side_effect=transient_change):
            with self.assertRaisesRegex(ValueError, '原始图片发生变化'):
                self.export('transient')
        self.assertTrue(changed)
        self.assertFalse((self.root / 'transient').exists())
        self.assertEqual(list(self.root.glob('.transient.preparing-*')), [])
        self.assertEqual(list(self.root.glob('.jarvis-verified-*')), [])

    def test_verified_export_reads_each_original_snapshot_twice(self):
        calls = {}
        def counted(episode, *, require_review, include_source_hashes=False):
            key = Path(episode).resolve()
            calls[key] = calls.get(key, 0) + 1
            return self.load(episode, require_review=require_review,
                             include_source_hashes=include_source_hashes)
        with patch('jarvis_camera.dataset_export.load_prepared_episode', side_effect=counted):
            self.export('twice')
        self.assertEqual(set(calls.values()), {2})

    def test_worker_uses_rgb_float32_and_separate_dataset_writers(self):
        bundle = self.export()
        datasets = []
        def factory(**kwargs):
            dataset = FakeDataset(kwargs)
            datasets.append(dataset)
            return dataset
        result = convert_bundle(bundle['output_dir'], self.root / 'lerobot', dataset_factory=factory)
        self.assertTrue(result['complete'])
        self.assertEqual([d.kwargs['repo_id'] for d in datasets], ['local/jarvis_train', 'local/jarvis_val', 'local/jarvis_test'])
        self.assertEqual(len({d.kwargs['root'] for d in datasets}), 3)
        self.assertTrue(all(d.finalized for d in datasets))
        seen_tasks = []
        for dataset in datasets:
            self.assertEqual(len(dataset.episodes), 1)
            self.assertEqual(dataset.kwargs['features']['action']['shape'], (8,))
            self.assertEqual(dataset.kwargs['features']['observation.images.top']['shape'], (3, 4, 3))
            frames = dataset.episodes[0]
            self.assertEqual(len(frames), 3)
            seen_tasks.append(frames[0]['task'])
            for frame in frames:
                self.assertEqual(frame['action'].dtype, np.float32)
                self.assertAlmostEqual(float(frame['action'][-1]), .43, places=6)
                self.assertGreater(int(frame['observation.images.top'][0, 0, 0]), 250)
                self.assertLess(int(frame['observation.images.top'][0, 0, 2]), 5)
                self.assertGreater(int(frame['observation.images.wrist'][0, 0, 2]), 250)
        self.assertEqual(len(set(seen_tasks)), 3)
        self.assertTrue((self.root / 'lerobot/export.json').exists())

    def test_worker_rejects_tampered_file_before_creating_dataset(self):
        bundle = self.export()
        root = Path(bundle['output_dir'])
        entry = bundle['episodes'][0]
        (root / entry['path'] / 'samples.jsonl').write_text('{}\n')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            convert_bundle(root, self.root / 'bad', dataset_factory=lambda **_: self.fail('must not create'))
        self.assertFalse((self.root / 'bad').exists())

    def test_worker_rejects_inconsistent_times_even_with_consistent_file_hashes(self):
        for name, mutate, message in (
            ('start', lambda rows: [r.update(timestamp=r['timestamp'] + 1) for r in rows], '从零'),
            ('gap', lambda rows: rows[1].update(timestamp=.07), '不连续'),
            ('clock', lambda rows: rows[1].update(monotonic_ns=1_070_000_000), '单调时钟'),
        ):
            with self.subTest(name=name):
                bundle = self.export('bundle_' + name)
                root = Path(bundle['output_dir'])
                entry = bundle['episodes'][0]
                episode = root / entry['path']
                samples = episode / 'samples.jsonl'
                rows = [json.loads(line) for line in samples.read_text().splitlines()]
                mutate(rows)
                samples.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                manifest_path = episode / 'episode.json'
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                manifest['files_sha256']['samples.jsonl'] = hashlib.sha256(samples.read_bytes()).hexdigest()
                manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                bundle.pop('output_dir')
                entry['manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                (root / 'bundle.json').write_text(json.dumps(bundle), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, message):
                    convert_bundle(root, self.root / ('bad_' + name), dataset_factory=lambda **_: self.fail('must not create'))

    def test_worker_failure_finalizes_writer_and_does_not_publish_partial_dataset(self):
        bundle = self.export()
        created = []
        class FailingDataset(FakeDataset):
            def add_frame(self, frame):
                raise OSError('disk full')
        def factory(**kwargs):
            dataset = FailingDataset(kwargs)
            created.append(dataset)
            return dataset
        with self.assertRaises(OSError):
            convert_bundle(bundle['output_dir'], self.root / 'failure', dataset_factory=factory)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].finalized)
        self.assertFalse((self.root / 'failure').exists())
        self.assertEqual(list(self.root.glob('.failure.converting-*')), [])

    def test_worker_detects_picture_change_after_preflight(self):
        bundle = self.export()
        root = Path(bundle['output_dir'])
        def factory(**kwargs):
            if kwargs['repo_id'] == 'local/jarvis_train':
                entry = next(e for e in bundle['episodes'] if e['split'] == 'train')
                picture = next((root / entry['path'] / 'images/top').glob('*.jpg'))
                picture.write_bytes(b'changed after verification')
            return FakeDataset(kwargs)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            convert_bundle(root, self.root / 'changed_image', dataset_factory=factory)
        self.assertFalse((self.root / 'changed_image').exists())

    def test_cli_reports_missing_environment_as_json_without_loading_model(self):
        bundle = self.export()
        lines = []
        with patch('jarvis_camera.lerobot_export_worker._load_dataset_factory', side_effect=ValueError('需要数据转换环境')), \
             patch('builtins.print', side_effect=lambda line, **_: lines.append(json.loads(line))):
            code = main(['--bundle', bundle['output_dir'], '--output', str(self.root / 'needs_env')])
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], {'type': 'error', 'message': '需要数据转换环境'})
        self.assertFalse((self.root / 'needs_env').exists())


if __name__ == '__main__':
    unittest.main()
