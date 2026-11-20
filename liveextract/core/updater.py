"""
Smart Differential Update & Patch Engine for LiveExtract.
Inspects updated server archives (ZIP/ZIP64, RAR, 7z) via pure HTTP Range in RAM,
compares remote files with local files on disk (via byte size and CRC-32),
and downloads ONLY new or modified files while keeping 100% of unchanged data intact.
Eliminates re-downloading entire 50-100+ GB archives for minor game patches or updates.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

from liveextract.client.state_journal import StateJournal
from liveextract.core.verifier import calculate_file_crc32, get_expected_file_table


@dataclass
class UpdatePlan:
    """Detailed differential update plan between local folder and remote archive."""
    target_dir: Path
    new_archive_source: str
    total_remote_files: int
    total_local_files: int
    unchanged_files: List[Tuple[str, int, str]] = field(default_factory=list)  # (path, size, crc32)
    modified_files: List[Tuple[str, int, str]] = field(default_factory=list)   # (path, new_size, new_crc32)
    new_files: List[Tuple[str, int, str]] = field(default_factory=list)        # (path, new_size, new_crc32)
    obsolete_files: List[str] = field(default_factory=list)                    # present locally, absent remotely
    download_bytes_needed: int = 0
    total_uncompressed_bytes: int = 0
    bytes_saved: int = 0
    savings_percent: float = 0.0
    has_updates: bool = False


def check_for_updates(
    target_dir: Path,
    new_archive_source: str,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> UpdatePlan:
    """
    Compare local folder with a newer server archive to calculate the exact differential patch.
    Reads remote archive headers directly over HTTP Range in RAM with 0 disk overhead.
    """
    target_path = Path(target_dir).resolve()
    
    # 1. Fetch remote archive manifest in RAM
    remote_table = get_expected_file_table(target_path, archive_source=new_archive_source)
    if not remote_table:
        raise ValueError(f"Could not read archive manifest from {new_archive_source}")

    # 2. Enumerate existing local files on disk
    local_files: Dict[str, int] = {}
    if target_path.exists():
        for p in target_path.rglob("*"):
            if p.is_file():
                rel = p.relative_to(target_path).as_posix()
                if rel == ".extract_state.json" or rel.endswith(".tmp") or p.name.startswith("."):
                    continue
                local_files[rel] = p.stat().st_size

    unchanged: List[Tuple[str, int, str]] = []
    modified: List[Tuple[str, int, str]] = []
    new_files: List[Tuple[str, int, str]] = []
    total_remote_bytes = 0
    needed_bytes = 0

    total_items = len(remote_table)
    for idx, (rel_path, meta) in enumerate(remote_table.items(), 1):
        if progress_cb:
            progress_cb(rel_path, idx, total_items)

        exp_size = meta.get("size", 0)
        exp_crc = meta.get("crc32", "").strip().upper()
        total_remote_bytes += exp_size

        local_file = target_path / Path(rel_path)
        if not local_file.exists():
            new_files.append((rel_path, exp_size, exp_crc))
            needed_bytes += exp_size
        else:
            actual_size = local_file.stat().st_size
            if actual_size != exp_size:
                modified.append((rel_path, exp_size, exp_crc))
                needed_bytes += exp_size
            elif exp_crc:
                # Check CRC-32 to detect content modifications even if file size matches
                actual_crc = calculate_file_crc32(local_file)
                if actual_crc != exp_crc:
                    modified.append((rel_path, exp_size, exp_crc))
                    needed_bytes += exp_size
                else:
                    unchanged.append((rel_path, exp_size, exp_crc))
            else:
                unchanged.append((rel_path, exp_size, exp_crc))

    # Obsolete files: present on disk but removed in the updated archive
    obsolete: List[str] = [rel for rel in local_files if rel not in remote_table]

    bytes_saved = max(0, total_remote_bytes - needed_bytes)
    savings_pct = (bytes_saved / max(1, total_remote_bytes)) * 100.0 if total_remote_bytes > 0 else 0.0
    has_updates = len(modified) > 0 or len(new_files) > 0 or len(obsolete) > 0

    return UpdatePlan(
        target_dir=target_path,
        new_archive_source=new_archive_source,
        total_remote_files=len(remote_table),
        total_local_files=len(local_files),
        unchanged_files=unchanged,
        modified_files=modified,
        new_files=new_files,
        obsolete_files=obsolete,
        download_bytes_needed=needed_bytes,
        total_uncompressed_bytes=total_remote_bytes,
        bytes_saved=bytes_saved,
        savings_percent=savings_pct,
        has_updates=has_updates,
    )


def apply_update_plan(
    plan: UpdatePlan,
    remove_obsolete: bool = False,
) -> None:
    """
    Prepare the target folder and StateJournal for differential update:
    1. Unlinks modified files so fresh versions are cleanly written.
    2. Optionally unlinks obsolete files removed from the new version.
    3. Populates StateJournal with the new version manifest:
       - Unchanged files are marked completed.
       - Modified and new files are marked pending so StreamClient only downloads them.
    """
    target_path = plan.target_dir
    journal = StateJournal(target_path, plan.new_archive_source)

    # 1. Unlink modified files on disk
    for rel_path, _, _ in plan.modified_files:
        f = target_path / Path(rel_path)
        try:
            if f.exists() and f.is_file():
                f.unlink()
        except Exception:
            pass

    # 2. Optionally remove obsolete files
    if remove_obsolete:
        for rel_path in plan.obsolete_files:
            f = target_path / Path(rel_path)
            try:
                if f.exists() and f.is_file():
                    f.unlink()
            except Exception:
                pass

    # 3. Synchronize journal files:
    # Unchanged files are marked completed with their known checksum
    for rel_path, size, crc in plan.unchanged_files:
        norm_p = rel_path.replace("\\", "/")
        journal.mark_file_completed(norm_p, crc, size)

    # Modified and new files are set to pending
    for rel_path, size, crc in (plan.modified_files + plan.new_files):
        norm_p = rel_path.replace("\\", "/")
        journal.invalidate_file(norm_p, expected_size=size, expected_crc32=crc)

    journal.save(force=True)
