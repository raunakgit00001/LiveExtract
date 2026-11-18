"""
Utility to construct reproducible test ZIP archives with varying compression methods,
directory nesting, and compressible data payloads.
"""

import io
import zipfile
from pathlib import Path


def create_sample_zip_bytes(num_files: int = 5, file_size_kb: int = 32) -> bytes:
    """
    Generate an in-memory ZIP archive with nested directories,
    mixed compression (STORED and DEFLATED), and known content.
    """
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        # 1. Root directory and empty directory
        zf.writestr("docs/", "")
        zf.writestr("docs/readme.txt", "LiveExtract Test Archive\nHeader Information.", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("docs/license.txt", "MIT License\nCopyright (c) 2026", compress_type=zipfile.ZIP_STORED)

        # 2. Nested folder with files
        zf.writestr("bin/", "")
        zf.writestr("bin/app.cfg", "key1=value1\nkey2=value2\n", compress_type=zipfile.ZIP_DEFLATED)

        # 3. Repeated compressible text to test DEFLATE sliding window
        for i in range(num_files):
            pattern = f"Chunk line {i:04d} with repeating patterns ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789\n"
            content = (pattern * ((file_size_kb * 1024) // len(pattern) + 1))[:file_size_kb * 1024].encode("utf-8")
            zf.writestr(f"data/file_{i:02d}.dat", content, compress_type=zipfile.ZIP_DEFLATED)

    return bio.getvalue()


def write_sample_zip_file(dest_path: Path, num_files: int = 5, file_size_kb: int = 32) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = create_sample_zip_bytes(num_files, file_size_kb)
    dest_path.write_bytes(zip_bytes)
    return dest_path
