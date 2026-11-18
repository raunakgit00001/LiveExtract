"""
Unit tests for cryptographic integrity verifier (missing file & corruption detection).
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from tests.make_test_archive import write_sample_zip_file
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient
from liveextract.core.verifier import verify_destination


class TestVerifier(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.zip_file = self.temp_root / "test_verify.zip"
        write_sample_zip_file(self.zip_file, num_files=4, file_size_kb=16)

        self.dest_dir = self.temp_root / "extracted"
        self.manifest = generate_manifest_from_zip(self.zip_file)

        # Extract using client
        client = StreamClient()
        client.extract_from_manifest(
            manifest=self.manifest,
            archive_source=str(self.zip_file),
            target_dir=self.dest_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_verify_all_intact(self):
        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertTrue(report.is_valid)
        self.assertEqual(len(report.missing_files), 0)
        self.assertEqual(len(report.corrupted_files), 0)
        self.assertGreater(report.total_expected, 0)

    def test_verify_detects_deleted_file(self):
        # Manually delete docs/readme.txt
        target_file = self.dest_dir / "docs" / "readme.txt"
        self.assertTrue(target_file.exists())
        target_file.unlink()

        # Run verification
        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report.is_valid)
        self.assertEqual(len(report.missing_files), 1)
        self.assertIn("docs/readme.txt", report.missing_files[0].replace("\\", "/"))

    def test_verify_detects_corrupted_file(self):
        # Corrupt data in bin/app.cfg
        cfg_file = self.dest_dir / "bin" / "app.cfg"
        self.assertTrue(cfg_file.exists())
        cfg_file.write_bytes(b"corrupted bytes different from original")

        # Run verification
        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report.is_valid)
        self.assertGreater(len(report.corrupted_files), 0)
        corrupted_paths = [c[0].replace("\\", "/") for c in report.corrupted_files]
        self.assertTrue(any("bin/app.cfg" in p for p in corrupted_paths))


if __name__ == "__main__":
    unittest.main()
