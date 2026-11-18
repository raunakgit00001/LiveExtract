"""
Unit and end-to-end tests for zero-disk 7z.dll COM virtual streaming engine
(supports RAR4, RAR5 Standard & Solid, and 7z over HTTP Range with 0 bytes archive on disk).
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.mock_http_handler import create_mock_opener
from liveextract.client.stream_client import StreamClient
from liveextract.core.rar_extractor import (
    find_rar_extractor,
    find_7z_dll,
    find_bridge_dll,
    is_sevenz,
    SEVENZ_SIGNATURE,
)
from liveextract.core.rar_parser import RarBinaryParser, RAR4_SIGNATURE, RAR5_SIGNATURE
from liveextract.core.verifier import verify_destination


class TestRarAndZeroDiskBridge(unittest.TestCase):
    def test_find_zero_disk_bridge_and_7z_dll(self):
        extractor = find_rar_extractor()
        self.assertIsNotNone(extractor, "Expected 7z.dll or 7z.exe to be located on system")
        self.assertTrue(extractor.exists())
        self.assertIsNotNone(find_7z_dll(), "Expected 7z.dll to be installed")
        self.assertIsNotNone(find_bridge_dll(), "Expected liveextract_7z_bridge.dll to be built")

    def test_magic_byte_detection(self):
        self.assertTrue(RarBinaryParser.is_rar(RAR4_SIGNATURE + b"\x00" * 8))
        self.assertTrue(RarBinaryParser.is_rar(RAR5_SIGNATURE + b"\x00" * 8))
        self.assertTrue(is_sevenz(SEVENZ_SIGNATURE + b"\x00" * 8))
        self.assertFalse(RarBinaryParser.is_rar(b"PK\x03\x04\x14\x00\x00\x00"))
        self.assertFalse(is_sevenz(b"PK\x03\x04\x14\x00\x00\x00"))

    def test_e2e_zero_disk_remote_http_range_7z_com_stream(self):
        """
        End-to-end proof that remote HTTP Range streaming via liveextract_7z_bridge.dll -> 7z.dll
        extracts directly to disk with STRICTLY 0 BYTES of archive/spool file written to disk.
        """
        if not (find_7z_dll() and find_bridge_dll()):
            self.skipTest("7z.dll or bridge DLL not available")

        temp_root = Path(tempfile.mkdtemp())
        try:
            src_dir = temp_root / "source"
            (src_dir / "assets").mkdir(parents=True)
            file_a = b"Zero-disk COM virtual stream text payload!\n" * 200
            file_b = bytes(range(256)) * 400
            (src_dir / "readme.txt").write_bytes(file_a)
            (src_dir / "assets" / "binary.pak").write_bytes(file_b)

            storage_dir = temp_root / "remote_cdn"
            storage_dir.mkdir()
            arc_path = storage_dir / "game_pack.7z"
            subprocess.run(
                [r"C:\Program Files\7-Zip\7z.exe", "a", "-t7z", str(arc_path), str(src_dir / "*")],
                check=True,
                capture_output=True,
            )

            # Serve via mock HTTP Range server (no local file access by client)
            opener = create_mock_opener(storage_dir)
            dest_dir = temp_root / "extracted"

            client = StreamClient(opener=opener)
            res = client.extract_from_url("mock://storage/game_pack.7z", dest_dir)

            self.assertEqual(res["status"], "completed")
            self.assertEqual(res.get("archive_disk_bytes"), 0)

            # Confirm NO .tmp, .7z, or .rar file exists anywhere in dest_dir
            spool_files = list(dest_dir.rglob("*.tmp")) + list(dest_dir.rglob("*.7z")) + list(dest_dir.rglob("*.rar"))
            self.assertEqual(spool_files, [], f"Unexpected temporary archive files on disk: {spool_files}")

            # Confirm bitwise file match
            self.assertEqual((dest_dir / "readme.txt").read_bytes(), file_a)
            self.assertEqual((dest_dir / "assets" / "binary.pak").read_bytes(), file_b)

            # Confirm cryptographic CRC-32 verifier passes
            report = verify_destination(dest_dir)
            self.assertTrue(report.is_valid, f"Verification failed: {report.failure_summary}")
            self.assertEqual(report.total_expected, 2)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
