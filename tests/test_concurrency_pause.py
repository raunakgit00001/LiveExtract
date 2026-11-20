"""
Unit tests for multi-worker concurrency and live Pause/Resume/Cancel control.
"""

import unittest
import threading
import tempfile
import shutil
import time
from pathlib import Path
from tests.make_test_archive import write_sample_zip_file
from tests.mock_http_handler import create_mock_opener
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient, StreamProgress
from liveextract.client.controller import StreamController


class TestConcurrencyAndPause(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage_dir = Path(tempfile.mkdtemp())
        cls.zip_file = cls.storage_dir / "large_package.zip"
        # 12 files to give plenty of parallel work
        write_sample_zip_file(cls.zip_file, num_files=12, file_size_kb=64)
        cls.opener = create_mock_opener(cls.storage_dir)
        cls.archive_url = "mock://storage/large_package.zip"
        cls.manifest = generate_manifest_from_zip(cls.zip_file)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.storage_dir, ignore_errors=True)

    def test_concurrent_extraction_workers_4(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            client = StreamClient(max_workers=4, opener=self.opener)
            result = client.extract_from_manifest(
                manifest=self.manifest,
                archive_source=self.archive_url,
                target_dir=dest_dir,
            )
            self.assertEqual(result["status"], "completed")

            # Check that all files exist and have non-zero size
            for entry in self.manifest.files:
                if not entry.is_dir and entry.uncompressed_size > 0:
                    target_file = dest_dir / entry.path.replace("/", "\\")
                    self.assertTrue(target_file.exists())
                    self.assertEqual(target_file.stat().st_size, entry.uncompressed_size)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_interactive_pause_and_resume(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            controller = StreamController()
            client = StreamClient(opener=self.opener, controller=controller)

            run_result = {}
            error_holder = []
            paused_once = False

            def on_prog(p):
                nonlocal paused_once
                if not paused_once:
                    paused_once = True
                    controller.pause()

            def run_worker():
                try:
                    res = client.extract_from_manifest(
                        manifest=self.manifest,
                        archive_source=self.archive_url,
                        target_dir=dest_dir,
                        progress_cb=on_prog,
                    )
                    run_result.update(res)
                except Exception as ex:
                    error_holder.append(ex)

            worker_thread = threading.Thread(target=run_worker)
            worker_thread.start()

            # Wait until paused
            for _ in range(50):
                if controller.is_paused:
                    break
                time.sleep(0.01)

            self.assertTrue(controller.is_paused)
            self.assertEqual(controller.status, "paused")

            # Wait while paused - verify thread has not completed
            time.sleep(0.05)
            self.assertTrue(worker_thread.is_alive())

            # Resume
            controller.resume()
            self.assertFalse(controller.is_paused)
            self.assertEqual(controller.status, "running")

            worker_thread.join(timeout=5.0)
            self.assertFalse(worker_thread.is_alive())
            self.assertEqual(run_result.get("status"), "completed")
            self.assertEqual(len(error_holder), 0)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_cancellation(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            controller = StreamController()
            client = StreamClient(opener=self.opener, controller=controller)

            def run_worker():
                client.extract_from_manifest(
                    manifest=self.manifest,
                    archive_source=self.archive_url,
                    target_dir=dest_dir,
                )

            worker_thread = threading.Thread(target=run_worker)
            worker_thread.start()

            time.sleep(0.02)
            controller.cancel()
            self.assertTrue(controller.is_cancelled)

            worker_thread.join(timeout=3.0)
            self.assertFalse(worker_thread.is_alive())
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
