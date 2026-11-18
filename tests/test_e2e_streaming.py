"""
Comprehensive end-to-end tests for LiveExtract streaming extraction engine.
Tests Mode A (Manifest), Mode B (HTTP Range Direct), Bitwise Integrity, and Resumption.
"""

import unittest
import tempfile
import shutil
import zipfile
from pathlib import Path
from tests.make_test_archive import write_sample_zip_file
from tests.mock_http_handler import create_mock_opener
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient, StreamProgress


class TestE2EStreaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage_dir = Path(tempfile.mkdtemp())
        cls.zip_file = cls.storage_dir / "game_package.zip"
        write_sample_zip_file(cls.zip_file, num_files=6, file_size_kb=32)
        cls.opener = create_mock_opener(cls.storage_dir)
        cls.archive_url = "mock://storage/game_package.zip"

        # Baseline: Extract ground truth using Python zipfile
        cls.ground_truth_dir = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(cls.zip_file, "r") as zf:
            zf.extractall(cls.ground_truth_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.storage_dir, ignore_errors=True)
        shutil.rmtree(cls.ground_truth_dir, ignore_errors=True)

    def _verify_extracted_files(self, extracted_dir: Path):
        """Verify that every file in extracted_dir bitwise matches ground truth."""
        for ref_file in self.ground_truth_dir.rglob("*"):
            rel_path = ref_file.relative_to(self.ground_truth_dir)
            target_file = extracted_dir / rel_path

            self.assertTrue(target_file.exists(), f"Missing extracted item: {rel_path}")
            if ref_file.is_file():
                self.assertTrue(target_file.is_file(), f"Expected file, found dir: {rel_path}")
                self.assertEqual(
                    target_file.read_bytes(),
                    ref_file.read_bytes(),
                    f"Bitwise mismatch in file: {rel_path}",
                )

    def test_mode_a_manifest_driven_stream(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            manifest = generate_manifest_from_zip(self.zip_file)
            client = StreamClient(opener=self.opener)

            progress_events = []
            result = client.extract_from_manifest(
                manifest=manifest,
                archive_source=self.archive_url,
                target_dir=dest_dir,
                progress_cb=progress_events.append,
            )

            self.assertEqual(result["status"], "completed")
            self.assertGreater(len(progress_events), 0)
            self._verify_extracted_files(dest_dir)

            # Confirm 0 bytes of zip archive were stored in target directory
            self.assertFalse((dest_dir / "game_package.zip").exists())
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_mode_b_direct_http_range_stream(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            client = StreamClient(opener=self.opener)
            result = client.extract_from_url(
                archive_source=self.archive_url,
                target_dir=dest_dir,
            )

            self.assertEqual(result["status"], "completed")
            self._verify_extracted_files(dest_dir)
            self.assertFalse((dest_dir / "game_package.zip").exists())
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_mode_b_local_file_stream(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            client = StreamClient()
            result = client.extract_from_url(
                archive_source=str(self.zip_file),
                target_dir=dest_dir,
            )

            self.assertEqual(result["status"], "completed")
            self._verify_extracted_files(dest_dir)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_resumption_after_interruption(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            manifest = generate_manifest_from_zip(self.zip_file)
            client = StreamClient(opener=self.opener, resume=True)

            # 1. Partial run: simulate interruption after 2 files
            call_count = 0

            def interrupt_progress(p: StreamProgress):
                nonlocal call_count
                call_count += 1
                if p.file_index >= 3:
                    raise KeyboardInterrupt("Simulated connection drop")

            try:
                client.extract_from_manifest(
                    manifest=manifest,
                    archive_source=self.archive_url,
                    target_dir=dest_dir,
                    progress_cb=interrupt_progress,
                )
            except KeyboardInterrupt:
                pass  # Simulated network drop

            # Verify state journal exists
            journal_path = dest_dir / ".extract_state.json"
            self.assertTrue(journal_path.exists())

            # 2. Resume extraction
            resumed_result = client.extract_from_manifest(
                manifest=manifest,
                archive_source=self.archive_url,
                target_dir=dest_dir,
            )

            self.assertEqual(resumed_result["status"], "completed")
            self._verify_extracted_files(dest_dir)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_range_batch_coalescing_reduces_http_requests(self):
        """Verify 50 small contiguous files coalesce into a single HTTP Range request instead of 50."""
        temp_storage = Path(tempfile.mkdtemp())
        dest_dir = Path(tempfile.mkdtemp())
        try:
            small_zip = temp_storage / "many_small.zip"
            write_sample_zip_file(small_zip, num_files=50, file_size_kb=2)

            request_count = [0]
            opener = create_mock_opener(temp_storage)
            orig_open = opener.open

            def counting_open(req, *args, **kwargs):
                request_count[0] += 1
                return orig_open(req, *args, **kwargs)

            opener.open = counting_open

            client = StreamClient(max_workers=4, opener=opener)
            res = client.extract_from_url("mock://storage/many_small.zip", dest_dir)
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["total_files"], 53)
            # 1 size + 1 head magic + 1 EOCD tail + 1 CD + 1 coalesced batch = 5 total HTTP requests for 53 files!
            self.assertLessEqual(request_count[0], 6, f"Expected <= 6 HTTP requests for 53 files, got {request_count[0]}")
        finally:
            shutil.rmtree(temp_storage, ignore_errors=True)
            shutil.rmtree(dest_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
