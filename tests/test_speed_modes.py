"""
Unit tests for Max Speed vs Optimized streaming profiles, adaptive batching,
and high-throughput sliding RAM cache prefetching.
"""

import tempfile
import shutil
import unittest
from pathlib import Path

from tests.make_test_archive import write_sample_zip_file
from tests.mock_http_handler import create_mock_opener
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient
from liveextract.client.range_reader import RangeReader
from liveextract.core.rar_extractor import RamSlidingRangeCache
from liveextract.cli.main import build_parser


class TestSpeedModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage_dir = Path(tempfile.mkdtemp())
        cls.zip_file = cls.storage_dir / "speed_test.zip"
        write_sample_zip_file(cls.zip_file, num_files=8, file_size_kb=64)
        cls.opener = create_mock_opener(cls.storage_dir)
        cls.archive_url = "mock://storage/speed_test.zip"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.storage_dir, ignore_errors=True)

    def test_client_max_speed_defaults(self):
        client = StreamClient(speed_mode="max")
        self.assertEqual(client.speed_mode, "max")
        self.assertEqual(client.chunk_size, 1048576)  # Upgraded to 1MB buffer

    def test_client_optimized_defaults(self):
        client = StreamClient(speed_mode="optimized")
        self.assertEqual(client.speed_mode, "optimized")
        self.assertEqual(client.chunk_size, 262144)   # 256KB buffer

    def test_max_speed_extraction_execution(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            client = StreamClient(speed_mode="max", opener=self.opener)
            result = client.extract_from_url(self.archive_url, dest_dir)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["uncompressed_bytes"] > 0)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_optimized_extraction_execution(self):
        dest_dir = Path(tempfile.mkdtemp())
        try:
            client = StreamClient(speed_mode="optimized", opener=self.opener)
            result = client.extract_from_url(self.archive_url, dest_dir)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["uncompressed_bytes"] > 0)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_ram_sliding_cache_speed_modes(self):
        reader = RangeReader(str(self.zip_file))
        total_size = reader.get_total_size()

        # Test Max Speed cache initialization
        cache_max = RamSlidingRangeCache(
            reader=reader,
            total_size=total_size,
            speed_mode="max",
        )
        try:
            chunk = cache_max.read_at(0, 100)
            self.assertEqual(len(chunk), 100)
            self.assertTrue(cache_max.block_size >= 4 * 1024 * 1024)
        finally:
            cache_max.close()

        # Test Optimized cache initialization
        cache_opt = RamSlidingRangeCache(
            reader=reader,
            total_size=total_size,
            speed_mode="optimized",
        )
        try:
            chunk2 = cache_opt.read_at(0, 50)
            self.assertEqual(len(chunk2), 50)
        finally:
            cache_opt.close()
            reader.close()

    def test_cli_parser_speed_mode_arguments(self):
        parser = build_parser()

        # Test stream subcommand
        args_stream = parser.parse_args(["stream", "http://example.com/test.zip", "-d", "./out", "--mode", "max"])
        self.assertEqual(args_stream.speed_mode, "max")

        args_stream_opt = parser.parse_args(["stream", "http://example.com/test.zip", "-d", "./out", "--speed-mode", "optimized"])
        self.assertEqual(args_stream_opt.speed_mode, "optimized")

        # Test repair subcommand
        args_rep = parser.parse_args(["repair", "./out", "--mode", "max"])
        self.assertEqual(args_rep.speed_mode, "max")

        # Test update subcommand
        args_upd = parser.parse_args(["update", "./out", "--mode", "optimized"])
        self.assertEqual(args_upd.speed_mode, "optimized")


if __name__ == "__main__":
    unittest.main()
