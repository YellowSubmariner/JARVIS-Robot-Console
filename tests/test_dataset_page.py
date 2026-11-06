"""Offline sample-library behavior. No robot, model, network or real converter."""

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from jarvis_camera.dataset_page import DatasetPage, scan_episodes, safe_image_path, run_conversion


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def make_episode(root, name='episode_1', reviewed=False):
    episode = root / name
    episode.mkdir()
    write_json(episode / 'metadata.json', {'task': '把红色积木放到框里',
               'recording_status': 'complete', 'user_outcome': 'success'})
    write_json(episode / 'prepared/manifest.json', {'status': 'ready_for_review',
               'technical_pass': True, 'source_fingerprint': 'source123',
               'prepared_signature': 'derived123', 'num_samples': 5})
    if reviewed:
        write_json(episode / 'prepared/review.json', {'reviewed_for_export': True,
                   'source_fingerprint': 'source123', 'prepared_signature': 'derived123',
                   'checks': {'alignment': True, 'position_targets': True, 'stable_end': True}})
    return episode


class LibraryFileTests(unittest.TestCase):
    def test_page_preserves_tk_widget_methods(self):
        overrides = {name for name, value in vars(DatasetPage).items()
                     if not name.startswith('__') and callable(value) and hasattr(tk.Frame, name)}
        self.assertEqual(overrides, {'destroy'}, 'Application helpers must not shadow Tk internals')

    def test_summary_does_not_treat_arbitrary_review_as_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            episode = make_episode(root, reviewed=True)
            self.assertTrue(scan_episodes(root)[0]['reviewed'])
            original_review = json.loads((episode / 'prepared/review.json').read_text())
            for changes in ({'reviewed_for_export': False}, {'prepared_signature': 'changed'},
                            {'checks': None},
                            {'checks': {'alignment': True, 'position_targets': False, 'stable_end': True}}):
                review = dict(original_review)
                review.update(changes)
                write_json(episode / 'prepared/review.json', review)
                self.assertFalse(scan_episodes(root)[0]['reviewed'])

    def test_viewer_rejects_outside_image_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            episode = make_episode(root)
            (root / 'outside.jpg').write_bytes(b'test')
            for path in ('../outside.jpg', str(root / 'outside.jpg')):
                with self.assertRaises(ValueError):
                    safe_image_path(episode, path)
            (episode / 'inside.jpg').write_bytes(b'test')
            self.assertEqual(safe_image_path(episode, 'inside.jpg'), episode / 'inside.jpg')

    def test_bad_metadata_is_visible_not_a_valid_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            episode = make_episode(root, reviewed=True)
            (episode / 'metadata.json').write_text('broken', encoding='utf-8')
            entries = scan_episodes(root)
            self.assertEqual(entries[0]['status'], '记录无法读取')
            self.assertFalse(entries[0]['reviewed'])

    def test_converter_protocol_requires_success_result(self):
        class Process:
            def __init__(self, output, code):
                self.stdout = io.StringIO(output)
                self.code = code
            def wait(self):
                return self.code
            def poll(self):
                return self.code

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / 'python.exe'
            python.write_bytes(b'placeholder')
            progress = []
            output = '\n'.join([json.dumps({'type': 'progress', 'message': '转换中'}),
                                json.dumps({'type': 'result', 'output_dir': str(root / 'data')})])
            with patch('jarvis_camera.dataset_page.subprocess.Popen', return_value=Process(output, 0)) as popen:
                result = run_conversion(root, root / 'data', python, progress.append)
            self.assertEqual(len(progress), 1)
            self.assertEqual(result['output_dir'], str(root / 'data'))
            args = popen.call_args.args[0]
            self.assertEqual(args[0], str(python))
            self.assertIn('--bundle', args)
            self.assertNotIn('shell', popen.call_args.kwargs)
            with patch('jarvis_camera.dataset_page.subprocess.Popen', return_value=Process('not a result', 0)):
                with self.assertRaises(ValueError):
                    run_conversion(root, root / 'data', python, progress.append)


class DatasetPageTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.root.withdraw()
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        record = SimpleNamespace(folder=tk.StringVar(master=self.root, value=str(self.folder)), busy=False)
        self.app = SimpleNamespace(pages={'record': record})
        self.page = DatasetPage(self.root, self.app)

    def tearDown(self):
        self.page.destroy()
        self.root.destroy()
        self.temporary.cleanup()

    def pump(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('Background processing did not complete')
            self.page.update_library()
            self.root.update()
            threading.Event().wait(.005)
        self.page.update_library()

    def select_episode(self, episode):
        self.page.refresh()
        key = next(key for key, path in self.page._tree_paths.items() if path == episode)
        self.page.tree.selection_set(key)
        self.page._selection_changed()

    def test_automatic_jobs_are_sequential_deduplicated_and_reported_on_main_thread(self):
        first = make_episode(self.folder, 'episode_1')
        second = make_episode(self.folder, 'episode_2')
        entered = threading.Event()
        release = threading.Event()
        jobs = []
        worker_threads = []
        main_thread = threading.get_ident()

        def work(kind, payload, progress):
            worker_threads.append(threading.get_ident())
            jobs.append(payload['episode'])
            if len(jobs) == 1:
                entered.set()
                release.wait(2)
            progress({'message': '后台整理'})
            return {'status': 'ready_for_review'}

        with patch('jarvis_camera.dataset_page.perform_job', side_effect=work):
            self.assertTrue(self.page.enqueue_episode(first))
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.page.enqueue_episode(first))
            self.assertTrue(self.page.enqueue_episode(second))
            self.assertEqual(jobs, [first])
            release.set()
            self.pump(lambda: not self.page.busy)
        self.assertEqual(jobs, [first, second])
        self.assertTrue(all(identity != main_thread for identity in worker_threads))
        self.assertIn('已自动整理', self.page.status_text.get())

    def test_close_waits_for_existing_job_and_refuses_new_work(self):
        episode = make_episode(self.folder)
        release = threading.Event()
        closed = []
        def work(*_args):
            release.wait(2)
            return {'status': 'ready_for_review'}
        with patch('jarvis_camera.dataset_page.perform_job', side_effect=work):
            self.page.enqueue_episode(episode)
            self.page.request_close(lambda: closed.append(True))
            self.assertEqual(closed, [])
            self.assertFalse(self.page.enqueue_episode(episode / 'other'))
            release.set()
            self.pump(lambda: bool(closed))
            self.page.update_library()
        self.assertEqual(closed, [True])

    def test_export_and_conversion_do_not_start_during_recording(self):
        self.app.pages['record'].busy = True
        with patch('jarvis_camera.dataset_page.filedialog.askdirectory') as dialog:
            self.page._export()
            self.page._convert()
            dialog.assert_not_called()
        self.assertFalse(self.page.exporting)

    def test_preparation_waits_until_recording_finishes(self):
        episode = make_episode(self.folder)
        self.app.pages['record'].busy = True
        with patch('jarvis_camera.dataset_page.perform_job', return_value={'status': 'ready_for_review'}) as worker:
            self.page.enqueue_episode(episode)
            worker.assert_not_called()
            self.app.pages['record'].busy = False
            self.pump(lambda: not self.page.busy)
            self.assertEqual(worker.call_count, 1)

    def test_review_is_never_prechecked_or_submitted_partially(self):
        episode = make_episode(self.folder)
        self.select_episode(episode)
        self.page._view_episode = episode
        self.page._rows = [{'timestamp': 0}]
        self.assertFalse(any(v.get() for v in self.page.review_vars.values()))
        with patch.object(self.page, '_enqueue') as enqueue:
            self.page.review_vars['alignment'].set(True)
            self.page.review_vars['stable_end'].set(True)
            self.page._approve()
            enqueue.assert_not_called()
            self.page.review_vars['position_targets'].set(True)
            self.page._approve()
            args = enqueue.call_args.args
            self.assertEqual(args[0], 'approve')
            self.assertEqual(args[1]['episode'], episode)
            self.assertTrue(all(args[1]['checks'].values()))

    def test_export_requires_three_reviewed_samples_and_uses_all_candidates(self):
        episodes = [make_episode(self.folder, f'episode_{i}', reviewed=True) for i in range(2)]
        with patch('jarvis_camera.dataset_page.messagebox.showinfo') as info:
            self.page._export()
            info.assert_called_once()
        episodes.append(make_episode(self.folder, 'episode_2', reviewed=True))
        make_episode(self.folder, 'not_reviewed')
        with patch('jarvis_camera.dataset_page.filedialog.askdirectory', return_value=str(self.folder)), \
             patch.object(self.page, '_enqueue') as enqueue:
            self.page._export()
        payload = enqueue.call_args.args[1]
        self.assertEqual(set(payload['episodes']), set(episodes))
        self.assertFalse(payload['output'].exists())

    def test_exporting_property_tracks_only_export_or_convert(self):
        self.page._active = {'kind': 'prepare'}
        self.assertFalse(self.page.exporting)
        self.page._active = {'kind': 'export'}
        self.assertTrue(self.page.exporting)
        self.page._active = None
        self.page._jobs.append({'kind': 'convert'})
        self.assertTrue(self.page.exporting)
        self.page._jobs.clear()

    def test_blocked_preparation_shows_error_before_warning(self):
        self.page._completed({'kind': 'prepare', 'payload': {'episode': self.folder}},
                             {'status': 'blocked', 'issues': [
                                 {'severity': 'warning', 'message': '仅记录主机接收时间'},
                                 {'severity': 'error', 'message': '夹爪反馈缺失'}]})
        self.assertIn('夹爪反馈缺失', self.page.status_text.get())
        self.assertNotIn('仅记录主机接收时间', self.page.status_text.get())

    @unittest.skipUnless(os.environ.get('JARVIS_RUN_VISIBLE_UI_TESTS') == '1',
                         'visible UI checks are opt-in; use a dedicated test desktop')
    def test_mapped_window_loads_images_and_handles_real_tk_events(self):
        """Exercise Tcl widget-name resolution, layout events and delayed drawing."""
        callback_errors = []
        self.root.report_callback_exception = lambda kind, error, trace: callback_errors.append(error)
        self.assertIs(self.page._root(), self.root)
        self.assertIs(self.page.nametowidget(str(self.page.tree)), self.page.tree)
        self.assertIs(self.page.slider._root(), self.root)

        episode = make_episode(self.folder)
        for camera in ('top', 'wrist'):
            Image.new('RGB', (80, 60), color='navy').save(episode / f'{camera}.jpg')
        rows = [{'timestamp': index * .1,
                 'observation.images.top': 'top.jpg', 'observation.images.wrist': 'wrist.jpg',
                 'observation.state': [0.] * 7 + [.2], 'action': [.01] * 7 + [.4]}
                for index in range(2)]
        self.page.pack(fill='both', expand=True)
        self.root.geometry('1000x650')
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.page.winfo_ismapped())
        self.select_episode(episode)
        self.page.tree.event_generate('<<TreeviewSelect>>', when='tail')
        self.root.update()

        with patch('jarvis_camera.dataset_page.perform_job',
                   return_value=({'task': '把红色积木放到框里'}, rows)):
            self.page._load_selected()
            self.pump(lambda: not self.page.busy and set(self.page._photos) == {'top', 'wrist'})
        self.page.slider.set(1)
        self.page.slider.event_generate('<ButtonRelease-1>', x=30, y=10, when='tail')
        self.root.geometry('900x600')
        self.pump(lambda: self.page.sample_text.get().startswith('0.10'))
        self.root.update()
        self.assertIn('40.0%', self.page.sample_text.get())
        self.assertLessEqual(int(self.page.sample_label.cget('wraplength')),
                             self.page.sample_label.winfo_width())
        self.assertEqual(callback_errors, [])


if __name__ == '__main__':
    unittest.main()
