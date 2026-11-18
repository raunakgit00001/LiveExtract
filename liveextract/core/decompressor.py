"""
High-throughput streaming decompression engine.
Supports all major ZIP compression methods:
  - Method 0:  STORED (uncompressed passthrough)
  - Method 8:  DEFLATE (raw RFC 1951, fixed 32 KB RAM sliding window)
  - Method 12: BZIP2
  - Method 14: LZMA (ZIP APPNOTE 5.8.8)
  - Method 93: ZSTD (Zstandard, native Python 3.14 compression.zstd or zstandard package)
  - Method 95: XZ
"""

import bz2
import lzma
import struct
import zlib
from abc import ABC, abstractmethod
from typing import Optional

COMPRESSION_METHOD_NAMES = {
    0: "STORED",
    8: "DEFLATE",
    12: "BZIP2",
    14: "LZMA",
    93: "ZSTD",
    95: "XZ",
}


class DecompressionError(Exception):
    """Raised when compressed stream data is corrupted or malformed."""
    pass


class StreamDecompressor(ABC):
    """Abstract interface for streaming chunked decompression."""

    @abstractmethod
    def decompress_chunk(self, data: bytes) -> bytes:
        pass

    @abstractmethod
    def flush(self) -> bytes:
        pass

    @property
    @abstractmethod
    def unconsumed_tail(self) -> bytes:
        pass

    @property
    @abstractmethod
    def eof(self) -> bool:
        pass


class StoredStreamDecompressor(StreamDecompressor):
    """Passthrough decompressor for uncompressed (STORED, method 0) streams."""

    def __init__(self):
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        return data

    def flush(self) -> bytes:
        self._eof = True
        return b""

    @property
    def unconsumed_tail(self) -> bytes:
        return b""

    @property
    def eof(self) -> bool:
        return self._eof


class RawDeflateStreamDecompressor(StreamDecompressor):
    """Streaming DEFLATE decompressor for raw RFC 1951 payloads (ZIP method 8, 32 KB window)."""

    def __init__(self, window_bits: int = -15):
        self.decompress_obj = zlib.decompressobj(window_bits)
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data:
            return b""
        try:
            out = self.decompress_obj.decompress(data)
            if self.decompress_obj.eof:
                self._eof = True
            return out
        except zlib.error as err:
            raise DecompressionError(f"Corrupted DEFLATE stream: {err}") from err

    def flush(self) -> bytes:
        try:
            out = self.decompress_obj.flush()
            self._eof = True
            return out
        except zlib.error as err:
            raise DecompressionError(f"Error during DEFLATE stream flush: {err}") from err

    @property
    def unconsumed_tail(self) -> bytes:
        return self.decompress_obj.unconsumed_tail

    @property
    def eof(self) -> bool:
        return self._eof or self.decompress_obj.eof


class ZstdStreamDecompressor(StreamDecompressor):
    """Streaming Zstandard (Zstd) decompressor (ZIP method 93)."""

    def __init__(self):
        self._backend = None
        self._dctx = None
        self._eof = False
        self._unused = b""

        try:
            from compression import zstd as std_zstd
            self._zstd_mod = std_zstd
            self._backend = "stdlib"
            self._dctx = std_zstd.ZstdDecompressor()
        except ImportError:
            try:
                import zstandard as ext_zstd
                self._zstd_mod = ext_zstd
                self._backend = "zstandard"
                self._dctx = ext_zstd.ZstdDecompressor().decompressobj()
            except ImportError as err:
                raise DecompressionError(
                    "ZIP entry uses Zstandard (method 93), which requires Python 3.14+ "
                    "or the 'zstandard' package (pip install zstandard)."
                ) from err

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data:
            return b""

        try:
            if self._backend == "stdlib":
                out_parts = []
                buf = data
                while buf:
                    if self._dctx.eof:
                        self._dctx = self._zstd_mod.ZstdDecompressor()
                    out = self._dctx.decompress(buf)
                    if out:
                        out_parts.append(out)
                    if self._dctx.eof:
                        buf = self._dctx.unused_data
                        self._unused = buf
                    else:
                        buf = b""
                self._eof = self._dctx.eof and not self._unused
                return b"".join(out_parts)
            else:
                return self._dctx.decompress(data)
        except Exception as err:
            raise DecompressionError(f"Corrupted Zstandard (method 93) stream: {err}") from err

    def flush(self) -> bytes:
        self._eof = True
        if self._backend == "zstandard" and hasattr(self._dctx, "flush"):
            try:
                return self._dctx.flush()
            except Exception as err:
                raise DecompressionError(f"Error flushing Zstd stream: {err}") from err
        return b""

    @property
    def unconsumed_tail(self) -> bytes:
        return self._unused

    @property
    def eof(self) -> bool:
        return self._eof


class Bzip2StreamDecompressor(StreamDecompressor):
    """Streaming BZIP2 decompressor (ZIP compression method 12)."""

    def __init__(self):
        self._dctx = bz2.BZ2Decompressor()
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data or self._eof:
            return b""
        try:
            out = self._dctx.decompress(data)
            if self._dctx.eof:
                self._eof = True
            return out
        except OSError as err:
            raise DecompressionError(f"Corrupted BZIP2 stream: {err}") from err

    def flush(self) -> bytes:
        self._eof = True
        return b""

    @property
    def unconsumed_tail(self) -> bytes:
        return getattr(self._dctx, "unused_data", b"")

    @property
    def eof(self) -> bool:
        return self._eof or self._dctx.eof


class ZipLzmaStreamDecompressor(StreamDecompressor):
    """Streaming LZMA decompressor for ZIP compression method 14 (PKWARE APPNOTE 5.8.8)."""

    def __init__(self):
        self._header_buf = bytearray()
        self._props_size: Optional[int] = None
        self._dctx: Optional[lzma.LZMADecompressor] = None
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data or self._eof:
            return b""

        try:
            if self._dctx is None:
                self._header_buf.extend(data)
                if len(self._header_buf) < 4:
                    return b""
                if self._props_size is None:
                    _, self._props_size = struct.unpack("<HH", self._header_buf[:4])
                total_hdr = 4 + self._props_size
                if len(self._header_buf) < total_hdr:
                    return b""

                props_bytes = bytes(self._header_buf[4:total_hdr])
                filter_dict = lzma._decode_filter_properties(lzma.FILTER_LZMA1, props_bytes)
                self._dctx = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filter_dict])

                remainder = bytes(self._header_buf[total_hdr:])
                self._header_buf.clear()
                if not remainder:
                    return b""
                data = remainder

            out = self._dctx.decompress(data)
            if self._dctx.eof:
                self._eof = True
            return out
        except Exception as err:
            raise DecompressionError(f"Corrupted ZIP LZMA (method 14) stream: {err}") from err

    def flush(self) -> bytes:
        self._eof = True
        return b""

    @property
    def unconsumed_tail(self) -> bytes:
        return getattr(self._dctx, "unused_data", b"") if self._dctx else b""

    @property
    def eof(self) -> bool:
        return self._eof


class XzStreamDecompressor(StreamDecompressor):
    """Streaming XZ decompressor (ZIP compression method 95)."""

    def __init__(self):
        self._dctx = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data or self._eof:
            return b""
        try:
            out = self._dctx.decompress(data)
            if self._dctx.eof:
                self._eof = True
            return out
        except lzma.LZMAError as err:
            raise DecompressionError(f"Corrupted XZ (method 95) stream: {err}") from err

    def flush(self) -> bytes:
        self._eof = True
        return b""

    @property
    def unconsumed_tail(self) -> bytes:
        return getattr(self._dctx, "unused_data", b"")

    @property
    def eof(self) -> bool:
        return self._eof or self._dctx.eof


def get_compression_method_name(compression_method: int) -> str:
    return COMPRESSION_METHOD_NAMES.get(compression_method, f"METHOD_{compression_method}")


def create_decompressor(compression_method: int) -> StreamDecompressor:
    if compression_method == 0:
        return StoredStreamDecompressor()
    elif compression_method == 8:
        return RawDeflateStreamDecompressor()
    elif compression_method == 12:
        return Bzip2StreamDecompressor()
    elif compression_method == 14:
        return ZipLzmaStreamDecompressor()
    elif compression_method == 93:
        return ZstdStreamDecompressor()
    elif compression_method == 95:
        return XzStreamDecompressor()
    else:
        supported = ", ".join(f"{k}={v}" for k, v in COMPRESSION_METHOD_NAMES.items())
        raise ValueError(
            f"Unsupported compression method: {compression_method} (Supported: {supported})"
        )
