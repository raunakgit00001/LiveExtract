"""
RAR archive inspector and Solid Archive detector.
Supports RAR4 and RAR5 formats, detects whether archives are Standard (Non-Solid) or Solid,
and determines dictionary sliding window requirements (128 KB - 4 GB).
"""

import struct
from dataclasses import dataclass
from typing import List, Tuple


RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"
RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"


class RarFormatError(ValueError):
    """Raised when RAR headers are invalid or malformed."""
    pass


@dataclass
class RarFileEntry:
    filename: str
    is_dir: bool
    compressed_size: int
    uncompressed_size: int
    crc32: int
    compression_method: int
    data_offset: int
    is_solid_file: bool


@dataclass
class RarArchiveInfo:
    version: str
    is_solid: bool
    dictionary_size_bytes: int
    total_files: int
    files: List[RarFileEntry]
    notes: str

    @property
    def can_stream_by_offset(self) -> bool:
        return not self.is_solid


class RarBinaryParser:
    """Parser for RAR4 and RAR5 headers."""

    @staticmethod
    def is_rar(buffer: bytes) -> bool:
        return buffer.startswith(RAR4_SIGNATURE) or buffer.startswith(RAR5_SIGNATURE)

    @staticmethod
    def inspect(buffer: bytes) -> RarArchiveInfo:
        if buffer.startswith(RAR5_SIGNATURE):
            return RarBinaryParser._parse_rar5(buffer)
        elif buffer.startswith(RAR4_SIGNATURE):
            return RarBinaryParser._parse_rar4(buffer)
        else:
            raise RarFormatError("Invalid signature: neither RAR4 nor RAR5 magic bytes found.")

    @staticmethod
    def _parse_rar4(buf: bytes) -> RarArchiveInfo:
        offset = len(RAR4_SIGNATURE)
        buf_len = len(buf)
        is_solid = False
        files: List[RarFileEntry] = []
        max_dict_size = 64 * 1024

        while offset + 7 <= buf_len:
            crc, head_type, flags, head_size = struct.unpack("<HBHH", buf[offset:offset + 7])
            if head_size < 7:
                break

            head_end = offset + head_size
            data_start = head_end

            if head_type == 0x73:
                if flags & 0x0008:
                    is_solid = True

            elif head_type == 0x74 and offset + 32 <= buf_len:
                pack_size, unp_size, host_os, file_crc, file_time, unp_ver, method, name_len, attr = struct.unpack(
                    "<IIBIIBBHI", buf[offset + 7:offset + 32]
                )
                dict_bits = (flags & 0x00E0) >> 5
                dict_size = (64 * 1024) << dict_bits
                if dict_size > max_dict_size:
                    max_dict_size = dict_size

                name_start = offset + 32
                if flags & 0x0100:
                    name_start += 8

                name_bytes = buf[name_start:name_start + name_len]
                filename = name_bytes.decode("utf-8", errors="replace")

                is_dir = bool(attr & 0x10) or filename.endswith("/")
                file_is_solid = bool(flags & 0x0010) or is_solid

                files.append(
                    RarFileEntry(
                        filename=filename,
                        is_dir=is_dir,
                        compressed_size=pack_size,
                        uncompressed_size=unp_size,
                        crc32=file_crc,
                        compression_method=method,
                        data_offset=data_start,
                        is_solid_file=file_is_solid,
                    )
                )

                data_start += pack_size
                offset = data_start
                continue

            elif head_type == 0x7B:
                break

            offset = head_end

        notes = (
            "Solid RAR: Single continuous sliding window across all files."
            if is_solid else
            "Standard (Non-Solid) RAR: Independent compressed blocks."
        )

        return RarArchiveInfo(
            version="RAR4",
            is_solid=is_solid,
            dictionary_size_bytes=max_dict_size,
            total_files=len(files),
            files=files,
            notes=notes,
        )

    @staticmethod
    def _read_vint(buf: bytes, offset: int) -> Tuple[int, int]:
        result = 0
        shift = 0
        bytes_read = 0
        while offset < len(buf):
            b = buf[offset]
            offset += 1
            bytes_read += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, bytes_read

    @staticmethod
    def _parse_rar5(buf: bytes) -> RarArchiveInfo:
        offset = len(RAR5_SIGNATURE)
        buf_len = len(buf)
        is_solid = False
        files: List[RarFileEntry] = []
        max_dict_size = 128 * 1024

        while offset + 4 <= buf_len:
            offset += 4
            head_size, sz_len = RarBinaryParser._read_vint(buf, offset)
            offset += sz_len
            head_end = offset + head_size

            head_type, type_len = RarBinaryParser._read_vint(buf, offset)
            offset += type_len

            head_flags, flags_len = RarBinaryParser._read_vint(buf, offset)
            offset += flags_len

            if head_type == 1:
                if head_flags & 0x0004:
                    is_solid = True

            elif head_type == 2:
                file_flags, fflags_len = RarBinaryParser._read_vint(buf, offset)
                offset += fflags_len
                unp_size, unp_len = RarBinaryParser._read_vint(buf, offset)
                offset += unp_len
                attr, attr_len = RarBinaryParser._read_vint(buf, offset)
                offset += attr_len

                is_dir = bool(file_flags & 0x0001)

                comp_info, cinfo_len = RarBinaryParser._read_vint(buf, offset)
                offset += cinfo_len
                method = (comp_info >> 7) & 0x07
                dict_code = comp_info & 0x0F
                dict_size = (128 * 1024) << dict_code
                if dict_size > max_dict_size:
                    max_dict_size = dict_size

                name_len, nlen_len = RarBinaryParser._read_vint(buf, offset)
                offset += nlen_len
                name_bytes = buf[offset:offset + name_len]
                filename = name_bytes.decode("utf-8", errors="replace")
                offset += name_len

                files.append(
                    RarFileEntry(
                        filename=filename,
                        is_dir=is_dir,
                        compressed_size=0,
                        uncompressed_size=unp_size,
                        crc32=0,
                        compression_method=method,
                        data_offset=head_end,
                        is_solid_file=is_solid,
                    )
                )

            offset = head_end

        notes = (
            "Solid RAR5: Single continuous sliding window."
            if is_solid else
            "Standard (Non-Solid) RAR5: Independent compressed blocks."
        )

        return RarArchiveInfo(
            version="RAR5",
            is_solid=is_solid,
            dictionary_size_bytes=max_dict_size,
            total_files=len(files),
            files=files,
            notes=notes,
        )
