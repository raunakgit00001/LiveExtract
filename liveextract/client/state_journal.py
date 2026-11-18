"""
Transaction state journal for tracking extraction progress and enabling seamless resumption.
Throttles disk flushes during high-speed multi-file extraction to avoid disk I/O contention.
"""

import json
import time
from pathlib import Path
from typing import Dict, Any


class StateJournal:
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

    def is_file_completed(self, path_str: str, expected_size: int) -> bool:
        entry = self._data["files"].get(path_str)
        if not entry or entry.get("status") != "completed":
            return False

        local_path = self.target_dir / Path(path_str)
        if not local_path.exists():
            return False

        return local_path.stat().st_size == expected_size

    def mark_file_started(self, path_str: str) -> None:
        self._data["files"][path_str] = {
            "status": "in_progress",
            "updated": time.time(),
        }

    def mark_file_completed(self, path_str: str, crc32_hex: str, size: int) -> None:
        self._data["files"][path_str] = {
            "status": "completed",
            "crc32": crc32_hex,
            "size": size,
            "completed_at": time.time(),
        }
        self._dirty = True
        self.save(force=False)

    def mark_file_failed(self, path_str: str, reason: str) -> None:
        self._data["files"][path_str] = {
            "status": "failed",
            "error": reason,
            "failed_at": time.time(),
        }
        self.save(force=True)

    def mark_all_completed(self, cleanup: bool = False) -> None:
        self._data["status"] = "completed"
        self._data["completed_time"] = time.time()
        if cleanup:
            if self.state_file.exists():
                try:
                    self.state_file.unlink()
                except Exception:
                    pass
        else:
            self.save(force=True)
