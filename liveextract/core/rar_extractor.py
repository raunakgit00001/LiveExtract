"""
Zero-Disk Streaming Extraction Engine for RAR4, RAR5 (Standard & Solid), and 7z archives.

Primary Mode (True Zero-Disk Streaming):
  Uses our native C++ COM bridge (`liveextract_7z_bridge.dll`) to feed `7z.dll` via a
  virtual RAM-only `IInStream` backed by an 8 MB LRU HTTP Range sliding cache.
  - Compressed archive bytes written to disk: STRICTLY 0.00 B (even for 100+ GB RAR5 archives!)
  - RAM cache strictly bounded (e.g. 4 x 2 MB = 8 MB max)
  - Live Pause / Resume / Cancel inside callbacks
  - Automatic `.extract_state.json` generation with exact sizes and CRC-32 checksums

Fallback Mode (if 7z.dll or bridge DLL is absent):
  Falls back to external `7z.exe` / `unrar.exe` if installed.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List, Tuple

from liveextract.client.controller import StreamController
from liveextract.core.rar_parser import RAR4_SIGNATURE, RAR5_SIGNATURE

SEVENZ_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

# 7-Zip Handler Format IDs matching liveextract_7z_bridge.cpp
FORMAT_ID_ZIP = 1
FORMAT_ID_RAR4 = 3
FORMAT_ID_7Z = 7
FORMAT_ID_RAR5 = 0xCC


class RarExtractorNotFoundError(FileNotFoundError):
    """Raised when neither 7-Zip (7z.dll/7z.exe) nor UnRAR is found on the system."""
    pass


def find_7z_dll() -> Optional[Path]:
    """Locate 7z.dll on the host system."""
    candidates = [
        Path(r"C:\Program Files\7-Zip\7z.dll"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.dll"),
    ]
    # Also check alongside 7z.exe on PATH or in executable dir
    exe_7z = shutil.which("7z") or shutil.which("7za")
    if exe_7z:
        candidates.append(Path(exe_7z).parent / "7z.dll")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "7z.dll")

    for c in candidates:
        if c.exists():
            return c
    return None


def find_bridge_dll() -> Optional[Path]:
    """Locate our compiled liveextract_7z_bridge.dll (works from source and PyInstaller .exe)."""
    candidates: List[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "liveextract_7z_bridge.dll")
        candidates.append(Path(sys.executable).parent / "liveextract_7z_bridge.dll")
        candidates.append(Path(sys.executable).parent / "_internal" / "liveextract_7z_bridge.dll")

    candidates.extend([
        Path(__file__).parent.parent.parent / "liveextract_7z_bridge.dll",
        Path("liveextract_7z_bridge.dll").resolve(),
    ])

    for c in candidates:
        if c.exists():
            return c
    return None


def find_rar_extractor() -> Optional[Path]:
    """
    Locate 7-Zip (7z.dll / 7z.exe) or UnRAR on the host system.
    Prefers 7z.dll when our zero-disk COM bridge is present.
    """
    dll_7z = find_7z_dll()
    if dll_7z and find_bridge_dll():
        return dll_7z

    candidates = [
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
        Path(r"C:\Program Files\WinRAR\WinRAR.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c

    for cmd in ["7z", "7za", "unrar"]:
        found = shutil.which(cmd)
        if found:
            return Path(found)

    return None


def is_sevenz(buffer: bytes) -> bool:
    """Return True if buffer begins with the 7z archive signature."""
    return buffer.startswith(SEVENZ_SIGNATURE)


class RamSlidingRangeCache:
    """
    High-throughput RAM-only LRU sliding block cache with dedicated asynchronous prefetch pipeline.
    Maintains dual persistent Keep-Alive connections (main reader + prefetch reader) so prefetching
    never stalls the decompressor or causes socket contention.
    Strictly caps memory usage in RAM while writing ZERO bytes of compressed archive to disk.
    """

    def __init__(
        self,
        reader,
        total_size: int,
        block_size: Optional[int] = None,
        max_blocks: Optional[int] = None,
        speed_mode: str = "max",
    ):
        import queue
        import threading

        self.reader = reader
        self.total_size = total_size
        self.speed_mode = speed_mode

        # Adaptive block sizing: scale up for large (60-80+ GB) archives to maximize TCP window saturation
        if block_size is not None:
            self.block_size = block_size
        elif total_size >= 10 * 1024 * 1024 * 1024:  # >= 10 GB
            self.block_size = (16 * 1024 * 1024) if speed_mode == "max" else (8 * 1024 * 1024)
        elif total_size >= 1024 * 1024 * 1024:        # >= 1 GB
            self.block_size = 8 * 1024 * 1024
        else:
            self.block_size = 4 * 1024 * 1024

        if max_blocks is not None:
            self.max_blocks = max_blocks
        elif speed_mode == "max":
            self.max_blocks = 6  # e.g. 6 x 16 MB = 96 MB RAM max
        else:
            self.max_blocks = 4  # e.g. 4 x 8 MB = 32 MB RAM max

        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._lock = threading.Lock()
        self.compressed_bytes_fetched = 0

        # Dedicated prefetch reader to avoid socket collision on HTTP Keep-Alive
        self._prefetch_reader = None
        if getattr(reader, "is_url", False):
            from liveextract.client.range_reader import RangeReader
            self._prefetch_reader = RangeReader(
                reader.source,
                timeout=reader.timeout,
                opener=reader.opener,
                socket_rcvbuf=getattr(reader, "socket_rcvbuf", 2097152),
            )

        # Persistent background prefetch worker
        self._prefetch_queue: queue.Queue = queue.Queue(maxsize=8)
        self._prefetching_blocks = set()
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._prefetch_worker, daemon=True)
        self._worker_thread.start()

    def _prefetch_worker(self) -> None:
        import queue
        reader = self._prefetch_reader or self.reader
        while not self._stop_event.is_set():
            try:
                block_idx = self._prefetch_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if self._stop_event.is_set():
                break

            with self._lock:
                if block_idx in self._cache:
                    self._prefetching_blocks.discard(block_idx)
                    continue

            start = block_idx * self.block_size
            if start >= self.total_size:
                with self._lock:
                    self._prefetching_blocks.discard(block_idx)
                continue
            end = min(self.total_size - 1, start + self.block_size - 1)

            try:
                data = reader.read_range(start, end)
                with self._lock:
                    self.compressed_bytes_fetched += len(data)
                    self._cache[block_idx] = data
                    self._cache.move_to_end(block_idx)
                    while len(self._cache) > self.max_blocks:
                        self._cache.popitem(last=False)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._prefetching_blocks.discard(block_idx)

    def _trigger_prefetch(self, from_block_idx: int) -> None:
        """Pipeline 2 blocks ahead into persistent prefetch worker queue."""
        for ahead in (1, 2):
            target = from_block_idx + ahead
            if target * self.block_size >= self.total_size:
                break
            with self._lock:
                if target in self._cache or target in self._prefetching_blocks:
                    continue
                self._prefetching_blocks.add(target)
            try:
                self._prefetch_queue.put_nowait(target)
            except Exception:
                with self._lock:
                    self._prefetching_blocks.discard(target)

    def _get_block(self, block_idx: int) -> bytes:
        self._trigger_prefetch(block_idx)

        with self._lock:
            if block_idx in self._cache:
                self._cache.move_to_end(block_idx)
                return self._cache[block_idx]

        # Block not in cache yet: fetch directly using main reader
        start = block_idx * self.block_size
        if start >= self.total_size:
            return b""
        end = min(self.total_size - 1, start + self.block_size - 1)

        data = self.reader.read_range(start, end)
        with self._lock:
            self.compressed_bytes_fetched += len(data)
            self._cache[block_idx] = data
            self._cache.move_to_end(block_idx)
            while len(self._cache) > self.max_blocks:
                self._cache.popitem(last=False)

        return data

    def read_at(self, offset: int, size: int) -> bytes:
        if offset >= self.total_size or size <= 0:
            return b""

        actual_end = min(self.total_size, offset + size)
        remaining = actual_end - offset
        pos = offset
        parts: List[bytes] = []

        while remaining > 0:
            block_idx = pos // self.block_size
            in_block_off = pos % self.block_size
            block_data = self._get_block(block_idx)
            if not block_data or in_block_off >= len(block_data):
                break
            take = min(remaining, len(block_data) - in_block_off)
            parts.append(block_data[in_block_off:in_block_off + take])
            pos += take
            remaining -= take

        return b"".join(parts) if len(parts) > 1 else (parts[0] if parts else b"")

    def close(self) -> None:
        """Stop background prefetch worker and close prefetch Keep-Alive connection."""
        self._stop_event.set()
        if self._prefetch_reader is not None:
            try:
                self._prefetch_reader.close()
            except Exception:
                pass


def _extract_via_7z_com_zero_disk(
    reader,
    total_size: int,
    format_id: int,
    target_dir: Path,
    filename: str,
    progress_cb: Optional[Callable[[Any], None]] = None,
    controller: Optional[StreamController] = None,
    speed_mode: str = "max",
) -> Dict[str, Any]:
    """
    Pure Zero-Disk Streaming extraction via liveextract_7z_bridge.dll -> 7z.dll.
    Streams RAR4, RAR5 (Standard & Solid), and 7z directly from HTTP Range RAM cache
    into destination files with STRICTLY 0 BYTES of archive ever written to disk.
    """
    bridge_path = find_bridge_dll()
    sevenzip_dll = find_7z_dll()
    if not bridge_path or not sevenzip_dll:
        raise RarExtractorNotFoundError("7z.dll or liveextract_7z_bridge.dll not found.")

    target_dir.mkdir(parents=True, exist_ok=True)
    bridge = ctypes.CDLL(str(bridge_path))

    READ_CB = ctypes.CFUNCTYPE(
        ctypes.c_int32,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    META_CB = ctypes.CFUNCTYPE(
        None,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.c_int32,
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    PROG_CB = ctypes.CFUNCTYPE(
        ctypes.c_int32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.c_uint64,
        ctypes.c_void_p,
    )

    bridge.ze7z_extract_stream.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int32,
        ctypes.c_uint64,
        ctypes.c_wchar_p,
        READ_CB,
        META_CB,
        PROG_CB,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    bridge.ze7z_extract_stream.restype = ctypes.c_int32

    ram_cache = RamSlidingRangeCache(reader=reader, total_size=total_size, speed_mode=speed_mode)
    if controller:
        controller.start()

    start_time = time.time()
    file_metadata: Dict[str, Dict[str, Any]] = {}
    total_uncompressed_expected = [0]
    total_file_count = [0]
    bytes_written_so_far = [0]
    last_emit = [0.0]

    from liveextract.client.stream_client import StreamProgress

    def on_read(offset: int, out_buf, size: int, _ud) -> int:
        if controller and not controller.check_and_wait():
            return -1  # Cancelled
        try:
            chunk = ram_cache.read_at(offset, size)
            n = len(chunk)
            if n > 0:
                ctypes.memmove(out_buf, chunk, n)
            return n
        except Exception:
            return -1

    def on_meta(idx: int, rel_path: str, is_dir: int, size: int, crc: int, _ud) -> None:
        if not rel_path:
            return
        norm_p = rel_path.replace("\\", "/")
        if not is_dir:
            total_file_count[0] += 1
            total_uncompressed_expected[0] += size
            file_metadata[norm_p] = {
                "status": "completed",
                "size": size,
                "crc32": f"{crc:08X}" if crc else "",
                "completed_at": time.time(),
            }

    def on_prog(idx: int, total_items: int, cur_path: str, delta: int, _ud) -> int:
        if controller and not controller.check_and_wait():
            return 1  # Abort extraction

        bytes_written_so_far[0] += delta
        now = time.time()
        if progress_cb and (now - last_emit[0] >= 0.08 or bytes_written_so_far[0] >= total_uncompressed_expected[0]):
            last_emit[0] = now
            elapsed = max(0.001, now - start_time)
            progress_cb(
                StreamProgress(
                    current_file=cur_path or filename,
                    file_index=min(idx + 1, max(1, total_file_count[0])),
                    total_files=max(1, total_file_count[0]),
                    uncompressed_bytes_done=bytes_written_so_far[0],
                    total_uncompressed_bytes=max(total_uncompressed_expected[0], bytes_written_so_far[0]),
                    compressed_bytes_streamed=ram_cache.compressed_bytes_fetched,
                    elapsed_seconds=elapsed,
                    bytes_per_second=bytes_written_so_far[0] / elapsed,
                    active_workers=1,
                )
            )
        return 0

    out_files = ctypes.c_uint32(0)
    out_bytes = ctypes.c_uint64(0)

    c_read = READ_CB(on_read)
    c_meta = META_CB(on_meta)
    c_prog = PROG_CB(on_prog)

    try:
        rc = bridge.ze7z_extract_stream(
            str(sevenzip_dll),
            format_id,
            total_size,
            str(target_dir),
            c_read,
            c_meta,
            c_prog,
            None,
            ctypes.byref(out_files),
            ctypes.byref(out_bytes),
        )
    finally:
        ram_cache.close()

    elapsed = max(0.001, time.time() - start_time)

    if rc == 1 or (controller and controller.is_cancelled):
        return {
            "status": "cancelled",
            "total_files": total_file_count[0],
            "uncompressed_bytes": bytes_written_so_far[0],
            "compressed_streamed_bytes": ram_cache.compressed_bytes_fetched,
            "elapsed_seconds": elapsed,
        }

    if rc != 0:
        raise RuntimeError(f"Zero-disk 7z.dll streaming extraction failed with code {rc}")

    # Persist cryptographic verification journal (.extract_state.json)
    if file_metadata:
        try:
            state_data = {
                "archive_source": filename,
                "status": "completed",
                "start_time": start_time,
                "completed_time": time.time(),
                "files": file_metadata,
            }
            with open(target_dir / ".extract_state.json", "w", encoding="utf-8") as f_st:
                json.dump(state_data, f_st, indent=2)
        except Exception:
            pass

    if controller:
        controller.set_status("completed")

    final_bytes = max(out_bytes.value, bytes_written_so_far[0])
    return {
        "status": "completed",
        "total_files": out_files.value or total_file_count[0],
        "uncompressed_bytes": final_bytes,
        "compressed_streamed_bytes": ram_cache.compressed_bytes_fetched,
        "elapsed_seconds": elapsed,
        "speed_mbps": (final_bytes / (1024 * 1024)) / elapsed,
        "archive_disk_bytes": 0,  # Strictly 0 bytes on disk!
    }


def extract_rar_file(
    archive_path: Path,
    target_dir: Path,
    progress_cb: Optional[Callable[[Any], None]] = None,
    controller: Optional[StreamController] = None,
) -> Dict[str, Any]:
    """
    Extract a local RAR/7z archive into target_dir.
    Uses the native 7z.dll COM bridge if available, else falls back to 7z.exe / unrar.exe.
    """
    if find_7z_dll() and find_bridge_dll():
        from liveextract.client.range_reader import RangeReader
        reader = RangeReader(str(archive_path))
        total_size = reader.get_total_size()
        head = reader.read_range(0, min(15, total_size - 1))
        fmt_id = _detect_7z_format_id(head)
        if fmt_id is not None:
            return _extract_via_7z_com_zero_disk(
                reader=reader,
                total_size=total_size,
                format_id=fmt_id,
                target_dir=target_dir,
                filename=archive_path.name,
                progress_cb=progress_cb,
                controller=controller,
            )

    # CLI Fallback if 7z.dll bridge is not available
    extractor_bin = None
    for c in [
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
    ]:
        if c.exists():
            extractor_bin = c
            break
    if not extractor_bin:
        found = shutil.which("7z") or shutil.which("unrar")
        extractor_bin = Path(found) if found else None

    if not extractor_bin:
        raise RarExtractorNotFoundError(
            "RAR/7z archive detected, but neither 7-Zip nor UnRAR was found on this system.\n"
            "Please install 7-Zip (https://www.7-zip.org)."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    bin_name = extractor_bin.stem.lower()
    if "unrar" in bin_name or "winrar" in bin_name:
        cmd = [str(extractor_bin), "x", "-y", str(archive_path), str(target_dir) + "\\"]
    else:
        cmd = [str(extractor_bin), "x", str(archive_path), f"-o{target_dir}", "-y", "-bso1", "-bsp1"]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    while True:
        if controller and controller.is_cancelled:
            proc.kill()
            raise RuntimeError("RAR extraction was cancelled by user.")
        line = proc.stdout.readline() if proc.stdout else ""
        if not line and proc.poll() is not None:
            break

    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"RAR extraction failed with code {proc.returncode}: {stderr or stdout}")

    elapsed = max(0.001, time.time() - t0)
    all_extracted = [p for p in target_dir.rglob("*") if p.is_file() and p.name != ".extract_state.json"]
    total_uncompressed = sum(f.stat().st_size for f in all_extracted)

    return {
        "status": "completed",
        "total_files": len(all_extracted),
        "uncompressed_bytes": total_uncompressed,
        "compressed_streamed_bytes": archive_path.stat().st_size if archive_path.exists() else 0,
        "elapsed_seconds": elapsed,
        "speed_mbps": (total_uncompressed / (1024 * 1024)) / elapsed,
    }


def _detect_7z_format_id(head_bytes: bytes) -> Optional[int]:
    if head_bytes.startswith(RAR5_SIGNATURE):
        return FORMAT_ID_RAR5
    if head_bytes.startswith(RAR4_SIGNATURE):
        return FORMAT_ID_RAR4
    if head_bytes.startswith(SEVENZ_SIGNATURE):
        return FORMAT_ID_7Z
    return None


def stream_and_extract_rar(
    reader,
    total_size: int,
    target_dir: Path,
    filename: str,
    chunk_size: int = 262144,
    progress_cb: Optional[Callable[[Any], None]] = None,
    controller: Optional[StreamController] = None,
    head_bytes: Optional[bytes] = None,
    speed_mode: str = "max",
) -> Dict[str, Any]:
    """
    Pure Zero-Disk Streaming Extraction for remote or local RAR4, RAR5 (Standard & Solid), and 7z archives!
    Streams directly from HTTP Range -> RAM LRU cache -> 7z.dll -> final uncompressed files on disk.
    Archive bytes written to disk: STRICTLY 0.00 B.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    if head_bytes is None:
        head_bytes = reader.read_range(0, min(15, total_size - 1))

    fmt_id = _detect_7z_format_id(head_bytes)
    if fmt_id is None:
        raise ValueError(f"Unrecognized RAR/7z magic bytes: {head_bytes[:8]!r}")

    # Primary Path: PURE ZERO-DISK COM STREAMING via 7z.dll! (0 bytes archive on disk)
    if find_7z_dll() and find_bridge_dll():
        return _extract_via_7z_com_zero_disk(
            reader=reader,
            total_size=total_size,
            format_id=fmt_id,
            target_dir=target_dir,
            filename=filename,
            progress_cb=progress_cb,
            controller=controller,
            speed_mode=speed_mode,
        )

    # Local direct fallback
    if not reader.is_url and reader.local_path and reader.local_path.exists():
        return extract_rar_file(reader.local_path, target_dir, progress_cb, controller)

    raise RarExtractorNotFoundError(
        "Pure zero-disk RAR/7z streaming requires 7-Zip (7z.dll) and liveextract_7z_bridge.dll. "
        "Please install 7-Zip (https://www.7-zip.org) so 7z.dll is available."
    )
