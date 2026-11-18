"""
Core archive parsing, decompression, pre-allocation, and checksum modules.
"""

from liveextract.core.binary_parser import ZipBinaryParser, CentralDirectoryEntry, EOCDRecord
from liveextract.core.rar_parser import RarBinaryParser, RarArchiveInfo, RarFileEntry
from liveextract.core.rar_extractor import find_rar_extractor, extract_rar_file, stream_and_extract_rar
from liveextract.core.manifest import ArchiveManifest, FileManifestEntry, generate_manifest_from_zip
from liveextract.core.decompressor import RawDeflateStreamDecompressor, StreamDecompressor
from liveextract.core.disk_allocator import DiskAllocator
from liveextract.core.verifier import verify_destination, VerificationReport, calculate_file_crc32
from liveextract.core.checksum import StreamingCRC32

__all__ = [
    "ZipBinaryParser",
    "CentralDirectoryEntry",
    "EOCDRecord",
    "RarBinaryParser",
    "RarArchiveInfo",
    "RarFileEntry",
    "find_rar_extractor",
    "extract_rar_file",
    "stream_and_extract_rar",
    "verify_destination",
    "VerificationReport",
    "calculate_file_crc32",
    "ArchiveManifest",
    "FileManifestEntry",
    "generate_manifest_from_zip",
    "RawDeflateStreamDecompressor",
    "StreamDecompressor",
    "DiskAllocator",
    "StreamingCRC32",
]
