"""
High-throughput streaming archive extraction client.
Optimized for maximum network saturation:
  - Contiguous Range Batch Coalescing (coalesces hundreds of small files into single continuous HTTP streams)
  - Persistent HTTP/1.1 Keep-Alive TLS sockets per worker (zero repeated TLS handshakes)
  - Zero-RTT inline Local File Header skipping
  - Rolling 2-second real-time network & extraction speed telemetry
  - Full ZIP codec support (STORED, DEFLATE, BZIP2, LZMA, ZSTD/93, XZ, ZIP64) + Zero-Disk RAR4/RAR5/7z
  - Live Pause/Resume/Cancel with throttled transaction journaling
"""

import collections
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Generator, List, Optional, Tuple, Union

from liveextract.core.binary_parser import (
    ZipBinaryParser,
    EOCD_MIN_SIZE,
    LOCAL_HEADER_MIN_SIZE,
    ZIP64_EOCD_MIN_SIZE,
    ZIP64_EOCD_SIGNATURE,
)
from liveextract.core.manifest import ArchiveManifest, FileManifestEntry
from liveextract.core.decompressor import create_decompressor
from liveextract.core.disk_allocator import DiskAllocator
from liveextract.core.checksum import StreamingCRC32
from liveextract.client.range_reader import RangeReader
from liveextract.client.state_journal import StateJournal
from liveextract.client.controller import StreamController


class IntegrityError(ValueError):
    """Raised when decompressed file CRC-32 does not match archive metadata."""
    pass


class ExtractionCancelledError(Exception):
    """Raised when extraction was cancelled by user."""
    pass


@dataclass
class StreamProgress:
    """Real-time progress telemetry."""
    current_file: str
    file_index: int
    total_files: int
    uncompressed_bytes_done: int
    total_uncompressed_bytes: int
    compressed_bytes_streamed: int
    elapsed_seconds: float
    bytes_per_second: float            # Extraction speed (uncompressed B/s, rolling 2s)
    active_workers: int = 1
    net_bytes_per_second: float = 0.0  # Network download speed (compressed B/s, rolling 2s)

    @property
    def percent(self) -> float:
        if self.total_uncompressed_bytes == 0:
            return 100.0
        return min(100.0, (self.uncompressed_bytes_done / self.total_uncompressed_bytes) * 100.0)


ProgressCallback = Callable[[StreamProgress], None]


class _BatchByteCursor:
    """
    Zero-copy-friendly streaming byte cursor over a single continuous HTTP Range request.
    Allows demuxing dozens or hundreds of contiguous ZIP entries straight out of RAM
    without reopening sockets or issuing per-file HTTP requests.
    """

    def __init__(
        self,
        reader: RangeReader,
        start_offset: int,
        end_offset: int,
        total_archive_size: int,
        chunk_size: int,
    ):
        self.reader = reader
        self.pos = start_offset
        self.end_offset = end_offset
        self.total_archive_size = total_archive_size
        self.chunk_size = chunk_size
        self._iter = iter(reader.stream_range(start_offset, end_offset, chunk_size=chunk_size))
        self._buf = b""
        self._buf_idx = 0

    def _fill(self, min_needed: int = 1) -> bool:
        if self._buf_idx < len(self._buf):
            return True
        try:
            self._buf = next(self._iter)
            self._buf_idx = 0
            return len(self._buf) > 0
        except StopIteration:
            # Rare continuation if local extra field exceeded slack estimate
            if self.pos < self.total_archive_size:
                cont_end = min(self.total_archive_size - 1, self.pos + max(min_needed, self.chunk_size) - 1)
                self._iter = iter(self.reader.stream_range(self.pos, cont_end, chunk_size=self.chunk_size))
                self.end_offset = cont_end
                try:
                    self._buf = next(self._iter)
                    self._buf_idx = 0
                    return len(self._buf) > 0
                except StopIteration:
                    return False
            return False

    def skip_bytes(self, n: int) -> None:
        remaining = n
        while remaining > 0:
            if not self._fill(remaining):
                raise EOFError(f"Unexpected EOF skipping {n} bytes at offset {self.pos}")
            avail = len(self._buf) - self._buf_idx
            take = min(remaining, avail)
            self._buf_idx += take
            self.pos += take
            remaining -= take

    def skip_to(self, target_offset: int) -> None:
        if target_offset > self.pos:
            self.skip_bytes(target_offset - self.pos)
        elif target_offset < self.pos:
            # Should not happen on sorted batches, but handle safely via fresh range
            self.pos = target_offset
            self._buf = b""
            self._buf_idx = 0
            self._iter = iter(self.reader.stream_range(self.pos, self.end_offset, chunk_size=self.chunk_size))

    def read_exact(self, n: int) -> bytes:
        parts = []
        remaining = n
        while remaining > 0:
            if not self._fill(remaining):
                raise EOFError(f"Unexpected EOF reading {n} bytes at offset {self.pos}")
            avail = len(self._buf) - self._buf_idx
            take = min(remaining, avail)
            parts.append(self._buf[self._buf_idx:self._buf_idx + take])
            self._buf_idx += take
            self.pos += take
            remaining -= take
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def stream_n_bytes(self, n: int) -> Generator[bytes, None, None]:
        remaining = n
        while remaining > 0:
            if not self._fill(remaining):
                raise EOFError(f"Unexpected EOF streaming payload ({remaining} bytes left at offset {self.pos})")
            avail = len(self._buf) - self._buf_idx
            take = min(remaining, avail)
            chunk = self._buf[self._buf_idx:self._buf_idx + take]
            self._buf_idx += take
            self.pos += take
            remaining -= take
            yield chunk

    def close(self) -> None:
        """Close generator and ensure underlying socket is clean if not fully drained."""
        if hasattr(self, "_iter") and hasattr(self._iter, "close"):
            try:
                self._iter.close()
            except Exception:
                pass
        if self.pos <= self.end_offset:
            # Slack or interrupted batch: close reader socket so subsequent batch won't read stale bytes
            self.reader.close()
        self._buf = b""
        self._buf_idx = 0


class StreamClient:
    """
    LiveExtract client engine for on-the-fly streaming extraction.
    Coalesces contiguous files into large Keep-Alive HTTP Range streams to sustain full bandwidth.
    """

    def __init__(
        self,
        chunk_size: int = 262144,  # 256 KB default for high network throughput
        max_workers: int = 1,      # 1 = sequential, >1 = parallel multi-connection
        timeout: float = 20.0,
        verify_crc: bool = True,
        resume: bool = True,
        opener: Optional[urllib.request.OpenerDirector] = None,
        controller: Optional[StreamController] = None,
    ):
        self.chunk_size = chunk_size
        self.max_workers = max(1, max_workers)
        self.timeout = timeout
        self.verify_crc = verify_crc
        self.resume = resume
        self.opener = opener
        self.controller = controller or StreamController()

    def _build_range_batches(
        self,
        entries: List[FileManifestEntry],
        total_archive_size: int,
    ) -> List[Tuple[int, int, List[FileManifestEntry]]]:
        """
        Coalesce adjacent ZIP entries into large continuous HTTP Range batches.
        Eliminates hundreds of tiny HTTP requests and TLS handshakes for small game files.
        """
        if not entries:
            return []

        sorted_entries = sorted(entries, key=lambda e: e.local_header_offset)
        total_comp = sum(e.compressed_size for e in sorted_entries)

        # Target batch size: keep all workers saturated, up to 16 MB per continuous Range request
        if self.max_workers > 1 and total_comp > 0:
            target_batch_bytes = max(2 * 1024 * 1024, min(16 * 1024 * 1024, total_comp // (self.max_workers * 2)))
        else:
            target_batch_bytes = 16 * 1024 * 1024

        max_gap_bytes = 64 * 1024  # Coalesce if gap between adjacent files <= 64 KB

        def _entry_bounds(e: FileManifestEntry) -> Tuple[int, int]:
            if e.raw_data_offset >= 0 and e.raw_data_end > e.raw_data_offset:
                return e.raw_data_offset, e.raw_data_end
            slack = LOCAL_HEADER_MIN_SIZE + len(e.path.encode("utf-8")) + 1024
            return e.local_header_offset, min(total_archive_size, e.local_header_offset + slack + e.compressed_size)

        batches: List[Tuple[int, int, List[FileManifestEntry]]] = []
        cur_batch: List[FileManifestEntry] = []
        cur_start = 0
        cur_end = 0

        for entry in sorted_entries:
            e_start, e_end = _entry_bounds(entry)
            if not cur_batch:
                cur_batch = [entry]
                cur_start = e_start
                cur_end = e_end
                continue

            gap = e_start - cur_end
            new_span = max(cur_end, e_end) - cur_start
            if gap <= max_gap_bytes and new_span <= target_batch_bytes:
                cur_batch.append(entry)
                cur_end = max(cur_end, e_end)
            else:
                batches.append((cur_start, min(total_archive_size - 1, cur_end - 1), cur_batch))
                cur_batch = [entry]
                cur_start = e_start
                cur_end = e_end

        if cur_batch:
            batches.append((cur_start, min(total_archive_size - 1, cur_end - 1), cur_batch))

        return batches

    def extract_from_manifest(
        self,
        manifest: ArchiveManifest,
        archive_source: str,
        target_dir: Union[str, Path],
        progress_cb: Optional[ProgressCallback] = None,
        effective_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute high-speed streaming extraction using Range Batch Coalescing and persistent Keep-Alive sockets.
        """
        target_path = Path(target_dir).resolve()
        allocator = DiskAllocator(target_path)
        journal = StateJournal(target_path, archive_source) if self.resume else None

        # 1. Pre-allocate disk hierarchy & files
        manifest_dict_list = [f.to_dict() for f in manifest.files]
        allocated_paths = allocator.pre_allocate_manifest(manifest_dict_list, verify_space=True)

        # 2. Filter files to download (skip dirs & already completed files)
        files_to_extract: List[FileManifestEntry] = []
        uncompressed_done = 0
        compressed_streamed = 0
        processed_files = 0

        for entry in manifest.files:
            if entry.is_dir:
                continue
            if journal and journal.is_file_completed(entry.path, entry.uncompressed_size, entry.crc32):
                uncompressed_done += entry.uncompressed_size
                compressed_streamed += entry.compressed_size
                processed_files += 1
            else:
                files_to_extract.append(entry)

        self.controller.start()
        start_time = time.time()
        progress_lock = threading.Lock()
        stream_target = effective_url or archive_source

        # Rolling 2-second window for true instantaneous speed (like IDM)
        speed_window: Deque[Tuple[float, int, int]] = collections.deque()
        window_comp_sum = 0
        window_uncomp_sum = 0

        # Per-thread persistent Keep-Alive RangeReader pool
        thread_local = threading.local()
        all_readers: List[RangeReader] = []
        readers_lock = threading.Lock()

        def _get_worker_reader() -> RangeReader:
            r = getattr(thread_local, "reader", None)
            if r is None:
                r = RangeReader(stream_target, timeout=self.timeout, opener=self.opener)
                r._total_size = manifest.total_archive_size
                thread_local.reader = r
                with readers_lock:
                    all_readers.append(r)
            return r

        def _record_and_emit(file_path: str, comp_delta: int, uncomp_delta: int) -> None:
            nonlocal uncompressed_done, compressed_streamed, window_comp_sum, window_uncomp_sum
            now = time.time()
            compressed_streamed += comp_delta
            uncompressed_done += uncomp_delta

            if comp_delta > 0 or uncomp_delta > 0:
                speed_window.append((now, comp_delta, uncomp_delta))
                window_comp_sum += comp_delta
                window_uncomp_sum += uncomp_delta

            # Trim samples older than 2.0 seconds
            cutoff = now - 2.0
            while speed_window and speed_window[0][0] < cutoff:
                _, old_c, old_u = speed_window.popleft()
                window_comp_sum -= old_c
                window_uncomp_sum -= old_u

            if progress_cb:
                elapsed = max(0.001, now - start_time)
                win_span = max(0.15, (now - speed_window[0][0]) if speed_window else elapsed)
                net_bps = window_comp_sum / win_span if speed_window else (compressed_streamed / elapsed)
                ext_bps = window_uncomp_sum / win_span if speed_window else (uncompressed_done / elapsed)
                progress_cb(
                    StreamProgress(
                        current_file=file_path,
                        file_index=processed_files,
                        total_files=manifest.total_files,
                        uncompressed_bytes_done=uncompressed_done,
                        total_uncompressed_bytes=manifest.total_uncompressed_size,
                        compressed_bytes_streamed=compressed_streamed,
                        elapsed_seconds=elapsed,
                        bytes_per_second=ext_bps,
                        active_workers=self.max_workers,
                        net_bytes_per_second=net_bps,
                    )
                )

        def _extract_batch(batch_spec: Tuple[int, int, List[FileManifestEntry]]) -> None:
            nonlocal processed_files
            b_start, b_end, batch_entries = batch_spec

            if not self.controller.check_and_wait():
                return

            # Handle batch containing only 0-byte empty files without making any HTTP request
            if all(e.compressed_size == 0 or e.uncompressed_size == 0 for e in batch_entries):
                for entry in batch_entries:
                    allocated_paths[entry.path].touch(exist_ok=True)
                    with progress_lock:
                        processed_files += 1
                        if journal:
                            journal.mark_file_completed(entry.path, entry.crc32, 0)
                        _record_and_emit(entry.path, 0, 0)
                return

            reader = _get_worker_reader()
            cursor = _BatchByteCursor(
                reader=reader,
                start_offset=b_start,
                end_offset=b_end,
                total_archive_size=manifest.total_archive_size,
                chunk_size=self.chunk_size,
            )

            try:
                for entry in batch_entries:
                    if not self.controller.check_and_wait():
                        if journal:
                            with progress_lock:
                                journal.flush_if_dirty()
                        return

                    local_file = allocated_paths[entry.path]
                    if entry.compressed_size == 0 or entry.uncompressed_size == 0:
                        local_file.touch(exist_ok=True)
                        with progress_lock:
                            processed_files += 1
                            if journal:
                                journal.mark_file_completed(entry.path, entry.crc32, 0)
                            _record_and_emit(entry.path, 0, 0)
                        continue

                    if journal:
                        with progress_lock:
                            journal.mark_file_started(entry.path)

                    # Position cursor to exact start of raw compressed bytes
                    if entry.raw_data_offset >= 0 and entry.raw_data_end > entry.raw_data_offset:
                        cursor.skip_to(entry.raw_data_offset)
                    else:
                        cursor.skip_to(entry.local_header_offset)
                        hdr30 = cursor.read_exact(LOCAL_HEADER_MIN_SIZE)
                        fname_len, extra_len = ZipBinaryParser.parse_local_header_prefix(hdr30)
                        if fname_len + extra_len > 0:
                            cursor.skip_bytes(fname_len + extra_len)

                    crc_calc = StreamingCRC32()
                    decompressor = create_decompressor(entry.compression_method)

                    try:
                        with open(local_file, "wb") as f_out:
                            for payload_chunk in cursor.stream_n_bytes(entry.compressed_size):
                                if not self.controller.check_and_wait():
                                    if journal:
                                        with progress_lock:
                                            journal.flush_if_dirty()
                                    return

                                decompressed = decompressor.decompress_chunk(payload_chunk)
                                if decompressed:
                                    crc_calc.update(decompressed)
                                    f_out.write(decompressed)

                                with progress_lock:
                                    _record_and_emit(entry.path, len(payload_chunk), len(decompressed) if decompressed else 0)

                            final_bytes = decompressor.flush()
                            if final_bytes:
                                crc_calc.update(final_bytes)
                                f_out.write(final_bytes)
                                with progress_lock:
                                    _record_and_emit(entry.path, 0, len(final_bytes))
                    except Exception as stream_err:
                        try:
                            if local_file.exists():
                                local_file.unlink()
                        except Exception:
                            pass
                        if journal:
                            with progress_lock:
                                journal.mark_file_failed(entry.path, str(stream_err))
                        raise

                    if self.verify_crc and entry.crc32:
                        if not crc_calc.verify(entry.crc32):
                            err_msg = (
                                f"CRC32 mismatch for '{entry.path}': "
                                f"computed {crc_calc.hexdigest()} != expected {entry.crc32}"
                            )
                            try:
                                if local_file.exists():
                                    local_file.unlink()
                            except Exception:
                                pass
                            if journal:
                                with progress_lock:
                                    journal.mark_file_failed(entry.path, err_msg)
                            raise IntegrityError(err_msg)

                    with progress_lock:
                        processed_files += 1
                        if journal:
                            journal.mark_file_completed(entry.path, entry.crc32 or crc_calc.hexdigest(), entry.uncompressed_size)
                        _record_and_emit(entry.path, 0, 0)
            finally:
                cursor.close()

        # 3. Coalesce files into continuous Range Batches & execute
        batches = self._build_range_batches(files_to_extract, manifest.total_archive_size)

        try:
            if self.max_workers <= 1 or len(batches) <= 1:
                for b_spec in batches:
                    if not self.controller.check_and_wait():
                        break
                    _extract_batch(b_spec)
            else:
                with ThreadPoolExecutor(max_workers=min(self.max_workers, len(batches))) as executor:
                    futures = [executor.submit(_extract_batch, b_spec) for b_spec in batches]
                    for fut in as_completed(futures):
                        fut.result()
        finally:
            # Always flush journal and close persistent Keep-Alive sockets cleanly
            if journal:
                journal.flush_if_dirty()
            for r in all_readers:
                r.close()

        if self.controller.is_cancelled:
            return {
                "status": "cancelled",
                "total_files": manifest.total_files,
                "uncompressed_bytes": uncompressed_done,
                "compressed_streamed_bytes": compressed_streamed,
                "elapsed_seconds": max(0.001, time.time() - start_time),
            }

        if journal:
            journal.mark_all_completed(cleanup=False)

        self.controller.set_status("completed")
        total_elapsed = max(0.001, time.time() - start_time)
        return {
            "status": "completed",
            "total_files": manifest.total_files,
            "uncompressed_bytes": uncompressed_done,
            "compressed_streamed_bytes": compressed_streamed,
            "elapsed_seconds": total_elapsed,
            "speed_mbps": (uncompressed_done / (1024 * 1024)) / total_elapsed,
        }

    def extract_from_url(
        self,
        archive_source: str,
        target_dir: Union[str, Path],
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """
        Execute streaming extraction from URL or file (auto-detects ZIP/ZIP64, RAR4/RAR5, and 7z).
        """
        target_path = Path(target_dir).resolve()
        reader = RangeReader(archive_source, timeout=self.timeout, opener=self.opener)
        try:
            total_size = reader.get_total_size()

            # 1. Format Auto-Detection via magic header bytes
            from liveextract.core.rar_parser import RarBinaryParser
            from liveextract.core.rar_extractor import is_sevenz, stream_and_extract_rar
            head_bytes = reader.read_range(0, min(15, total_size - 1))
            is_rar_or_7z = RarBinaryParser.is_rar(head_bytes) or is_sevenz(head_bytes)
            resolved_name = reader.suggested_filename
            if not resolved_name or len(resolved_name) > 64 or "?" in resolved_name:
                if RarBinaryParser.is_rar(head_bytes):
                    ext = ".rar"
                elif is_sevenz(head_bytes):
                    ext = ".7z"
                else:
                    ext = ".zip"
                resolved_name = f"downloaded_archive{ext}"

            if is_rar_or_7z:
                return stream_and_extract_rar(
                    reader=reader,
                    total_size=total_size,
                    target_dir=target_path,
                    filename=resolved_name,
                    chunk_size=self.chunk_size,
                    progress_cb=progress_cb,
                    controller=self.controller,
                    head_bytes=head_bytes,
                )

            # 2. ZIP / ZIP64 Archive: Tail probe to read EOCD in RAM
            scan_len = min(total_size, 65536 + EOCD_MIN_SIZE)
            tail_bytes, _ = reader.read_tail(scan_len)
            eocd = ZipBinaryParser.find_eocd(tail_bytes, archive_size=total_size)

            if (
                eocd.is_zip64
                and eocd.zip64_eocd_offset is not None
                and (eocd.cd_offset == 0xFFFFFFFF or eocd.cd_size == 0xFFFFFFFF)
            ):
                z64_buf = reader.read_range(
                    eocd.zip64_eocd_offset,
                    eocd.zip64_eocd_offset + ZIP64_EOCD_MIN_SIZE - 1,
                )
                if len(z64_buf) >= ZIP64_EOCD_MIN_SIZE and z64_buf[:4] == ZIP64_EOCD_SIGNATURE:
                    _, _, _, _, _, _, _, z_total_recs, z_cd_size, z_cd_offset = struct.unpack(
                        "<4sQHHIIQQQQ", z64_buf[:ZIP64_EOCD_MIN_SIZE]
                    )
                    eocd.total_records = z_total_recs
                    eocd.cd_size = z_cd_size
                    eocd.cd_offset = z_cd_offset

            # 3. Fetch Central Directory buffer in 1 single request on the same Keep-Alive socket
            cd_bytes = reader.read_range(eocd.cd_offset, eocd.cd_offset + eocd.cd_size - 1)
            cd_entries = ZipBinaryParser.parse_central_directory(cd_bytes)
            eff_url = reader.effective_url
        finally:
            reader.close()

        # 4. Build manifest immediately with ZERO per-file pre-scan HTTP requests
        file_entries: List[FileManifestEntry] = []
        total_uncompressed = 0
        file_count = 0
        dir_count = 0

        for cd_entry in cd_entries:
            if cd_entry.is_dir:
                dir_count += 1
            else:
                file_count += 1
                total_uncompressed += cd_entry.uncompressed_size

            file_entries.append(
                FileManifestEntry(
                    path=cd_entry.filename,
                    is_dir=cd_entry.is_dir,
                    uncompressed_size=cd_entry.uncompressed_size,
                    compressed_size=cd_entry.compressed_size,
                    compression_method=cd_entry.compression_method,
                    crc32=cd_entry.crc32_hex,
                    local_header_offset=cd_entry.local_header_offset,
                    raw_data_offset=-1,  # Coalesced + resolved inline on-the-fly
                    raw_data_end=-1,
                )
            )

        manifest = ArchiveManifest(
            archive_name=resolved_name,
            total_archive_size=total_size,
            total_uncompressed_size=total_uncompressed,
            total_files=file_count,
            total_dirs=dir_count,
            files=file_entries,
        )

        return self.extract_from_manifest(
            manifest=manifest,
            archive_source=archive_source,
            target_dir=target_path,
            progress_cb=progress_cb,
            effective_url=eff_url,
        )
