"""
ctypes bridge connecting the LiveExtract Python pipeline to the compiled
native C streaming engine (liveextract_core.dll / libliveextract_core.so).
"""

import os
import ctypes
from pathlib import Path
from typing import Optional, Tuple
from liveextract.core.decompressor import StreamDecompressor, DecompressionError


class NativeCDecompressor(StreamDecompressor):
    """
    Streaming DEFLATE decompressor powered by compiled C engine (liveextract_core.dll).
    Binds directly to the 32 KB sliding window in C RAM space.
    """

    _lib: Optional[ctypes.CDLL] = None
    _loaded: bool = False

    @classmethod
    def load_library(cls) -> Optional[ctypes.CDLL]:
        if cls._loaded:
            return cls._lib

        candidates = [
            Path(__file__).parent.parent.parent / "liveextract_core.dll",
            Path("liveextract_core.dll").resolve(),
            Path(__file__).parent.parent.parent / "libliveextract_core.so",
        ]

        for p in candidates:
            if p.exists():
                try:
                    lib = ctypes.CDLL(str(p))
                    lib.ze_stream_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
                    lib.ze_stream_init.restype = ctypes.c_int

                    lib.ze_stream_decompress_chunk.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_char_p,
                        ctypes.c_size_t,
                        ctypes.POINTER(ctypes.c_size_t),
                        ctypes.c_char_p,
                        ctypes.c_size_t,
                        ctypes.POINTER(ctypes.c_size_t),
                    ]
                    lib.ze_stream_decompress_chunk.restype = ctypes.c_int

                    lib.ze_stream_flush.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_char_p,
                        ctypes.c_size_t,
                        ctypes.POINTER(ctypes.c_size_t),
                    ]
                    lib.ze_stream_flush.restype = ctypes.c_int

                    lib.ze_crc32.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
                    lib.ze_crc32.restype = ctypes.c_uint32

                    lib.ze_stream_free.argtypes = [ctypes.c_void_p]
                    lib.ze_stream_free.restype = None

                    cls._lib = lib
                    cls._loaded = True
                    return lib
                except Exception:
                    pass

        cls._loaded = True
        return None

    @classmethod
    def is_available(cls) -> bool:
        return cls.load_library() is not None

    def __init__(self, window_bits: int = -15):
        lib = self.load_library()
        if not lib:
            raise RuntimeError("Native C library (liveextract_core.dll) is not loaded.")

        self.lib = lib
        self.stream_ptr = ctypes.c_void_p(0)
        rc = self.lib.ze_stream_init(ctypes.byref(self.stream_ptr), window_bits)
        if rc != 0:
            raise DecompressionError(f"Failed to initialize native C stream: code {rc}")
        self._eof = False

    def decompress_chunk(self, data: bytes) -> bytes:
        if not data or self._eof:
            return b""

        chunks = []
        offset = 0
        total_len = len(data)
        out_cap = 65536
        out_buf = ctypes.create_string_buffer(out_cap)

        while offset < total_len and not self._eof:
            in_slice = data[offset:]
            in_consumed = ctypes.c_size_t(0)
            out_written = ctypes.c_size_t(0)

            rc = self.lib.ze_stream_decompress_chunk(
                self.stream_ptr,
                in_slice,
                len(in_slice),
                ctypes.byref(in_consumed),
                out_buf,
                out_cap,
                ctypes.byref(out_written),
            )

            if out_written.value > 0:
                chunks.append(out_buf.raw[:out_written.value])

            offset += in_consumed.value

            if rc == 1:
                self._eof = True
                break
            elif rc < 0:
                raise DecompressionError(f"Native C decompression error: code {rc}")

            if in_consumed.value == 0 and out_written.value == 0:
                break

        return b"".join(chunks)

    def flush(self) -> bytes:
        if self._eof:
            return b""

        chunks = []
        out_cap = 65536
        out_buf = ctypes.create_string_buffer(out_cap)

        while not self._eof:
            out_written = ctypes.c_size_t(0)
            rc = self.lib.ze_stream_flush(
                self.stream_ptr,
                out_buf,
                out_cap,
                ctypes.byref(out_written),
            )
            if out_written.value > 0:
                chunks.append(out_buf.raw[:out_written.value])

            if rc == 1:
                self._eof = True
                break
            elif rc < 0:
                raise DecompressionError(f"Native C stream flush error: code {rc}")

            if out_written.value == 0:
                self._eof = True
                break

        return b"".join(chunks)

    @property
    def unconsumed_tail(self) -> bytes:
        return b""

    @property
    def eof(self) -> bool:
        return self._eof

    def __del__(self):
        if hasattr(self, "stream_ptr") and self.stream_ptr and self.lib:
            self.lib.ze_stream_free(self.stream_ptr)
            self.stream_ptr = None
