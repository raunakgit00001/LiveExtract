"""
Unit and integration tests for streaming decompression codecs (STORED, DEFLATE, BZIP2, LZMA, ZSTD, XZ)
and lockstep CRC-32 verification.
"""

import bz2
import lzma
import shutil
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from liveextract.core.decompressor import (
    RawDeflateStreamDecompressor,
    StoredStreamDecompressor,
    ZstdStreamDecompressor,
    Bzip2StreamDecompressor,
    XzStreamDecompressor,
    create_decompressor,
    DecompressionError,
)
from liveextract.core.checksum import StreamingCRC32
from liveextract.client.stream_client import StreamClient


class TestStreamingDecompressor(unittest.TestCase):
    def test_raw_deflate_streaming_small_chunks(self):
        original_data = b"The quick brown fox jumps over the lazy dog. 1234567890! " * 4000
        expected_crc = zlib.crc32(original_data) & 0xFFFFFFFF

        comp_obj = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
        compressed_stream = comp_obj.compress(original_data) + comp_obj.flush()

        decompressor = RawDeflateStreamDecompressor()
        crc_tracker = StreamingCRC32()
        decompressed_chunks = []

        chunk_size = 128
        for i in range(0, len(compressed_stream), chunk_size):
            chunk = compressed_stream[i:i + chunk_size]
            out = decompressor.decompress_chunk(chunk)
            if out:
                crc_tracker.update(out)
                decompressed_chunks.append(out)

        out_final = decompressor.flush()
        if out_final:
            crc_tracker.update(out_final)
            decompressed_chunks.append(out_final)

        reconstructed = b"".join(decompressed_chunks)
        self.assertEqual(len(reconstructed), len(original_data))
        self.assertEqual(reconstructed, original_data)
        self.assertTrue(crc_tracker.verify(expected_crc))

    def test_zstd_method_93_streaming_small_chunks(self):
        from compression import zstd
        original_data = b"LiveExtract Zstandard Method 93 Streaming Payload! " * 6000
        expected_crc = zlib.crc32(original_data) & 0xFFFFFFFF
        compressed_stream = zstd.compress(original_data)

        decompressor = create_decompressor(93)
        self.assertIsInstance(decompressor, ZstdStreamDecompressor)
        crc_tracker = StreamingCRC32()
        chunks = []

        for i in range(0, len(compressed_stream), 97):
            out = decompressor.decompress_chunk(compressed_stream[i:i + 97])
            if out:
                crc_tracker.update(out)
                chunks.append(out)

        final = decompressor.flush()
        if final:
            crc_tracker.update(final)
            chunks.append(final)

        reconstructed = b"".join(chunks)
        self.assertEqual(reconstructed, original_data)
        self.assertTrue(crc_tracker.verify(expected_crc))

    def test_bzip2_and_xz_streaming(self):
        original = b"BZIP2 and XZ streaming check 0123456789 " * 500
        # BZIP2 (Method 12)
        bz_comp = bz2.compress(original)
        d_bz = create_decompressor(12)
        out_bz = b"".join(d_bz.decompress_chunk(bz_comp[i:i + 64]) for i in range(0, len(bz_comp), 64)) + d_bz.flush()
        self.assertEqual(out_bz, original)

        # XZ (Method 95)
        xz_comp = lzma.compress(original, format=lzma.FORMAT_XZ)
        d_xz = create_decompressor(95)
        out_xz = b"".join(d_xz.decompress_chunk(xz_comp[i:i + 64]) for i in range(0, len(xz_comp), 64)) + d_xz.flush()
        self.assertEqual(out_xz, original)

    def test_e2e_multi_codec_zip_including_zstd_93_bzip2_12_lzma_14(self):
        temp_root = Path(tempfile.mkdtemp())
        try:
            zip_path = temp_root / "multi_codec.zip"
            payloads = {
                "stored.txt": (b"Uncompressed stored file content" * 50, zipfile.ZIP_STORED),
                "deflate.bin": (b"Deflate compressed asset bytes " * 2000, zipfile.ZIP_DEFLATED),
                "zstd_game.pak": (b"Zstandard Method 93 modern game archive payload! " * 4000, zipfile.ZIP_ZSTANDARD),
                "bzip2.dat": (b"Bzip2 Method 12 payload " * 1500, zipfile.ZIP_BZIP2),
                "lzma.dat": (b"LZMA Method 14 payload " * 1500, zipfile.ZIP_LZMA),
            }

            with zipfile.ZipFile(zip_path, "w") as zf:
                for name, (data, method) in payloads.items():
                    zf.writestr(name, data, compress_type=method)

            dest_dir = temp_root / "extracted"
            client = StreamClient(max_workers=4, verify_crc=True)
            res = client.extract_from_url(str(zip_path), dest_dir)

            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["total_files"], len(payloads))
            for name, (expected_data, _) in payloads.items():
                out_file = dest_dir / name
                self.assertTrue(out_file.exists(), f"Missing extracted file: {name}")
                self.assertEqual(out_file.read_bytes(), expected_data, f"Bitwise mismatch for {name}")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_stored_decompressor(self):
        data = b"Hello uncompressed world!"
        decompressor = StoredStreamDecompressor()
        out = decompressor.decompress_chunk(data)
        out += decompressor.flush()
        self.assertEqual(out, data)

    def test_corrupted_stream_raises(self):
        corrupted = b"this is completely invalid deflate data 123456789"
        decompressor = RawDeflateStreamDecompressor()
        with self.assertRaises(DecompressionError):
            decompressor.decompress_chunk(corrupted)


if __name__ == "__main__":
    unittest.main()
