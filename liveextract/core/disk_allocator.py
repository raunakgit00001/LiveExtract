"""
Filesystem graph pre-allocation and safe path resolution.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any


class PathSecurityError(ValueError):
    """Raised when an archive entry attempts directory traversal outside target root."""
    pass


class InsufficientDiskSpaceError(IOError):
    """Raised when available disk space is less than the required total uncompressed size."""
    pass


class DiskAllocator:
    """
    Manages safe path sanitization, free space verification,
    and pre-allocation of directories and file descriptors.
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).resolve()

    def sanitize_path(self, relative_path: str) -> Path:
        """
        Safely resolve relative_path inside self.root_dir.
        Blocks Zip Slip (directory traversal attacks using '../' or absolute paths).
        """
        # Normalize slashes and strip leading slashes / drive letters
        cleaned = relative_path.replace("\\", "/").lstrip("/")
        
        # Check for path components that attempt traversal
        parts = Path(cleaned).parts
        if ".." in parts:
            raise PathSecurityError(f"Directory traversal detected in path: {relative_path}")

        target_path = (self.root_dir / cleaned).resolve()
        
        # Verify that target_path is truly within self.root_dir
        try:
            target_path.relative_to(self.root_dir)
        except ValueError:
            raise PathSecurityError(
                f"Resolved path '{target_path}' escapes target root '{self.root_dir}'"
            )
            
        return target_path

    def check_disk_space(self, required_bytes: int, safety_margin: int = 10 * 1024 * 1024) -> Tuple[bool, int, int]:
        """
        Check if target storage has enough free disk space.
        Returns: (has_space: bool, free_bytes: int, required_bytes: int)
        """
        self.root_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root_dir)
        has_space = usage.free >= (required_bytes + safety_margin)
        return has_space, usage.free, required_bytes

    def pre_allocate_file(self, target_path: Path, uncompressed_size: int, skip_if_exists: bool = True) -> None:
        """
        Pre-allocate empty file on disk with exact uncompressed size.
        Uses truncate to allocate blocks immediately, reducing fragmentation.
        If skip_if_exists is True and the file already exists with matching size, leaves it intact.
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        # If directory entry (size 0 and ends in directory structure)
        if uncompressed_size == 0:
            target_path.touch(exist_ok=True)
            return

        if skip_if_exists and target_path.exists() and target_path.stat().st_size == uncompressed_size:
            return

        with open(target_path, "wb") as f:
            f.truncate(uncompressed_size)

    def pre_allocate_manifest(
        self,
        files: List[Dict[str, Any]],
        verify_space: bool = True,
        safety_margin_bytes: int = 10 * 1024 * 1024,
        skip_if_exists: bool = True,
        allocate_files: bool = True,
    ) -> Dict[str, Path]:
        """
        Pre-allocate all directories and files defined in a file list / manifest.
        Returns mapping from archive path string to resolved local Path object.
        If allocate_files is False, paths and disk space are validated in RAM immediately
        without touching disk, avoiding multi-minute file allocation freezes on large archives.
        """
        self.root_dir.mkdir(parents=True, exist_ok=True)
        
        total_required = sum(f.get("uncompressed_size", 0) for f in files)
        if verify_space:
            has_space, free_b, req_b = self.check_disk_space(total_required, safety_margin_bytes)
            if not has_space:
                raise InsufficientDiskSpaceError(
                    f"Insufficient disk space on {self.root_dir}: "
                    f"Available {free_b / 1024 / 1024:.2f} MB, "
                    f"Required {(req_b + safety_margin_bytes) / 1024 / 1024:.2f} MB"
                )

        allocated_paths: Dict[str, Path] = {}

        for entry in files:
            path_str = entry["path"]
            resolved = self.sanitize_path(path_str)
            allocated_paths[path_str] = resolved
            
            if entry.get("is_dir", False) or path_str.endswith("/"):
                if allocate_files:
                    resolved.mkdir(parents=True, exist_ok=True)
            else:
                if allocate_files:
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    size = entry.get("uncompressed_size", 0)
                    self.pre_allocate_file(resolved, size, skip_if_exists=skip_if_exists)

        return allocated_paths
