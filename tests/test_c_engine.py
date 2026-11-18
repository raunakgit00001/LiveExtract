"""
Unit tests for NativeCDecompressor ctypes bridge.
"""

import unittest
import zlib
from liveextract.core.c_engine import NativeCDecompressor


class TestNativeCEngine(unittest.TestCase):
    def test_native_c_available(self):
        self.assertTrue(NativeCDecompressor.is_available(), "liveextract_core.dll should be loaded")

    def test_native_c_decompression_matches_python(self):
        if not NativeCDecompressor.is_available():
            self.skipTest("Native C library not available")

        # 100 KB payload
        original_data = (b"Game asset mesh data chunk 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ! " * 2000)
        expected_crc = zlib.crc32(original_data) & 0xFFFFFFFF

        comp_obj = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
        compressed = comp_obj.compress(original_data) + comp_obj.flush()

        decompressor = NativeCDecompressor()
        chunks = []
        chunk_size = 256
        for i in range(0, len(compressed), chunk_size):
            slice_data = compressed[i:i + chunk_size]
            out = decompressor.decompress_chunk(slice_data)
            if out:
                chunks.append(out)

        final = decompressor.flush()
        if final:
            chunks.append(final)

        reconstructed = b"".join(chunks)
        self.assertEqual(len(reconstructed), len(original_data))
        self.assertEqual(reconstructed, original_data)
        self.assertEqual(zlib.crc32(reconstructed) & 0xFFFFFFFF, expected_crc)


if __name__ == "__main__":
    unittest.main()
