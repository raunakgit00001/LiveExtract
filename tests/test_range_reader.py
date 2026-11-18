"""
Integration tests for RangeReader with byte-range requests and local files.
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from tests.make_test_archive import write_sample_zip_file
from tests.mock_http_handler import create_mock_opener
from liveextract.client.range_reader import RangeReader


class TestRangeReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp())
        cls.zip_file = cls.temp_dir / "test_archive.zip"
        write_sample_zip_file(cls.zip_file, num_files=4, file_size_kb=16)
        cls.zip_bytes = cls.zip_file.read_bytes()

        # In-memory mock opener with byte-range capability
        cls.opener = create_mock_opener(cls.temp_dir)
        cls.base_url = "mock://storage/test_archive.zip"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_get_total_size(self):
        reader = RangeReader(self.base_url, opener=self.opener)
        total_size = reader.get_total_size()
        self.assertEqual(total_size, len(self.zip_bytes))

    def test_read_range(self):
        reader = RangeReader(self.base_url, opener=self.opener)
        # Read byte range 10 to 50
        slice_data = reader.read_range(10, 50)
        self.assertEqual(slice_data, self.zip_bytes[10:51])

    def test_read_tail(self):
        reader = RangeReader(self.base_url, opener=self.opener)
        tail_data, total = reader.read_tail(128)
        self.assertEqual(total, len(self.zip_bytes))
        self.assertEqual(tail_data, self.zip_bytes[-128:])

    def test_local_file_reading(self):
        reader = RangeReader(str(self.zip_file))
        slice_data = reader.read_range(20, 60)
        self.assertEqual(slice_data, self.zip_bytes[20:61])


if __name__ == "__main__":
    unittest.main()
