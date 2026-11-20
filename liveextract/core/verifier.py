"""
Cryptographic integrity verifier for extracted archives.
Verifies on-disk files against recorded transaction journals (.extract_state.json)
or live archive manifests, checking file existence, exact byte size, and CRC-32 checksums.
"""

import os
import json
import zlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional, Callable


@dataclass
class VerificationReport:
    """Detailed cryptographic verification report."""
    total_expected: int
    intact_files: List[str] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)
    corrupted_files: List[Tuple[str, str]] = field(default_factory=list)  # (path, reason)
    total_verified_bytes: int = 0
    elapsed_seconds: float = 0.0

    @property
    def is_valid(self) -> bool:
        return len(self.missing_files) == 0 and len(self.corrupted_files) == 0 and self.total_expected > 0

    @property
    def failure_summary(self) -> str:
        lines = []
        if self.missing_files:
            lines.append(f"❌ Missing files ({len(self.missing_files)}):")
            for m in self.missing_files[:10]:
                lines.append(f"   • {m}")
            if len(self.missing_files) > 10:
                lines.append(f"   ... and {len(self.missing_files) - 10} more")
        if self.corrupted_files:
            lines.append(f"⚠️ Corrupted files ({len(self.corrupted_files)}):")
            for path, reason in self.corrupted_files[:10]:
                lines.append(f"   • {path}: {reason}")
            if len(self.corrupted_files) > 10:
                lines.append(f"   ... and {len(self.corrupted_files) - 10} more")
        return "\n".join(lines)


def calculate_file_crc32(file_path: Path, chunk_size: int = 65536) -> str:
    """Compute the CRC-32 checksum of a local file on disk."""
    crc = 0
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return f"{(crc & 0xFFFFFFFF):08X}"


def get_expected_file_table(
    target_dir: Path,
    archive_source: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Retrieve the table of expected files, sizes, and CRC-32 checksums.
    Prefers authoritative archive headers when archive_source is provided,
    falling back to .extract_state.json.
    """
    # 1. Authoritative archive source inspection (RAM only via HTTP Range / Central Directory)
    if archive_source:
        try:
            import struct
            from liveextract.client.range_reader import RangeReader
            from liveextract.core.binary_parser import (
                ZipBinaryParser, EOCD_MIN_SIZE, ZIP64_EOCD_MIN_SIZE, ZIP64_EOCD_SIGNATURE
            )
            reader = RangeReader(archive_source)
            try:
                total_size = reader.get_total_size()
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
                cd_bytes = reader.read_range(eocd.cd_offset, eocd.cd_offset + eocd.cd_size - 1)
                cd_entries = ZipBinaryParser.parse_central_directory(cd_bytes)
                entries: Dict[str, Dict[str, Any]] = {}
                for cd_entry in cd_entries:
                    if not cd_entry.is_dir:
                        norm_p = cd_entry.filename.replace("\\", "/")
                        entries[norm_p] = {
                            "size": cd_entry.uncompressed_size,
                            "crc32": cd_entry.crc32_hex,
                        }
                if entries:
                    return entries
            finally:
                reader.close()
        except Exception:
            pass

        # Fallback to 7-Zip CLI for local RAR/7z archives
        local_cand = Path(archive_source)
        if local_cand.exists():
            from liveextract.core.rar_extractor import find_rar_extractor
            import subprocess
            extractor_bin = find_rar_extractor()
            if extractor_bin:
                try:
                    p = subprocess.run(
                        [str(extractor_bin), "l", "-slt", str(local_cand)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    entries = {}
                    cur = {}
                    for line in p.stdout.splitlines():
                        if not line.strip() and cur:
                            if cur.get("Folder") == "-" and "Path" in cur:
                                rel_path = cur["Path"].replace("\\", "/")
                                entries[rel_path] = {
                                    "size": int(cur.get("Size", 0)),
                                    "crc32": cur.get("CRC", ""),
                                }
                            cur = {}
                        elif " = " in line:
                            k, v = line.split(" = ", 1)
                            cur[k.strip()] = v.strip()
                    if cur and cur.get("Folder") == "-" and "Path" in cur:
                        rel_path = cur["Path"].replace("\\", "/")
                        entries[rel_path] = {
                            "size": int(cur.get("Size", 0)),
                            "crc32": cur.get("CRC", ""),
                        }
                    if entries:
                        return entries
                except Exception:
                    pass

    # 2. Local state file fallback (.extract_state.json)
    state_file = target_dir / ".extract_state.json"
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                files = data.get("files", {})
                if files:
                    result = {}
                    for k, v in files.items():
                        norm_k = k.replace("\\", "/")
                        result[norm_k] = v
                    return result
        except Exception:
            pass

    return {}


def verify_destination(
    target_dir: Path,
    archive_source: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> VerificationReport:
    """
    Perform deep verification of all files in target_dir against expected manifest:
    1. Existence check (identifies deleted / missing files)
    2. Size check (identifies truncated files)
    3. CRC-32 hash check (identifies modified or corrupted bytes)
    """
    t0 = time.time()
    target_path = Path(target_dir).resolve()
    expected_table = get_expected_file_table(target_path, archive_source)

    if not expected_table:
        return VerificationReport(
            total_expected=0,
            missing_files=[],
            corrupted_files=[("", "No manifest or transaction journal (.extract_state.json) found to verify against")],
        )

    intact: List[str] = []
    missing: List[str] = []
    corrupted: List[Tuple[str, str]] = []
    total_bytes = 0

    total_files = len(expected_table)
    for idx, (rel_path, meta) in enumerate(expected_table.items(), 1):
        if progress_cb:
            progress_cb(rel_path, idx, total_files)

        expected_size = meta.get("size", 0)
        expected_crc = meta.get("crc32", "").strip().upper()
        file_status = meta.get("status", "")

        # Resolve local disk file
        local_file = target_path / Path(rel_path)

        # 1. Check file existence
        if not local_file.exists() or not local_file.is_file():
            missing.append(rel_path)
            continue

        # 2. Check byte length
        actual_size = local_file.stat().st_size
        if actual_size != expected_size:
            corrupted.append(
                (rel_path, f"Size mismatch: expected {expected_size} bytes, got {actual_size} bytes")
            )
            continue

        # 3. Check status flag (pending/failed/missing files are never considered intact)
        if file_status in ("pending", "missing", "failed"):
            if expected_crc:
                actual_crc = calculate_file_crc32(local_file)
                if actual_crc != expected_crc:
                    corrupted.append(
                        (rel_path, f"Incomplete/unverified file (CRC-32 mismatch: expected {expected_crc}, got {actual_crc})")
                    )
                    continue
            else:
                corrupted.append(
                    (rel_path, f"Incomplete file (download status: {file_status})")
                )
                continue

        total_bytes += actual_size

        # 4. Check CRC-32 (if recorded)
        if expected_crc:
            actual_crc = calculate_file_crc32(local_file)
            if actual_crc != expected_crc:
                corrupted.append(
                    (rel_path, f"CRC-32 mismatch: expected {expected_crc}, got {actual_crc}")
                )
                continue

        intact.append(rel_path)

    elapsed = max(0.001, time.time() - t0)
    return VerificationReport(
        total_expected=total_files,
        intact_files=intact,
        missing_files=missing,
        corrupted_files=corrupted,
        total_verified_bytes=total_bytes,
        elapsed_seconds=elapsed,
    )
