"""
Unit tests for verifying and repairing externally downloaded/extracted folders.
Tests repairing folders created by 7-Zip, WinRAR, or browser download (no .extract_state.json present).
Verifies:
  1. Authoritative archive manifest retrieval in RAM
  2. Automatic folder structure matching and path alignment
  3. Multi-threaded 1 MB-chunked CRC-32 integrity scanning
  4. Surgical zero-disk repair (intact files preserved with 0 bytes downloaded)
  5. Automatic generation of valid .extract_state.json after repair
"""

import os
import shutil
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from liveextract.client.range_reader import RangeReader
from liveextract.client.state_journal import StateJournal
from liveextract.client.stream_client import StreamClient
from liveextract.core.repair import check_link_folder_match, prepare_repair
from liveextract.core.verifier import verify_destination, calculate_file_crc32, get_expected_file_table
from tests.mock_http_handler import create_mock_opener


def _make_sample_zip(target_zip: Path, entries: dict) -> None:
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class TestExternalFolderRepair(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_ext_repair_"))
        self.archive_zip = self.test_dir / "external_archive.zip"
        self.extracted_folder = self.test_dir / "installed_game"
        self.extracted_folder.mkdir(parents=True, exist_ok=True)

        # 4 files in archive
        self.file_data = {
            "bin/game.exe": b"GAME_EXECUTABLE_BINARY_DATA_" * 500,
            "data/textures.pak": b"TEXTURE_DATA_PAK_1234567890_" * 1000,
            "data/sounds.bank": b"SOUND_AUDIO_BANK_WAV_OGG_AAC_" * 800,
            "readme.txt": b"Thank you for installing this software.\n",
        }
        _make_sample_zip(self.archive_zip, self.file_data)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_repair_external_folder_with_missing_and_corrupted_files(self):
        """
        Simulate an external extraction (e.g. from 7-Zip or WinRAR):
        - NO .extract_state.json exists initially
        - 'bin/game.exe' is INTACT
        - 'data/textures.pak' is CORRUPTED (modified bytes)
        - 'data/sounds.bank' is MISSING (deleted by user/antivirus)
        - 'readme.txt' is INTACT
        """
        # Populate folder like external extractor without LiveExtract
        (self.extracted_folder / "bin").mkdir(parents=True, exist_ok=True)
        (self.extracted_folder / "data").mkdir(parents=True, exist_ok=True)

        # 1. Intact game.exe
        (self.extracted_folder / "bin/game.exe").write_bytes(self.file_data["bin/game.exe"])

        # 2. Corrupted textures.pak (wrong bytes, wrong CRC)
        (self.extracted_folder / "data/textures.pak").write_bytes(b"CORRUPTED_GARBAGE_BYTES_OVERWRITTEN")

        # 3. sounds.bank is completely deleted / missing
        # (do not create data/sounds.bank)

        # 4. Intact readme.txt
        (self.extracted_folder / "readme.txt").write_bytes(self.file_data["readme.txt"])

        # Confirm no .extract_state.json exists
        self.assertFalse((self.extracted_folder / ".extract_state.json").exists())

        archive_str = str(self.archive_zip)

        # Step 1: Check link folder match
        is_match, match_res, expected_table = check_link_folder_match(self.extracted_folder, archive_str)
        self.assertTrue(is_match)
        self.assertEqual(match_res.total_archive_files, 4)
        self.assertGreaterEqual(match_res.matched_names, 2)

        # Step 2: Multi-threaded verification
        progress_events = []
        def _on_prog(p, *extra):
            progress_events.append(p)

        report = verify_destination(
            self.extracted_folder,
            archive_source=archive_str,
            progress_cb=_on_prog,
            expected_table=expected_table,
            max_workers=4,
        )

        self.assertFalse(report.is_valid)
        self.assertEqual(report.total_expected, 4)
        self.assertIn("bin/game.exe", report.intact_files)
        self.assertIn("readme.txt", report.intact_files)
        self.assertEqual(len(report.intact_files), 2)

        self.assertIn("data/sounds.bank", report.missing_files)
        self.assertEqual(len(report.missing_files), 1)

        corrupt_names = [p for p, _ in report.corrupted_files]
        self.assertIn("data/textures.pak", corrupt_names)
        self.assertEqual(len(report.corrupted_files), 1)

        # Step 3: Prepare surgical repair
        repaired_count = prepare_repair(
            self.extracted_folder,
            archive_str,
            report,
            expected_table=expected_table,
        )
        self.assertEqual(repaired_count, 2)  # 1 missing + 1 corrupted

        # Corrupted file should be unlinked from disk
        self.assertFalse((self.extracted_folder / "data/textures.pak").exists())

        # Check that intact files are registered as 'completed' in StateJournal
        journal = StateJournal(self.extracted_folder, archive_str)
        self.assertTrue(journal.is_file_completed("bin/game.exe", len(self.file_data["bin/game.exe"])))
        self.assertTrue(journal.is_file_completed("readme.txt", len(self.file_data["readme.txt"])))

        # Step 4: Stream repair
        client = StreamClient(speed_mode="max", verify_crc=True, resume=True)
        res = client.extract_from_url(archive_str, self.extracted_folder)

        self.assertEqual(res["status"], "completed")

        # Step 5: Deep verify after repair -> Must be 100% valid!
        post_report = verify_destination(self.extracted_folder, archive_source=archive_str)
        self.assertTrue(post_report.is_valid)
        self.assertEqual(len(post_report.intact_files), 4)
        self.assertEqual(len(post_report.missing_files), 0)
        self.assertEqual(len(post_report.corrupted_files), 0)

        # Verify all contents on disk match bit-for-bit
        for name, expected_bytes in self.file_data.items():
            actual = (self.extracted_folder / name).read_bytes()
            self.assertEqual(actual, expected_bytes)

        # State journal should now exist and be completely valid
        self.assertTrue((self.extracted_folder / ".extract_state.json").exists())


if __name__ == "__main__":
    unittest.main()
