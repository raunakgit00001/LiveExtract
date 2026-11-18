"""
Unit tests for safe path resolution and disk pre-allocation.
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from liveextract.core.disk_allocator import (
    DiskAllocator,
    PathSecurityError,
)


class TestDiskAllocator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.allocator = DiskAllocator(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_path_resolution(self):
        p = self.allocator.sanitize_path("data/sub/file.txt")
        self.assertEqual(p, self.temp_dir / "data" / "sub" / "file.txt")

    def test_directory_traversal_blocked(self):
        with self.assertRaises(PathSecurityError):
            self.allocator.sanitize_path("../../escape.txt")

        with self.assertRaises(PathSecurityError):
            self.allocator.sanitize_path("data/../../../etc/passwd")

    def test_pre_allocation(self):
        manifest_files = [
            {"path": "assets/models/", "is_dir": True, "uncompressed_size": 0},
            {"path": "assets/models/player.mesh", "is_dir": False, "uncompressed_size": 1024},
            {"path": "config.ini", "is_dir": False, "uncompressed_size": 128},
        ]
        allocated = self.allocator.pre_allocate_manifest(manifest_files, verify_space=True)

        mesh_path = allocated["assets/models/player.mesh"]
        self.assertTrue(mesh_path.exists())
        self.assertEqual(mesh_path.stat().st_size, 1024)

        cfg_path = allocated["config.ini"]
        self.assertTrue(cfg_path.exists())
        self.assertEqual(cfg_path.stat().st_size, 128)


if __name__ == "__main__":
    unittest.main()
