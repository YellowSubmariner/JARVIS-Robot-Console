"""Incremental provenance/transaction tests with synthetic episodes only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from jarvis_camera import incremental_export as inc
from jarvis_camera.dataset_export import export_bundle
from test_dataset_export import CONTRACT


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = {}
        self.episodes = [self.episode(i) for i in range(4)]
        self.loader = patch('jarvis_camera.dataset_export.load_prepared_episode', side_effect=self.load)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.patch_convert = patch.object(inc.worker, 'convert_bundle', side_effect=self.fake_convert)
        self.convert = self.patch_convert.start()
        self.addCleanup(self.patch_convert.stop)
        self.patch_factory = patch.object(inc.worker, '_load_dataset_factory', return_value=(None, {}))
        self.patch_factory.start()
        self.addCleanup(self.patch_factory.stop)
        self.patch_append = patch.object(inc, '_append', side_effect=self.fake_append)
        self.append = self.patch_append.start()
        self.addCleanup(self.patch_append.stop)

    def episode(self, index):
        root = self.root / f'raw_{index}'
        root.mkdir()
        for cam in ('top', 'wrist'):
            (root / f'{cam}_frames').mkdir()
            for frame in range(3):
                Image.new('RGB', (8, 6), (index * 20, frame * 20, 30)).save(root / f'{cam}_frames' / f'{frame}.jpg')
        rows = [{'timestamp': i / 20, 'monotonic_ns': 1_000_000_000 + i * 50_000_000,
                 'observation.images.top': f'top_frames/{i}.jpg',
                 'observation.images.wrist': f'wrist_frames/{i}.jpg',
                 'observation.state': [float(index)] * 7 + [0.25 + i * .05],
                 'action': [float(index)] * 7 + [0.3 + i * .05]}
                for i in range(3)]
        manifest = {**copy.deepcopy(CONTRACT), 'technical_pass': True, 'status': 'ready_for_review',
                    'source_fingerprint': f'{index + 1:064x}', 'prepared_signature': f'{index + 51:064x}',
                    'task': f'fake task {index}', 'num_samples': len(rows)}
        self.data[root] = (manifest, rows)
        return root

    def load(self, episode, *, require_review, include_source_hashes=False):
        self.assertTrue(require_review)
        root = Path(episode).resolve()
        manifest, rows = copy.deepcopy(self.data[root])
        if include_source_hashes:
            hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.glob('*_frames/*.jpg')}
            return manifest, rows, hashes
        return manifest, rows

    def bundle(self, name, episodes):
        target = self.root / name
        export_bundle(episodes, target)
        return target

    def fake_convert(self, bundle, output, *, progress):
        info = json.loads((bundle / 'bundle.json').read_text(encoding='utf-8'))
        output.mkdir()
        for split in inc.SPLITS:
            data = [x for x in info['episodes'] if x['split'] == split]
            meta = output / split / 'meta'
            meta.mkdir(parents=True)
            (meta / 'info.json').write_text(json.dumps({'total_episodes': len(data),
                'total_frames': sum(x['num_samples'] for x in data),
                'features': {key: {'shape': [6, 8, 3]} for key in inc.worker.IMAGE_KEYS}}), encoding='utf-8')
            frames = sum(x['num_samples'] for x in data)
            stats = {key: {'count': [frames], 'q01': [0.] * 8, 'q99': [1.] * 8}
                     for key in ('observation.state', 'action')}
            (meta / 'stats.json').write_text(json.dumps(stats), encoding='utf-8')
            (output / split / 'old-video.mp4').write_bytes(f'old {split}'.encode())
        result = {'schema_version': 1, 'complete': True, 'format': 'lerobot',
                  'counts': info['counts'], 'feature_contract': info['feature_contract'], 'splits': info['splits']}
        (output / 'export.json').write_text(json.dumps(result), encoding='utf-8')
        return {**result, 'output_dir': str(output)}

    def fake_append(self, stage, additions, progress):
        for item in additions:
            p = stage / item['split'] / 'meta' / 'info.json'
            info = json.loads(p.read_text(encoding='utf-8'))
            info['total_episodes'] += 1
            info['total_frames'] += item['num_samples']
            p.write_text(json.dumps(info), encoding='utf-8')
            stats_path = p.with_name('stats.json')
            stats = json.loads(stats_path.read_text(encoding='utf-8'))
            for key in ('observation.state', 'action'):
                stats[key]['count'] = [info['total_frames']]
            stats_path.write_text(json.dumps(stats), encoding='utf-8')
            (stage / item['split'] / (item['source_fingerprint'] + '.mp4')).write_bytes(b'new')

    def test_incremental_reuses_old_samples_and_keeps_prior_unchanged(self):
        first = self.root / 'first'
        result = inc.incremental_export(self.bundle('bundle_first', self.episodes[:3]), first)
        self.assertEqual(result['incremental'], {'reused': 0, 'converted': 3})
        old = inc._json(first / inc.MANIFEST)
        before = inc._inventory(first)
        second = self.root / 'second'
        result = inc.incremental_export(self.bundle('bundle_second', self.episodes), second, first)
        self.assertEqual(result['incremental'], {'reused': 3, 'converted': 1})
        self.assertEqual(result['counts']['total'], 4)
        self.assertEqual(inc._inventory(first), before)
        self.assertEqual(self.convert.call_count, 1)
        self.assertEqual(len(self.append.call_args.args[1]), 1)
        new = inc._json(second / inc.MANIFEST)
        for item in old['episodes']:
            counterpart = next(e for e in new['episodes'] if e['source_fingerprint'] == item['source_fingerprint'])
            self.assertEqual(counterpart['split'], item['split'])
            self.assertEqual(counterpart['episode_index'], item['episode_index'])
        for split in inc.SPLITS:
            indices = sorted(e['episode_index'] for e in new['episodes'] if e['split'] == split)
            self.assertEqual(indices, list(range(len(indices))))
        for split in inc.SPLITS:
            self.assertEqual((first / split / 'old-video.mp4').read_bytes(), (second / split / 'old-video.mp4').read_bytes())

    def test_rejects_changed_old_output_without_publishing(self):
        first = self.root / 'first'
        inc.incremental_export(self.bundle('bundle_first', self.episodes[:3]), first)
        (first / 'train' / 'old-video.mp4').write_bytes(b'tampered')
        out = self.root / 'second'
        with self.assertRaisesRegex(ValueError, '已改变'):
            inc.incremental_export(self.bundle('bundle_second', self.episodes), out, first)
        self.assertFalse(out.exists())

    def test_rejects_removed_or_reprocessed_previously_used_episode(self):
        first = self.root / 'first'
        inc.incremental_export(self.bundle('bundle_first', self.episodes[:3]), first)
        with self.assertRaisesRegex(ValueError, '移除'):
            inc.incremental_export(self.bundle('removed', self.episodes[1:]), self.root / 'bad1', first)
        self.data[self.episodes[0]][0]['prepared_signature'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, '重新审核'):
            inc.incremental_export(self.bundle('changed', self.episodes), self.root / 'bad2', first)

    def test_rejects_legacy_prior_and_existing_output(self):
        bundle = self.bundle('bundle_first', self.episodes[:3])
        old = self.root / 'old'
        old.mkdir()
        with self.assertRaisesRegex(ValueError, '没有可验证'):
            inc.incremental_export(bundle, self.root / 'new', old)
        with self.assertRaises(FileExistsError):
            inc.incremental_export(bundle, old)
        self.assertEqual(list(old.iterdir()), [])

    def test_rejects_junction_root_and_children_before_copy(self):
        bundle = self.bundle('bundle_first', self.episodes[:3])
        prior = self.root / 'prior'
        prior.mkdir()
        with patch.object(Path, 'is_junction', autospec=True,
                          side_effect=lambda path: path == prior):
            with self.assertRaisesRegex(ValueError, '目录联接'):
                inc.incremental_export(bundle, self.root / 'out1', prior)
        child = prior / 'linked'
        child.mkdir()
        with patch.object(Path, 'is_junction', autospec=True,
                          side_effect=lambda path: path == child):
            with self.assertRaisesRegex(ValueError, '目录联接'):
                inc._inventory(prior)

    def test_rejects_tampered_manifest_and_duplicate_episode_index(self):
        first = self.root / 'first'
        inc.incremental_export(self.bundle('bundle_first', self.episodes[:3]), first)
        bundle4 = self.bundle('bundle_second', self.episodes)
        second = self.root / 'second'
        inc.incremental_export(bundle4, second, first)
        p = second / inc.MANIFEST
        manifest = inc._json(p)
        train = [x for x in manifest['episodes'] if x['split'] == 'train']
        self.assertEqual(len(train), 2)
        train[1]['episode_index'] = train[0]['episode_index']
        p.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不能复用'):
            inc.incremental_export(bundle4, self.root / 'bad_checksum', second)
        manifest['manifest_sha256'] = inc._manifest_checksum(manifest)
        p.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '索引不连续或重复'):
            inc.incremental_export(bundle4, self.root / 'bad_index', second)

    def test_append_failure_preserves_previous_output_and_publishes_nothing(self):
        first = self.root / 'first'
        inc.incremental_export(self.bundle('bundle_first', self.episodes[:3]), first)
        old = inc._inventory(first)
        out = self.root / 'failed'
        with patch.object(inc, '_append', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                inc.incremental_export(self.bundle('bundle_second', self.episodes), out, first)
        self.assertFalse(out.exists())
        self.assertEqual(inc._inventory(first), old)
        self.assertFalse(any(self.root.glob('.ji-*')))


if __name__ == '__main__':
    unittest.main()
