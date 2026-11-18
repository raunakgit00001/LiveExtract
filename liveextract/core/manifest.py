"""
Manifest data models, serializer, and generator for pre-flight streaming metadata.
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Union, BinaryIO

from liveextract.core.binary_parser import ZipBinaryParser, EOCD_MIN_SIZE, LOCAL_HEADER_MIN_SIZE


@dataclass
class FileManifestEntry:
    """Pre-computed byte offset and metadata for an individual archive file."""
    path: str
    is_dir: bool
    uncompressed_size: int
    compressed_size: int
    compression_method: int
    crc32: str
    local_header_offset: int
    raw_data_offset: int
    raw_data_end: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileManifestEntry":
        return cls(**data)


@dataclass
class ArchiveManifest:
    """Full archive pre-flight metadata specification."""
    archive_name: str
    total_archive_size: int
    total_uncompressed_size: int
    total_files: int
    total_dirs: int
    files: List[FileManifestEntry]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "archive_name": self.archive_name,
            "total_archive_size": self.total_archive_size,
            "total_uncompressed_size": self.total_uncompressed_size,
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
            "files": [f.to_dict() for f in self.files],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, output_path: Union[str, Path]) -> None:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveManifest":
        files = [FileManifestEntry.from_dict(f) for f in data["files"]]
        return cls(
            archive_name=data["archive_name"],
            total_archive_size=data["total_archive_size"],
            total_uncompressed_size=data["total_uncompressed_size"],
            total_files=data["total_files"],
            total_dirs=data["total_dirs"],
            files=files,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "ArchiveManifest":
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def load(cls, file_path: Union[str, Path]) -> "ArchiveManifest":
        with open(file_path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())


def generate_manifest_from_zip(zip_source: Union[str, Path, BinaryIO]) -> ArchiveManifest:
    """
    Inspect a ZIP file and construct a byte-mapped ArchiveManifest in a single fast pass.
    Reads Central Directory and local headers without extracting payload.
    """
    if isinstance(zip_source, (str, Path)):
        file_path = Path(zip_source)
        file_name = file_path.name
        with open(file_path, "rb") as f:
            return _generate_manifest_from_stream(f, file_name)
    else:
        name = getattr(zip_source, "name", "archive.zip")
        return _generate_manifest_from_stream(zip_source, Path(name).name)


def _generate_manifest_from_stream(stream: BinaryIO, archive_name: str) -> ArchiveManifest:
    stream.seek(0, 2)
    total_size = stream.tell()

    scan_len = min(total_size, 65536 + EOCD_MIN_SIZE)
    stream.seek(total_size - scan_len)
    tail_buffer = stream.read(scan_len)

    eocd = ZipBinaryParser.find_eocd(tail_buffer, archive_size=total_size)

    stream.seek(eocd.cd_offset)
    cd_bytes = stream.read(eocd.cd_size)
    cd_entries = ZipBinaryParser.parse_central_directory(cd_bytes)

    manifest_entries: List[FileManifestEntry] = []
    total_uncompressed = 0
    file_count = 0
    dir_count = 0

    for cd_entry in cd_entries:
        if cd_entry.is_dir:
            dir_count += 1
            raw_data_offset = cd_entry.local_header_offset
            raw_data_end = raw_data_offset
        else:
            file_count += 1
            total_uncompressed += cd_entry.uncompressed_size

            stream.seek(cd_entry.local_header_offset)
            local_hdr_buf = stream.read(LOCAL_HEADER_MIN_SIZE + cd_entry.filename_len + 1024)
            local_hdr = ZipBinaryParser.parse_local_header(local_hdr_buf, cd_entry.local_header_offset)

            raw_data_offset = local_hdr.raw_data_offset
            raw_data_end = raw_data_offset + cd_entry.compressed_size

        manifest_entries.append(
            FileManifestEntry(
                path=cd_entry.filename,
                is_dir=cd_entry.is_dir,
                uncompressed_size=cd_entry.uncompressed_size,
                compressed_size=cd_entry.compressed_size,
                compression_method=cd_entry.compression_method,
                crc32=cd_entry.crc32_hex,
                local_header_offset=cd_entry.local_header_offset,
                raw_data_offset=raw_data_offset,
                raw_data_end=raw_data_end,
            )
        )

    return ArchiveManifest(
        archive_name=archive_name,
        total_archive_size=total_size,
        total_uncompressed_size=total_uncompressed,
        total_files=file_count,
        total_dirs=dir_count,
        files=manifest_entries,
    )
