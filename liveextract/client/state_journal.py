"""
Transaction state journal for tracking extraction progress and enabling seamless resumption.
Throttles disk flushes during high-speed multi-file extraction to avoid disk I/O contention.
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, Optional


class StateJournal:
    """
    Manages the persistence of .extract_state.json inside the destination root.
    Enables atomic resumption if downloads are interrupted.
    """

    STATE_FILENAME = ".extract_state.json"

    def __init__(self, target_dir: Path, archive_source: str, flush_interval: float = 1.5):
        self.target_dir = Path(target_dir)
        self.archive_source = archive_source
        self.state_file = self.target_dir / self.STATE_FILENAME
        self.flush_interval = flush_interval
        self._last_flush = 0.0
        self._dirty = False
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("archive_source") == self.archive_source:
                        return data
                    elif "files" in data and isinstance(data["files"], dict):
                        # Adopt existing state file for mirror/repaired source
                        data["archive_source"] = self.archive_source
                        return data
            except Exception:
                pass

        return {
            "archive_source": self.archive_source,
            "status": "pending",
            "start_time": time.time(),
            "last_updated": time.time(),
            "files": {},
        }

    def save(self, force: bool = True) -> None:
        """Persist current state to disk (throttled unless force=True)."""
        now = time.time()
        if not force and (now - self._last_flush) < self.flush_interval:
            self._dirty = True
            return

        self._data["last_updated"] = now
        try:
            self.target_dir.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            temp_file.replace(self.state_file)
            self._last_flush = now
            self._dirty = False
        except Exception:
            pass

    def flush_if_dirty(self) -> None:
        if self._dirty:
            self.save(force=True)

    def is_file_completed(self, path_str: str, expected_size: int, expected_crc32: Optional[str] = None) -> bool:
        """Check if a file was previously completed successfully and is intact on disk."""
        norm_p = path_str.replace("\\", "/")
        entry = self._data["files"].get(norm_p) or self._data["files"].get(path_str)

        local_path = self.target_dir / Path(norm_p)
        if not local_path.exists():
            return False

        if local_path.stat().st_size != expected_size:
            return False

        # Fast-path: If the file is recorded as completed in the journal with matching
        # byte size and matching CRC-32, accept immediately without re-reading 100 GB from disk.
        if (
            entry
            and entry.get("status") == "completed"
            and entry.get("size") == expected_size
            and (not expected_crc32 or entry.get("crc32", "").strip().upper() == expected_crc32.strip().upper())
        ):
            return True

        # If file status is explicitly pending, in_progress, or failed: not completed
        if entry and entry.get("status") in ("pending", "in_progress", "failed"):
            if not expected_crc32:
                return False

        # If expected CRC-32 is provided, verify bitwise integrity of file on disk using 1 MB chunks
        if expected_crc32:
            import zlib
            crc = 0
            try:
                with open(local_path, "rb") as f:
                    while True:
                        chunk = f.read(1048576)
                        if not chunk:
                            break
                        crc = zlib.crc32(chunk, crc)
                actual_crc = f"{(crc & 0xFFFFFFFF):08X}"
                if actual_crc.upper() != expected_crc32.strip().upper():
                    return False
            except Exception:
                return False
        elif not entry or entry.get("status") != "completed":
            return False

        # Intact file on disk: ensure recorded as completed in journal
        if not entry or entry.get("status") != "completed":
            self.mark_file_completed(norm_p, expected_crc32 or "", expected_size)

        return True

    def invalidate_file(
        self,
        path_str: str,
        expected_size: Optional[int] = None,
        expected_crc32: Optional[str] = None,
    ) -> None:
        """Mark a missing or corrupted file as pending so that resume re-downloads it, preserving manifest entry."""
        norm_p = path_str.replace("\\", "/")
        files = self._data.setdefault("files", {})
        entry = files.get(norm_p) or files.get(path_str)
        if entry:
            entry["status"] = "pending"
            if expected_size is not None:
                entry["size"] = expected_size
            if expected_crc32 is not None:
                entry["crc32"] = expected_crc32
            entry["invalidated_at"] = time.time()
            if path_str in files and path_str != norm_p:
                del files[path_str]
            files[norm_p] = entry
        else:
            files[norm_p] = {
                "status": "pending",
                "size": expected_size if expected_size is not None else 0,
                "crc32": expected_crc32 or "",
                "invalidated_at": time.time(),
            }
        self._dirty = True
        self.save(force=True)

    def mark_file_started(self, path_str: str) -> None:
        norm_p = path_str.replace("\\", "/")
        self._data["files"][norm_p] = {
            "status": "in_progress",
            "updated": time.time(),
        }

    def mark_file_completed(self, path_str: str, crc32_hex: str, size: int) -> None:
        norm_p = path_str.replace("\\", "/")
        self._data["files"][norm_p] = {
            "status": "completed",
            "crc32": crc32_hex,
            "size": size,
            "completed_at": time.time(),
        }
        self._dirty = True
        self.save(force=False)

    def mark_file_failed(self, path_str: str, reason: str) -> None:
        norm_p = path_str.replace("\\", "/")
        self._data["files"][norm_p] = {
            "status": "failed",
            "error": reason,
            "failed_at": time.time(),
        }
        self.save(force=True)

    def mark_all_completed(self, cleanup: bool = False) -> None:
        all_done = all(
            f.get("status") == "completed" for f in self._data.get("files", {}).values()
        )
        if all_done:
            self._data["status"] = "completed"
            self._data["completed_time"] = time.time()
        if cleanup and all_done:
            if self.state_file.exists():
                try:
                    self.state_file.unlink()
                except Exception:
                    pass
        else:
            self.save(force=True)
