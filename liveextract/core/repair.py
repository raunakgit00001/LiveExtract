"""
Archive repair engine for LiveExtract.
Verifies an already-downloaded / extracted folder against an archive link or file,
confirms sufficient content/name matches to guarantee the link belongs to the folder,
and triggers surgical re-download of missing or corrupted files only.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

from liveextract.client.state_journal import StateJournal
from liveextract.core.verifier import verify_destination, get_expected_file_table, VerificationReport


@dataclass
class FolderMatchResult:
    """Confidence and match statistics between a local folder and an archive manifest."""
    is_match: bool
    confidence: float                  # 0.0 to 1.0
    matched_names: int
    matched_sizes: int
    total_folder_files: int
    total_archive_files: int
    sample_matches: List[str]
    sample_mismatches: List[str]
    message: str


def check_link_folder_match(
    target_dir: Path,
    archive_source: str,
    min_confidence: float = 0.20,
) -> Tuple[bool, FolderMatchResult, Dict[str, Dict[str, Any]]]:
    """
    Verify that an archive link or file matches an already extracted folder.
    
    1. Fetches expected file table from archive (RAM only via HTTP Range / Central Directory).
    2. Scans files physically present in target_dir.
    3. Verifies overlap of relative file paths and sizes.
    """
    target_path = Path(target_dir).resolve()
    if not target_path.exists() or not target_path.is_dir():
        res = FolderMatchResult(
            is_match=False,
            confidence=0.0,
            matched_names=0,
            matched_sizes=0,
            total_folder_files=0,
            total_archive_files=0,
            sample_matches=[],
            sample_mismatches=[],
            message=f"Directory does not exist: {target_dir}",
        )
        return False, res, {}

    # 1. Fetch expected archive file table
    expected_table = get_expected_file_table(target_path, archive_source=archive_source)
    if not expected_table:
        res = FolderMatchResult(
            is_match=False,
            confidence=0.0,
            matched_names=0,
            matched_sizes=0,
            total_folder_files=0,
            total_archive_files=0,
            sample_matches=[],
            sample_mismatches=[],
            message="Could not read file manifest from the provided archive link or file.",
        )
        return False, res, {}

    # 2. Enumerate existing local files (ignoring state journal and hidden files)
    local_files: List[str] = []
    for p in target_path.rglob("*"):
        if p.is_file():
            rel_str = p.relative_to(target_path).as_posix()
            if rel_str == ".extract_state.json" or rel_str.endswith(".tmp") or p.name.startswith("."):
                continue
            local_files.append(rel_str)

    total_folder = len(local_files)
    total_archive = len(expected_table)

    # 3. If folder is empty, allow fresh extraction
    if total_folder == 0:
        res = FolderMatchResult(
            is_match=True,
            confidence=1.0,
            matched_names=0,
            matched_sizes=0,
            total_folder_files=0,
            total_archive_files=total_archive,
            sample_matches=[],
            sample_mismatches=[],
            message=f"Folder is empty. All {total_archive} archive files will be downloaded.",
        )
        return True, res, expected_table

    # 4. Check name and size overlap
    matched_names = 0
    matched_sizes = 0
    sample_matches = []
    sample_mismatches = []

    for rel_p in local_files:
        if rel_p in expected_table:
            matched_names += 1
            if len(sample_matches) < 5:
                sample_matches.append(rel_p)
            exp_size = expected_table[rel_p].get("size", -1)
            actual_size = (target_path / Path(rel_p)).stat().st_size
            if exp_size == actual_size:
                matched_sizes += 1
        else:
            if len(sample_mismatches) < 5:
                sample_mismatches.append(rel_p)

    confidence = matched_names / max(1, min(total_folder, total_archive))

    # Evaluate match confidence
    if matched_names == 0:
        is_match = False
        msg = (
            f"Link Mismatch: None of the {total_folder} files in the folder match any of "
            f"the {total_archive} files in the archive."
        )
    elif confidence < min_confidence and matched_names < 3:
        is_match = False
        msg = (
            f"Low Match Confidence: Only {matched_names}/{total_folder} files match ({confidence:.0%}). "
            f"The link appears to be for a different version or archive."
        )
    else:
        is_match = True
        msg = (
            f"Match Confirmed: {matched_names}/{total_folder} folder files match "
            f"({matched_sizes} with exact size match, {confidence:.0%} confidence)."
        )

    res = FolderMatchResult(
        is_match=is_match,
        confidence=confidence,
        matched_names=matched_names,
        matched_sizes=matched_sizes,
        total_folder_files=total_folder,
        total_archive_files=total_archive,
        sample_matches=sample_matches,
        sample_mismatches=sample_mismatches,
        message=msg,
    )
    return is_match, res, expected_table


def inspect_and_verify_for_repair(
    target_dir: Path,
    archive_source: str,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Tuple[bool, FolderMatchResult, Optional[VerificationReport]]:
    """
    Perform pre-flight match validation followed by deep cryptographic verification.
    """
    is_match, match_res, expected_table = check_link_folder_match(target_dir, archive_source)
    if not is_match and match_res.matched_names == 0:
        return False, match_res, None

    report = verify_destination(target_dir, archive_source=archive_source, progress_cb=progress_cb)
    return True, match_res, report


def prepare_repair(
    target_dir: Path,
    archive_source: str,
    report: VerificationReport,
    expected_table: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """
    Purge corrupted files from disk and invalidate missing/corrupted entries
    in StateJournal so that StreamClient only re-downloads missing/corrupted files.
    Preserves all expected files in StateJournal so integrity checks never lose track of them.
    Returns the number of files requiring repair.
    """
    target_path = Path(target_dir).resolve()
    journal = StateJournal(target_path, archive_source)

    # 1. Ensure all expected files are registered in journal manifest
    if not expected_table:
        expected_table = get_expected_file_table(target_path, archive_source=archive_source)

    if expected_table:
        for p, meta in expected_table.items():
            norm_p = p.replace("\\", "/")
            if norm_p not in journal._data.get("files", {}):
                journal._data.setdefault("files", {})[norm_p] = {
                    "status": "pending",
                    "size": meta.get("size", 0),
                    "crc32": meta.get("crc32", ""),
                }

    repaired_count = 0
    all_bad = list(report.missing_files) + [p for p, _ in report.corrupted_files]
    for rel_p in all_bad:
        norm_p = rel_p.replace("\\", "/")
        meta = expected_table.get(norm_p, {}) if expected_table else {}
        exp_size = meta.get("size")
        exp_crc = meta.get("crc32")
        journal.invalidate_file(norm_p, expected_size=exp_size, expected_crc32=exp_crc)

        # Unlink bad file from disk so disk doesn't retain corrupted bytes or pre-allocated zeroes
        bad_file = target_path / Path(norm_p)
        try:
            if bad_file.exists() and bad_file.is_file():
                bad_file.unlink()
        except Exception:
            pass
        repaired_count += 1

    journal.save(force=True)
    return repaired_count
