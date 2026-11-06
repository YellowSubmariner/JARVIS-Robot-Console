"""Headless UI orchestration checks; no cameras or robot are accessed."""

import json
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from jarvis_camera.recording_page import RecordingPage, _clock
from jarvis_camera.credentials import CredentialStoreError
from jarvis_camera.settings import Settings


class FakeStream:
    def __init__(self):
        self.started = []
        self.stopped = 0

    def start_recording(self, directory, name):
        self.started.append((directory, name))

    def stop_recording(self):
        self.stopped += 1


class RecordingPageTests(unittest.TestCase):
    def test_event_clock_uses_cross_process_clock(self):
        sample = _clock()
        self.assertEqual(sample['clock'], 'python_perf_counter_ns')
        self.assertLess(abs(time.perf_counter_ns() - sample['monotonic_ns']), 1_000_000_000)

    def test_selected_python_path_is_remembered_without_overwriting_existing_camera_settings(self):
        data = self.episode / 'data'
        data.mkdir()
        (data / 'settings.json').write_text(json.dumps({'active_page': 'wrist',
            'wrist_url': 'rtsp://camera.example/color', 'astra_name': 'My Camera'}), encoding='utf-8')
        with patch('jarvis_camera.settings.app_directory', return_value=self.episode):
            self.app.settings = Settings()
            with patch('jarvis_camera.recording_page.filedialog.askopenfilename', return_value='C:/设备/python.exe'):
                self.page._choose_python()
            reopened = Settings()
            self.assertEqual(reopened.get('kortex_python'), 'C:/设备/python.exe')
            self.assertEqual(reopened.get('wrist_url'), 'rtsp://camera.example/color')
            self.assertEqual(reopened.get('astra_name'), 'My Camera')
            self.app.settings = reopened
            page = RecordingPage(self.root, self.app)
            try:
                self.assertEqual(page.python_path.get(), 'C:/设备/python.exe')
            finally:
                page.destroy()

    def test_missing_python_prompts_for_selection_before_password(self):
        self.app.pages = {key: SimpleNamespace(snapshot_info={'state': 'streaming', 'last_frame_at': time.time()})
                          for key in ('astra', 'wrist')}
        self.page.python_path.set('')
        self.page.task.set('一段示范')
        with patch('jarvis_camera.recording_page.messagebox.showinfo') as info, \
             patch.object(self.page, '_recording_password') as password:
            self.page.start()
            self.assertIn('Kortex Python', info.call_args.args[0])
            password.assert_not_called()

    def test_task_must_be_chosen_for_each_episode(self):
        self.assertEqual(self.page.task.get(), '')
        self.page.task.set('把红色积木放到盒子里')
        self.page.state = 'stopping'
        self.page.pending_outcome = 'success'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 3}
        self.page.camera_infos = self.cameras('complete')
        self.page._finalize()
        self.assertEqual(self.page.task.get(), '')

    def test_password_is_asked_once_then_loaded_for_next_recordings(self):
        class FakeStore:
            value = None
            def load(self, ip, user):
                return self.value
            def save(self, ip, user, password):
                self.value = password
            def delete(self, ip, user):
                self.value = None
        store = FakeStore()
        identity = ('192.0.2.1', 'operator')
        with patch('jarvis_camera.recording_page.WindowsCredentialStore', return_value=store), \
             patch('jarvis_camera.recording_page.simpledialog.askstring', return_value='test-secret') as prompt:
            self.assertEqual(self.page._recording_password(identity), 'test-secret')
            self.assertEqual(self.page._recording_password(identity), 'test-secret')
            prompt.assert_called_once()
        store.delete(*identity)
        self.assertIsNone(store.load(*identity))

    def test_credential_failure_does_not_fall_back_to_plaintext(self):
        class BrokenStore:
            def load(self, ip, user):
                raise CredentialStoreError('synthetic failure')
        with patch('jarvis_camera.recording_page.WindowsCredentialStore', return_value=BrokenStore()), \
             patch('jarvis_camera.recording_page.simpledialog.askstring') as prompt, \
             patch('jarvis_camera.recording_page.messagebox.showerror'):
            self.assertIsNone(self.page._recording_password(('192.0.2.1', 'operator')))
            prompt.assert_not_called()

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.episode = Path(self.temp.name)
        self.app = SimpleNamespace(streams={'astra': FakeStream(), 'wrist': FakeStream()}, pages={})
        self.page = RecordingPage(self.root, self.app)
        self.page.episode = self.episode
        self.page.manifest = {'task': 'test', 'training_ready': False}

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()

    @staticmethod
    def cameras(state, written=1):
        return {name: {'recording': {'state': state, 'written': written, 'dropped': 0}}
                for name in ('astra', 'wrist')}

    def test_camera_recording_waits_for_robot_and_both_sources(self):
        self.page.state = 'connecting_robot'
        self.page.start_clock = time.monotonic()
        self.page.robot_events.put({'type': 'status', 'state': 'recording'})
        self.page.update_recording(self.cameras('starting'), visible=False)
        self.assertEqual(self.page.state, 'starting_cameras')
        self.assertEqual(self.app.streams['astra'].started[0][1], 'top')
        self.assertEqual(self.app.streams['wrist'].started[0][1], 'wrist')
        self.page.update_recording(self.cameras('recording'), visible=False)
        self.assertEqual(self.page.state, 'recording')

    def test_camera_error_stops_both_sources(self):
        self.page.state = 'starting_cameras'
        self.page.start_clock = time.monotonic()
        self.page.camera_started = {'astra', 'wrist'}
        infos = self.cameras('recording')
        infos['astra']['recording']['state'] = 'error'
        self.page.update_recording(infos, visible=False)
        self.assertEqual(self.app.streams['astra'].stopped, 1)
        self.assertEqual(self.app.streams['wrist'].stopped, 1)
        self.assertEqual(self.page.manifest['failure_reason'], 'camera_recording_error')
        self.assertNotEqual(self.page.manifest.get('recording_status'), 'complete')

    def test_zero_robot_samples_cannot_be_complete(self):
        self.page.state = 'stopping'
        self.page.pending_outcome = 'success'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 0}
        self.page.camera_infos = self.cameras('complete')
        self.page._finalize()
        metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['recording_status'], 'incomplete')
        self.assertFalse(metadata['training_ready'])

    def test_finished_recording_is_never_automatically_training_ready(self):
        self.page.state = 'stopping'
        self.page.pending_outcome = 'success'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 3}
        self.page.camera_infos = self.cameras('complete')
        self.page._finalize()
        metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['recording_status'], 'complete')
        self.assertFalse(metadata['training_ready'])
        self.assertEqual(metadata['quality_review'], 'not_reviewed')

    def test_missing_required_feedback_cannot_be_complete(self):
        for counter, warning in (
            ('feedback_errors', 'feedback_error'),
            ('missing_joint_samples', 'missing_joint_feedback'),
            ('missing_gripper_samples', 'missing_gripper_feedback'),
        ):
            with self.subTest(counter=counter):
                self.page.state = 'stopping'
                self.page.pending_outcome = 'success'
                self.page.robot_state = 'stopped'
                self.page.robot_counts = {'feedback_samples': 3, counter: 1}
                self.page.camera_infos = self.cameras('complete')
                self.page.warnings = []
                self.page._finalize()
                metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
                self.assertEqual(metadata['recording_status'], 'incomplete')
                self.assertFalse(metadata['training_ready'])
                self.assertIn(warning, metadata['warnings'])

    def test_zero_controller_events_is_unverified_not_incomplete(self):
        self.page.state = 'stopping'
        self.page.pending_outcome = 'success'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 3, 'controller_subscription': 'subscribed',
                                  'controller_events': 0}
        self.page.camera_infos = self.cameras('complete')
        self.page.manifest['controller_events_requested'] = True
        self.page._finalize()
        metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['recording_status'], 'complete')
        self.assertIn('controller_input_unverified', metadata['warnings'])
        self.assertFalse(metadata['training_ready'])

    def test_joint_warning_and_fault_preserve_raw_episode_and_are_distinct(self):
        self.page.state = 'recording'
        self.page.robot_events.put({'type': 'warning', 'code': 'joint_warning_detected'})
        self.page.update_recording(self.cameras('recording'), visible=False)
        self.assertIn('关节警告', self.page.details_text.get())
        self.assertEqual(self.page.state, 'recording')
        self.page.robot_events.put({'type': 'warning', 'code': 'joint_fault_detected'})
        self.page.update_recording(self.cameras('recording'), visible=False)
        self.assertIn('关节故障', self.page.details_text.get())
        self.assertIn('关节警告', self.page.details_text.get())
        self.assertEqual(self.page.state, 'recording')

        self.page.state = 'stopping'
        self.page.pending_outcome = 'success'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 3, 'joint_fault_samples': 1,
                                  'joint_warning_samples': 2}
        self.page.camera_infos = self.cameras('complete')
        self.page._finalize()
        metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['recording_status'], 'complete')
        self.assertIn('joint_fault_detected', metadata['warnings'])
        self.assertIn('joint_warning_detected', metadata['warnings'])
        self.assertFalse(metadata['training_ready'])
        self.assertIn('关节故障', self.page.details_text.get())
        self.assertIn('关节警告', self.page.details_text.get())

    def test_zero_camera_frames_cannot_be_complete(self):
        self.page.state = 'stopping'
        self.page.pending_outcome = 'failed'
        self.page.robot_state = 'stopped'
        self.page.robot_counts = {'feedback_samples': 3}
        self.page.camera_infos = self.cameras('complete', written=0)
        self.page._finalize()
        metadata = json.loads((self.episode / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['recording_status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
