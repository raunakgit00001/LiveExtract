"""
Unit tests for archive repair and verification integrity.
Verifies that:
- Deleting files is detected as missing.
- When repair fails, state journal preserves expected files and subsequent verification STILL detects missing files.
- When repair succeeds, all files are restored and verified.
"""

import unittest
import tempfile
import shutil
from pathlib import Path

from tests.make_test_archive import write_sample_zip_file
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient
from liveextract.client.state_journal import StateJournal
from liveextract.core.verifier import verify_destination
from liveextract.core.repair import check_link_folder_match, prepare_repair


class TestRepairWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.zip_file = self.temp_root / "test_repair_pkg.zip"
        # 5 distinct files
        write_sample_zip_file(self.zip_file, num_files=5, file_size_kb=32)

        self.dest_dir = self.temp_root / "extracted"
        self.manifest = generate_manifest_from_zip(self.zip_file)

        # Full initial extraction
        client = StreamClient()
        res = client.extract_from_manifest(
            manifest=self.manifest,
            archive_source=str(self.zip_file),
            target_dir=self.dest_dir,
        )
        self.assertEqual(res["status"], "completed")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_repair_failure_keeps_missing_files_detected_by_verify(self):
        """
        Critical regression test:
        1. User deletes 2 files from extracted folder.
        2. Repair is initiated, marking the 2 files missing.
        3. The repair download FAILS (e.g. simulated network abort / dead link).
        4. When the user subsequently runs verify, it MUST STILL detect the missing files!
           It must NEVER falsely report 'all files intact' just because repair failed.
        """
        # Step 1: Delete 2 files
        doc_file = self.dest_dir / "docs" / "readme.txt"
        data_file = self.dest_dir / "data" / "items_0.dat"
        self.assertTrue(doc_file.exists())
        self.assertTrue(data_file.exists())
        doc_file.unlink()
        data_file.unlink()

        # Step 2: Initial verify detects exactly 2 missing files
        report1 = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report1.is_valid)
        self.assertEqual(len(report1.missing_files), 2)

        # Step 3: Run prepare_repair
        is_match, match_res, expected_table = check_link_folder_match(self.dest_dir, str(self.zip_file))
        self.assertTrue(is_match)
        prepare_repair(self.dest_dir, str(self.zip_file), report1, expected_table=expected_table)

        # Journal should now show total_files == 5, with 2 pending/missing
        journal = StateJournal(self.dest_dir)
        self.assertEqual(journal.total_files, 5)

        # Verify that doc_file is NOT completed in the journal
        norm_doc = str(Path("docs/readme.txt")).replace("\\", "/")
        self.assertFalse(journal.is_file_completed(norm_doc, doc_file.stat().st_size if doc_file.exists() else 0, None))

        # Step 4: Simulate FAILED repair (e.g. broken URL or network exception)
        # We do NOT download the files.
        # Now verify destination again (even if fallback without URL or with URL)
        report2 = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report2.is_valid, "Verify must NOT report valid after a failed repair!")
        self.assertEqual(len(report2.missing_files), 2)
        missing_normalized = [p.replace("\\", "/") for p in report2.missing_files]
        self.assertTrue(any("docs/readme.txt" in p for p in missing_normalized))
        self.assertTrue(any("data/items_0.dat" in p for p in missing_normalized))

        # Step 5: Test fallback verify without archive_source (only using .extract_state.json)
        report_fallback = verify_destination(self.dest_dir, archive_source=None)
        self.assertFalse(report_fallback.is_valid, "Fallback verify must detect missing files from state file!")
        self.assertEqual(len(report_fallback.missing_files), 2)

    def test_full_successful_repair(self):
        """Verify that repairing successfully restores all files and verifies 100%."""
        doc_file = self.dest_dir / "docs" / "readme.txt"
        doc_file.unlink()

        report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertFalse(report.is_valid)

        is_match, match_res, expected_table = check_link_folder_match(self.dest_dir, str(self.zip_file))
        self.assertTrue(is_match)
        prepare_repair(self.dest_dir, str(self.zip_file), report, expected_table=expected_table)

        client = StreamClient(resume=True)
        res = client.extract_from_manifest(
            manifest=self.manifest,
            archive_source=str(self.zip_file),
            target_dir=self.dest_dir,
        )
        self.assertEqual(res["status"], "completed")

        # After repair, verify must pass 100%
        final_report = verify_destination(self.dest_dir, archive_source=str(self.zip_file))
        self.assertTrue(final_report.is_valid)
        self.assertEqual(len(final_report.missing_files), 0)
        self.assertEqual(len(final_report.corrupted_files), 0)
        self.assertTrue(doc_file.exists())


if __name__ == "__main__":
    unittest.main()
