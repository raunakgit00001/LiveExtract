"""
Unit tests for cryptographic verifier and integrity checker.
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

    def test_verify_detects_same_size_zero_padded_corruption(self):
        # Corrupt file with identical file size (e.g. pre-allocated zero bytes or bit flip)
        cfg_file = self.dest_dir / "bin" / "app.cfg"
        orig_size = cfg_file.stat().st_size
        cfg_file.write_bytes(b"\x00" * orig_size)

        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report.is_valid)
        corrupted_paths = [c[0].replace("\\", "/") for c in report.corrupted_files]
        self.assertTrue(any("bin/app.cfg" in p for p in corrupted_paths))

    def test_repair_and_resume_corrupted_file(self):
        from liveextract.client.state_journal import StateJournal
        cfg_file = self.dest_dir / "bin" / "app.cfg"
        orig_size = cfg_file.stat().st_size
        cfg_file.write_bytes(b"\x00" * orig_size)

        # Verification fails
        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report.is_valid)

        # Repair workflow: invalidate corrupted file and unlink
        journal = StateJournal(self.dest_dir, str(self.zip_file))
        for p, _ in report.corrupted_files:
            journal.invalidate_file(p)
            (self.dest_dir / Path(p)).unlink(missing_ok=True)

        # Re-extract with resume=True
        client = StreamClient(resume=True)
        res = client.extract_from_manifest(
            manifest=self.manifest,
            archive_source=str(self.zip_file),
            target_dir=self.dest_dir,
        )
        self.assertEqual(res["status"], "completed")

        # Now verification passes completely
        report2 = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertTrue(report2.is_valid)
        self.assertEqual(len(report2.corrupted_files), 0)
        self.assertEqual(len(report2.missing_files), 0)

    def test_verify_fallback_without_state_file(self):
        # Remove state journal
        state_f = self.dest_dir / ".extract_state.json"
        if state_f.exists():
            state_f.unlink()

        # Should parse archive Central Directory directly and verify
        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertTrue(report.is_valid)
        self.assertEqual(report.total_expected, self.manifest.total_files)


if __name__ == "__main__":
    unittest.main()
