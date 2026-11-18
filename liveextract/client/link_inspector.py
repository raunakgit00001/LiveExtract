"""
Pre-flight link inspector for pre-download information display.
Extracts filename, file size, archive format, and byte-range capabilities before download.
"""

from pathlib import Path
from typing import Dict, Any
from liveextract.client.range_reader import RangeReader
from liveextract.core.rar_parser import RarBinaryParser


def format_bytes(size_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0 or unit == "TB":
            return f"{size_bytes:6.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def inspect_url_preflight(url: str, timeout: float = 12.0) -> Dict[str, Any]:
    reader = RangeReader(url, timeout=timeout)
    try:
        total_size = reader.get_total_size()
        head_bytes = reader.read_range(0, min(15, total_size - 1))
        format_name = "Unknown"
        is_solid = False
        sliding_window = "32 KB"

        if RarBinaryParser.is_rar(head_bytes):
            try:
                head_buf = reader.read_range(0, min(131072, total_size - 1))
                rar_info = RarBinaryParser.inspect(head_buf)
                format_name = rar_info.version
                is_solid = rar_info.is_solid
                sliding_window = format_bytes(rar_info.dictionary_size_bytes)
            except Exception:
                format_name = "RAR Archive"
        elif head_bytes.startswith(b"7z\xbc\xaf\x27\x1c"):
            format_name = "7z Archive (LZMA / LZMA2)"
            sliding_window = "12 MB RAM cache"
        elif head_bytes.startswith(b"PK\x03\x04") or head_bytes.startswith(b"PK\x05\x06"):
            import struct
            from liveextract.core.decompressor import get_compression_method_name
            codec_label = "DEFLATE / ZSTD"
            sliding_window = "32 KB fixed window"
            if head_bytes.startswith(b"PK\x03\x04") and len(head_bytes) >= 10:
                method_id = struct.unpack("<H", head_bytes[8:10])[0]
                if method_id != 0:
                    codec_label = get_compression_method_name(method_id)
                    if method_id == 93:
                        sliding_window = "Zstandard streaming window"
            format_name = f"ZIP ({codec_label})"

        resolved_name = reader.suggested_filename
        if not resolved_name or len(resolved_name) > 64 or "?" in resolved_name:
            ext = ".rar" if "RAR" in format_name else (".7z" if "7z" in format_name else ".zip")
            url_path = Path(url.split("?")[0]).name
            if url_path.lower().endswith((".zip", ".rar", ".7z", ".tar")):
                resolved_name = url_path
            else:
                resolved_name = f"downloaded_archive{ext}"

        return {
            "url": url,
            "effective_url": reader.effective_url,
            "filename": resolved_name,
            "total_size": total_size,
            "formatted_size": format_bytes(total_size),
            "format": format_name,
            "is_solid": is_solid,
            "supports_ranges": True,
            "sliding_window_ram": sliding_window,
            "archive_overhead_disk": "0.00 B",
        }
    finally:
        reader.close()
