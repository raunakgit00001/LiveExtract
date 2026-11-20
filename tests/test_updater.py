"""
Unit tests for smart differential update engine.
"""

import unittest
import tempfile
import shutil
from pathlib import Path

from tests.make_test_archive import write_sample_zip_file
from liveextract.core.manifest import generate_manifest_from_zip
from liveextract.client.stream_client import StreamClient
from liveextract.core.updater import check_for_updates, apply_update_plan


class TestDifferentialUpdater(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(tempfile.mkdtemp())
        self.dest_dir = self.temp_root / "installed_app"

        # Create v1 archive with 4 files
        self.v1_zip = self.temp_root / "app_v1.zip"
        write_sample_zip_file(self.v1_zip, num_files=4, file_size_kb=32)
        v1_manifest = generate_manifest_from_zip(self.v1_zip)

        # Install v1
        client = StreamClient()
        res = client.extract_from_manifest(
            manifest=v1_manifest,
            archive_source=str(self.v1_zip),
            target_dir=self.dest_dir,
        )
        self.assertEqual(res["status"], "completed")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_check_for_updates_when_already_up_to_date(self):
        # Checking against v1 again should report 0 updates
        plan = check_for_updates(self.dest_dir, str(self.v1_zip))
        self.assertFalse(plan.has_updates)
        self.assertEqual(len(plan.modified_files), 0)
        self.assertEqual(len(plan.new_files), 0)
        self.assertEqual(len(plan.unchanged_files), 4)
        self.assertEqual(plan.total_download_bytes, 0)
        self.assertEqual(plan.bandwidth_saved_pct, 100.0)

    def test_check_and_apply_differential_update(self):
        # Create v2 archive with:
        # - 3 identical files
        # - 1 modified file
        # - 1 brand new file
        self.v2_zip = self.temp_root / "app_v2.zip"
        # Write 5 files (some with different payload)
        import zipfile
        with zipfile.ZipFile(self.v2_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            # 3 unchanged files matching v1
            zf.writestr("docs/readme.txt", b"Mock content for file 0" * 200)
            zf.writestr("data/items_0.dat", b"Mock content for file 1" * 200)
            zf.writestr("data/items_1.dat", b"Mock content for file 2" * 200)
            # 1 modified file (different content)
            zf.writestr("bin/app.cfg", b"MODIFIED_V2_CONFIGURATION_DATA" * 50)
            # 1 brand new file
            zf.writestr("bin/patch.dll", b"BRAND_NEW_V2_BINARY_PAYLOAD" * 100)

        plan = check_for_updates(self.dest_dir, str(self.v2_zip))
        self.assertTrue(plan.has_updates)
        self.assertGreater(len(plan.modified_files) + len(plan.new_files), 0)
        self.assertGreater(plan.bandwidth_saved_pct, 0.0)

        # Apply update
        res = apply_update_plan(plan, target_dir=self.dest_dir, remove_obsolete=True)
        self.assertEqual(res["status"], "completed")

        # Verify that patch.dll exists and app.cfg has new content
        patch_file = self.dest_dir / "bin" / "patch.dll"
        cfg_file = self.dest_dir / "bin" / "app.cfg"
        self.assertTrue(patch_file.exists())
        self.assertTrue(cfg_file.exists())
        self.assertIn(b"MODIFIED_V2", cfg_file.read_bytes())


if __name__ == "__main__":
    unittest.main()
