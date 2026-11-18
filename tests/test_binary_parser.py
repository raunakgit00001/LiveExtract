"""
Unit tests for ZIP binary parsing (EOCD, Central Directory, Local File Headers).
"""

import unittest
import zipfile
import io
from tests.make_test_archive import create_sample_zip_bytes
from liveextract.core.binary_parser import (
    ZipBinaryParser,
    ZipFormatError,
    EOCD_SIGNATURE,
)


class TestZipBinaryParser(unittest.TestCase):
    def setUp(self):
        self.zip_bytes = create_sample_zip_bytes(num_files=3, file_size_kb=8)
        self.archive_size = len(self.zip_bytes)

    def test_find_eocd_success(self):
        # Pass the tail 65KB (or full archive)
        eocd = ZipBinaryParser.find_eocd(self.zip_bytes[-4096:], archive_size=self.archive_size)
        self.assertIsNotNone(eocd)
        self.assertGreater(eocd.total_records, 0)
        self.assertGreater(eocd.cd_size, 0)
        self.assertGreater(eocd.cd_offset, 0)

    def test_find_eocd_corrupted_raises(self):
        corrupted_bytes = b"random meaningless data without zip signature"
        with self.assertRaises(ZipFormatError):
            ZipBinaryParser.find_eocd(corrupted_bytes, archive_size=len(corrupted_bytes))

    def test_parse_central_directory(self):
        eocd = ZipBinaryParser.find_eocd(self.zip_bytes, archive_size=self.archive_size)
        cd_bytes = self.zip_bytes[eocd.cd_offset:eocd.cd_offset + eocd.cd_size]
        entries = ZipBinaryParser.parse_central_directory(cd_bytes)

        self.assertEqual(len(entries), eocd.total_records)
        filenames = [e.filename for e in entries]
        self.assertIn("docs/readme.txt", filenames)
        self.assertIn("docs/license.txt", filenames)
        self.assertIn("bin/app.cfg", filenames)

    def test_parse_local_header_and_raw_offset(self):
        eocd = ZipBinaryParser.find_eocd(self.zip_bytes, archive_size=self.archive_size)
        cd_bytes = self.zip_bytes[eocd.cd_offset:eocd.cd_offset + eocd.cd_size]
        entries = ZipBinaryParser.parse_central_directory(cd_bytes)

        # Check a non-directory entry
        file_entry = next(e for e in entries if not e.is_dir)
        local_hdr_buf = self.zip_bytes[file_entry.local_header_offset:file_entry.local_header_offset + 512]
        local_hdr = ZipBinaryParser.parse_local_header(local_hdr_buf, file_entry.local_header_offset)

        self.assertEqual(local_hdr.filename, file_entry.filename)
        self.assertEqual(local_hdr.compression_method, file_entry.compression_method)
        self.assertEqual(local_hdr.crc32, file_entry.crc32)
        self.assertGreater(local_hdr.raw_data_offset, file_entry.local_header_offset)

    def test_zip64_extra_field_resolution(self):
        import struct
        # Build ZIP64 extra field (ID 0x0001) with 64-bit uncompressed, compressed, and offset > 4GB
        unp_64 = 5 * 1024 * 1024 * 1024  # 5 GB
        cmp_64 = 2 * 1024 * 1024 * 1024  # 2 GB
        off_64 = 6 * 1024 * 1024 * 1024  # 6 GB offset
        extra = struct.pack("<HHQQQ", 0x0001, 24, unp_64, cmp_64, off_64)
        u, c, o, d = ZipBinaryParser._parse_zip64_extra(extra, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        self.assertEqual(u, unp_64)
        self.assertEqual(c, cmp_64)
        self.assertEqual(o, off_64)
        self.assertEqual(d, 0)


if __name__ == "__main__":
    unittest.main()
