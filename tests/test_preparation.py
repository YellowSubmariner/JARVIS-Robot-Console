"""Offline tests using synthetic raw recorder files; no SDK or hardware."""

import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis_camera.preparation import (
    CLOCK, PreparationConfig, PreparationError, approve_episode,
    load_prepared_episode, prepare_episode,
)


START = 10_000_000_000
STEP = 50_000_000
CHECKS = {'alignment': True, 'position_targets': True, 'stable_end': True}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def make_episode(root, count=61, camera_offset=0):
    root.mkdir()
    write_json(root / 'metadata.json', {
        'schema_version': 1, 'task': '把物体从左侧放到右侧',
        'recording_status': 'complete', 'user_outcome': 'success',
        'training_ready': False,
        'active': {'monotonic_ns': START, 'clock': CLOCK},
        'robot_counts': {'feedback_samples': count, 'feedback_errors': 0},
    })
    write_rows(root / 'events.jsonl', [
        {'event': 'recording_active', 'monotonic_ns': START, 'clock': CLOCK},
        {'event': 'stop_requested', 'monotonic_ns': START + (count - 1) * STEP,
         'clock': CLOCK},
    ])
    feedback = []
    for i in range(count):
        moment = START + i * STEP
        progress = min(i, 20)
        feedback.append({
            'schema_version': 1, 'sequence': i + 1,
            'request_monotonic_ns': moment - 1_000_000,
            'received_monotonic_ns': moment, 'clock': CLOCK,
            'feedback': {
                'actuators': [
                    {'position': 30 * j + progress / 10,
                     'fault_bank_a': 0, 'fault_bank_b': 0,
                     'warning_bank_a': 0, 'warning_bank_b': 0}
                    for j in range(7)
                ],
                'interconnect': {'gripper_feedback': {'motor': [
                    {'position': 20 + progress * 2}
                ]}},
            },
        })
    write_rows(root / 'robot_feedback.jsonl', feedback)
    for camera in ('top', 'wrist'):
        (root / (camera + '_frames')).mkdir()
        frames = []
        for i in range(count):
            relative = f'{camera}_frames/frame_{i:08d}.jpg'
            # Preparation only indexes/hashes images. Export tests own decoding.
            (root / relative).write_bytes(b'fake-jpeg-content-' + str(i).encode())
            frames.append({'sequence': i, 'file': relative,
                           'monotonic_ns': START + i * STEP + camera_offset,
                           'clock': CLOCK, 'source_segment_id': 0})
        write_rows(root / (camera + '_frames.jsonl'), frames)
        write_json(root / (camera + '_recording.json'), {
            'camera': camera, 'state': 'complete', 'complete': True,
            'clock': CLOCK, 'written': count, 'received': count,
            'dropped': 0, 'read_failures': 0, 'error': None,
        })
    return root


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = make_episode(Path(self.temp.name) / 'episode')

    def mutate_json(self, name, edit):
        path = self.root / name
        value = read_json(path)
        edit(value)
        write_json(path, value)

    def mutate_rows(self, name, edit):
        path = self.root / name
        rows = read_rows(path)
        edit(rows)
        write_rows(path, rows)

    def assert_blocked(self, code):
        manifest = prepare_episode(self.root)
        self.assertEqual(manifest['status'], 'blocked')
        self.assertFalse(manifest['technical_pass'])
        self.assertFalse(manifest['training_ready'])
        self.assertEqual(manifest['num_samples'], 0)
        self.assertIn(code, [issue['code'] for issue in manifest['issues']])
        self.assertEqual((self.root / 'prepared/samples.jsonl').read_bytes(), b'')
        with self.assertRaises(PreparationError):
            load_prepared_episode(self.root)
        return manifest

    def test_position_shift_units_and_continuous_gripper_preserve_raw(self):
        raw_before = {path.relative_to(self.root): path.read_bytes()
                      for path in self.root.rglob('*') if path.is_file()}
        manifest = prepare_episode(self.root)
        loaded, rows = load_prepared_episode(self.root)
        self.assertEqual(manifest, loaded)
        self.assertEqual(manifest['status'], 'ready_for_review')
        self.assertEqual(manifest['num_samples'], 60)
        self.assertFalse(manifest['training_ready'])
        self.assertEqual(manifest['action_schema'], 'next_measured_position_v1')
        self.assertEqual(manifest['units'], {'joints': 'radian', 'gripper': 'normalized_0_1'})
        self.assertEqual(rows[0]['observation.state'][7], .2)
        self.assertEqual(rows[0]['action'][7], .22)
        self.assertAlmostEqual(rows[0]['action'][0], math.radians(.1))
        self.assertEqual(rows[0]['action'], rows[1]['observation.state'])
        self.assertEqual(rows[0]['action_monotonic_ns'], START + STEP)
        self.assertEqual(rows[-1]['timestamp'], 2.95)
        self.assertEqual(rows[-1]['action_monotonic_ns'], START + 60 * STEP)
        self.assertTrue(all(len(row['action']) == 8 for row in rows))
        for name, data in raw_before.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_observations_are_causal_and_head_trim_is_explicit(self):
        for camera in ('top', 'wrist'):
            self.mutate_rows(camera + '_frames.jsonl',
                             lambda rows: [row.update(monotonic_ns=row['monotonic_ns'] + 10_000_000)
                                           for row in rows])
        manifest = prepare_episode(self.root)
        _, rows = load_prepared_episode(self.root)
        self.assertEqual(manifest['metrics']['trimmed_start_ms'], 50)
        self.assertEqual(rows[0]['monotonic_ns'], START + STEP)
        self.assertEqual(rows[0]['source_sequences']['top'], 0)
        self.assertEqual(rows[0]['source_ages_ms']['top'], 40)
        self.assertTrue(rows[0]['observation.images.top'].endswith('00000000.jpg'))
        self.assertEqual(rows[0]['timestamp'], 0)

    def test_stop_boundary_excludes_later_reset_motion(self):
        self.mutate_rows('events.jsonl', lambda rows: rows[1].update(monotonic_ns=START + 40 * STEP))
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[45]['feedback']['actuators'][0].update(position=300))
        manifest = prepare_episode(self.root)
        _, rows = load_prepared_episode(self.root)
        self.assertTrue(manifest['technical_pass'])
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[-1]['action_monotonic_ns'], START + 40 * STEP)

    def test_slow_camera_reuses_frames_without_compressing_time(self):
        for camera in ('top', 'wrist'):
            path = self.root / (camera + '_frames.jsonl')
            rows = read_rows(path)[::2]
            for i, row in enumerate(rows):
                row['sequence'] = i
            write_rows(path, rows)
            self.mutate_json(camera + '_recording.json', lambda value: value.update(written=len(rows)))
        manifest = prepare_episode(self.root)
        _, rows = load_prepared_episode(self.root)
        self.assertEqual(len(rows), 60)
        self.assertEqual(rows[0]['observation.images.top'], rows[1]['observation.images.top'])
        self.assertEqual(rows[1]['timestamp'] - rows[0]['timestamp'], .05)
        self.assertGreater(manifest['metrics']['reused_camera_frames']['top'], 0)

    def test_internal_camera_gap_blocks_entire_episode(self):
        path = self.root / 'top_frames.jsonl'
        rows = read_rows(path)
        rows = rows[:10] + rows[20:]
        for i, row in enumerate(rows):
            row['sequence'] = i
        write_rows(path, rows)
        self.mutate_json('top_recording.json', lambda value: value.update(written=len(rows)))
        self.assert_blocked('stale_camera')

    def test_internal_feedback_gap_blocks_entire_episode(self):
        path = self.root / 'robot_feedback.jsonl'
        rows = read_rows(path)
        rows = rows[:10] + rows[20:]
        for i, row in enumerate(rows):
            row['sequence'] = i + 1
        write_rows(path, rows)
        self.mutate_json('metadata.json', lambda value: value['robot_counts'].update(feedback_samples=len(rows)))
        self.assert_blocked('stale_feedback')

    def test_missing_image_and_path_traversal_block(self):
        (self.root / 'top_frames/frame_00000010.jpg').unlink()
        self.assert_blocked('missing_image')
        self.mutate_rows('top_frames.jsonl', lambda rows: rows[10].update(file='../outside.jpg'))
        self.assert_blocked('invalid_path')

    def test_joint_wrap_is_not_silently_unwrapped(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[10]['feedback']['actuators'][0].update(position=359))
        self.assert_blocked('joint_angle_wrap')

    def test_implausible_jump_blocks(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[10]['feedback']['actuators'][0].update(position=20))
        self.assert_blocked('joint_jump')

    def test_moving_terminal_gripper_blocks(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[-1]['feedback']['interconnect']['gripper_feedback']['motor'][0].update(position=65))
        self.assert_blocked('unstable_terminal_state')

    def test_safety_fault_blocks_warning_is_visible(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[5]['feedback']['actuators'][2].update(warning_bank_a=1))
        manifest = prepare_episode(self.root)
        self.assertTrue(manifest['technical_pass'])
        self.assertIn('joint_warning', [issue['code'] for issue in manifest['issues']])
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[5]['feedback']['actuators'][2].update(fault_bank_b=1))
        self.assert_blocked('joint_fault')

    def test_missing_safety_field_is_not_treated_as_zero(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[5]['feedback']['actuators'][2].pop('fault_bank_a'))
        self.assert_blocked('safety_unknown')

    def test_rejects_nonfinite_feedback(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[5]['feedback']['actuators'][0].update(position=float('nan')))
        self.assert_blocked('invalid_json')

    def test_incomplete_failed_and_legacy_recordings_are_clear(self):
        self.mutate_json('metadata.json', lambda value: value.update(recording_status='error'))
        self.assert_blocked('incomplete_recording')
        self.mutate_json('metadata.json', lambda value: value.update(recording_status='complete', user_outcome='failed'))
        self.assert_blocked('not_success')
        self.mutate_json('metadata.json', lambda value: value.update(user_outcome='success'))
        self.mutate_json('metadata.json', lambda value: value.pop('active'))
        self.assert_blocked('missing_active_time')

    def test_duplicate_timestamp_does_not_become_a_normal_sample(self):
        self.mutate_rows('robot_feedback.jsonl', lambda rows: rows[5].update(
            received_monotonic_ns=rows[4]['received_monotonic_ns']))
        self.assert_blocked('non_monotonic_time')

    def test_mixed_clock_is_rejected(self):
        self.mutate_rows('wrist_frames.jsonl', lambda rows: rows[5].update(clock='wall_clock'))
        self.assert_blocked('invalid_clock')

    def test_wrong_joint_and_gripper_dimensions_are_rejected(self):
        self.mutate_rows('robot_feedback.jsonl', lambda rows: rows[5]['feedback']['actuators'].pop())
        self.assert_blocked('missing_joints')

    def test_gripper_range_is_not_clamped(self):
        self.mutate_rows('robot_feedback.jsonl',
                         lambda rows: rows[5]['feedback']['interconnect']['gripper_feedback']['motor'][0].update(position=101))
        self.assert_blocked('gripper_range')

    def test_review_requires_all_explicit_checks(self):
        prepare_episode(self.root)
        with self.assertRaises(PreparationError):
            load_prepared_episode(self.root, require_review=True)
        for checks in ({}, {**CHECKS, 'position_targets': False}, {**CHECKS, 'alignment': 1}):
            with self.assertRaises(PreparationError):
                approve_episode(self.root, checks)
        review = approve_episode(self.root, CHECKS)
        manifest, _ = load_prepared_episode(self.root, require_review=True)
        self.assertEqual(review['prepared_signature'], manifest['prepared_signature'])
        self.assertEqual(review['review_kind'], 'human_confirmation_not_automatic_robot_validation')
        self.assertFalse(manifest['training_ready'])
        self.assertFalse(read_json(self.root / 'metadata.json')['training_ready'])

    def test_export_can_use_the_exact_reviewed_source_hashes(self):
        import hashlib
        prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        ordinary = load_prepared_episode(self.root, require_review=True)
        extended = load_prepared_episode(self.root, require_review=True, include_source_hashes=True)
        self.assertEqual(len(ordinary), 2)
        self.assertEqual(ordinary, extended[:2])
        image = self.root / 'top_frames/frame_00000010.jpg'
        self.assertEqual(extended[2]['top_frames/frame_00000010.jpg'],
                         hashlib.sha256(image.read_bytes()).hexdigest())

    def test_raw_image_edit_invalidates_existing_review(self):
        prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        (self.root / 'top_frames/frame_00000010.jpg').write_bytes(b'modified image')
        with self.assertRaisesRegex(PreparationError, '原始数据已经变化'):
            load_prepared_episode(self.root, require_review=True)

    def test_prepared_sample_edit_is_detected(self):
        prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        path = self.root / 'prepared/samples.jsonl'
        rows = read_rows(path)
        rows[3]['action'][7] = .99
        write_rows(path, rows)
        with self.assertRaisesRegex(PreparationError, '整理结果已变化'):
            load_prepared_episode(self.root, require_review=True)

    def test_reprocessing_invalidates_review_even_with_identical_source(self):
        first = prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        second = prepare_episode(self.root)
        self.assertEqual(first['source_fingerprint'], second['source_fingerprint'])
        self.assertNotEqual(first['prepared_signature'], second['prepared_signature'])
        with self.assertRaises(PreparationError):
            load_prepared_episode(self.root, require_review=True)

    def test_write_failure_is_reported_and_does_not_keep_old_approval(self):
        prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        with patch('jarvis_camera.preparation._atomic_bytes', side_effect=PreparationError('磁盘已满')):
            with self.assertRaisesRegex(PreparationError, '磁盘已满'):
                prepare_episode(self.root)
        with self.assertRaises(PreparationError):
            load_prepared_episode(self.root, require_review=True)

    def test_invalid_thresholds_rejected(self):
        for config in ({'fps': 0}, {'max_camera_age_ms': float('nan')}, {'typo': 2},
                       PreparationConfig(min_samples=1)):
            with self.assertRaises(PreparationError):
                prepare_episode(self.root, config)

    def test_malformed_counts_produce_blocked_manifest(self):
        self.mutate_json('metadata.json', lambda value: value.update(robot_counts='broken'))
        self.assert_blocked('invalid_feedback_counts')

    def test_review_checks_cannot_be_replaced_with_a_non_object(self):
        prepare_episode(self.root)
        approve_episode(self.root, CHECKS)
        self.mutate_json('prepared/review.json', lambda value: value.update(checks=['alignment']))
        with self.assertRaises(PreparationError):
            load_prepared_episode(self.root, require_review=True)

    def test_real_preparation_to_bundle_to_converter_keeps_units_and_rgb(self):
        from PIL import Image
        import numpy as np
        from jarvis_camera.dataset_export import export_bundle
        from jarvis_camera.lerobot_export_worker import convert_bundle

        episodes = [self.root]
        for index in (2, 3):
            episode = make_episode(Path(self.temp.name) / f'episode_{index}', count=31)
            metadata = read_json(episode / 'metadata.json')
            metadata['task'] += f'，第 {index} 次示范'
            write_json(episode / 'metadata.json', metadata)
            episodes.append(episode)
        for episode in episodes:
            for camera, color in (('top', (180, 20, 30)), ('wrist', (10, 20, 150))):
                for path in (episode / (camera + '_frames')).glob('*.jpg'):
                    Image.new('RGB', (16, 12), color).save(path, quality=95)
            self.assertTrue(prepare_episode(episode)['technical_pass'])
            approve_episode(episode, CHECKS)

        bundle_path = Path(self.temp.name) / 'bundle'
        bundle = export_bundle(episodes, bundle_path)
        datasets = []

        class FakeDataset:
            def __init__(self, **options):
                self.options = options
                self.frames = []
                self.episodes = []
                self.finalized = False
                options['root'].mkdir()
                datasets.append(self)

            def add_frame(self, frame):
                self.frames.append(frame)

            def save_episode(self):
                self.episodes.append(self.frames)
                self.frames = []

            def finalize(self):
                self.finalized = True

        result = convert_bundle(bundle_path, Path(self.temp.name) / 'lerobot',
                                dataset_factory=FakeDataset)
        self.assertEqual(bundle['counts'], {'train': 1, 'val': 1, 'test': 1, 'total': 3})
        self.assertEqual(result['counts'], bundle['counts'])
        self.assertEqual(len(datasets), 3)
        self.assertEqual(sum(len(dataset.episodes[0]) for dataset in datasets), 120)
        for dataset in datasets:
            self.assertTrue(dataset.finalized)
            self.assertEqual(len(dataset.episodes), 1)
            self.assertEqual(dataset.options['fps'], 20)
            self.assertEqual(dataset.options['features']['observation.images.top']['shape'], (12, 16, 3))
            first = dataset.episodes[0][0]
            self.assertAlmostEqual(float(first['observation.state'][7]), .2, places=6)
            self.assertAlmostEqual(float(first['action'][7]), .22, places=6)
            self.assertAlmostEqual(float(first['action'][0]), math.radians(.1), places=7)
            self.assertEqual(first['action'].dtype, np.float32)
            self.assertEqual(first['observation.images.top'].dtype, np.uint8)
            self.assertEqual(first['observation.images.wrist'].shape, (12, 16, 3))
            top_pixel = first['observation.images.top'][0, 0]
            wrist_pixel = first['observation.images.wrist'][0, 0]
            self.assertGreater(int(top_pixel[0]), int(top_pixel[2]))
            self.assertGreater(int(wrist_pixel[2]), int(wrist_pixel[0]))
            self.assertTrue(first['task'])


if __name__ == '__main__':
    unittest.main()
