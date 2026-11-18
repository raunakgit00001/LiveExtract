"""
High-performance in-memory binary parser for ZIP and ZIP64 archive structures.
Parses EOCD, ZIP64 EOCD/Locator, Central Directory, and Local File Headers directly from memory buffers.
"""

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any


# Magic Signatures
EOCD_SIGNATURE = b"PK\x05\x06"             # 0x06054b50
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"       # 0x06064b50
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"    # 0x07064b50
CD_HEADER_SIGNATURE = b"PK\x01\x02"        # 0x02014b50
LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"     # 0x04034b50

EOCD_MIN_SIZE = 22
ZIP64_LOCATOR_SIZE = 20
ZIP64_EOCD_MIN_SIZE = 56
CD_HEADER_MIN_SIZE = 46
LOCAL_HEADER_MIN_SIZE = 30


class ZipFormatError(ValueError):
    """Raised when ZIP headers are corrupted, truncated, or invalid."""
    pass


@dataclass
class EOCDRecord:
    """End of Central Directory record representation (supports ZIP32 and ZIP64)."""
    disk_num: int
    cd_start_disk: int
    cd_records_this_disk: int
    total_records: int
    cd_size: int
    cd_offset: int
    comment_len: int
    comment: bytes
    eocd_offset_in_archive: int
    is_zip64: bool = False
    zip64_eocd_offset: Optional[int] = None


@dataclass
class CentralDirectoryEntry:
    """Individual file entry in the ZIP Central Directory."""
    version_made_by: int
    version_needed: int
    flag_bits: int
    compression_method: int
    last_mod_time: int
    last_mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    filename_len: int
    extra_len: int
    comment_len: int
    disk_num_start: int
    internal_attr: int
    external_attr: int
    local_header_offset: int
    filename: str
    extra_field: bytes
    comment: bytes

    @property
    def is_dir(self) -> bool:
        return self.filename.endswith("/") or (self.external_attr & 0x10 != 0 and self.uncompressed_size == 0)

    @property
    def crc32_hex(self) -> str:
        return f"{self.crc32:08X}"


@dataclass
class LocalFileHeader:
    """Local File Header located right before the compressed payload."""
    version_needed: int
    flag_bits: int
    compression_method: int
    last_mod_time: int
    last_mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    filename_len: int
    extra_len: int
    filename: str
    extra_field: bytes
    raw_data_offset: int


class ZipBinaryParser:
    """
    Parser for ZIP / ZIP64 binary records operating directly over in-memory byte buffers.
    Optimized for zero-disk-overhead streaming pipelines.
    """

    @staticmethod
    def _parse_zip64_extra(
        extra_field: bytes,
        uncompressed_size: int,
        compressed_size: int,
        local_header_offset: int,
        disk_num_start: int,
    ) -> Tuple[int, int, int, int]:
        """Parse ZIP64 Extended Information Extra Field (ID 0x0001) if 32-bit fields overflow."""
        idx = 0
        extra_len = len(extra_field)
        while idx + 4 <= extra_len:
            header_id, data_size = struct.unpack("<HH", extra_field[idx:idx + 4])
            idx += 4
            if header_id == 0x0001:
                field_data = extra_field[idx:idx + data_size]
                pos = 0
                if uncompressed_size == 0xFFFFFFFF and pos + 8 <= len(field_data):
                    uncompressed_size = struct.unpack("<Q", field_data[pos:pos + 8])[0]
                    pos += 8
                if compressed_size == 0xFFFFFFFF and pos + 8 <= len(field_data):
                    compressed_size = struct.unpack("<Q", field_data[pos:pos + 8])[0]
                    pos += 8
                if local_header_offset == 0xFFFFFFFF and pos + 8 <= len(field_data):
                    local_header_offset = struct.unpack("<Q", field_data[pos:pos + 8])[0]
                    pos += 8
                if disk_num_start == 0xFFFF and pos + 4 <= len(field_data):
                    disk_num_start = struct.unpack("<I", field_data[pos:pos + 4])[0]
                    pos += 4
                break
            idx += data_size
        return uncompressed_size, compressed_size, local_header_offset, disk_num_start

    @staticmethod
    def find_eocd(buffer: bytes, archive_size: Optional[int] = None) -> EOCDRecord:
        buf_len = len(buffer)
        if buf_len < EOCD_MIN_SIZE:
            raise ZipFormatError(f"Buffer too small to contain EOCD ({buf_len} bytes < {EOCD_MIN_SIZE})")

        pos = buffer.rfind(EOCD_SIGNATURE)
        if pos == -1:
            raise ZipFormatError("Could not find End of Central Directory signature (PK\\x05\\x06)")

        if pos + EOCD_MIN_SIZE > buf_len:
            raise ZipFormatError("Truncated EOCD header found in buffer")

        eocd_raw = buffer[pos:pos + EOCD_MIN_SIZE]
        (
            sig,
            disk_num,
            cd_start_disk,
            cd_records_this_disk,
            total_records,
            cd_size,
            cd_offset,
            comment_len,
        ) = struct.unpack("<4s4H2IH", eocd_raw)

        comment = buffer[pos + EOCD_MIN_SIZE:pos + EOCD_MIN_SIZE + comment_len]
        eocd_abs_offset = (archive_size - (buf_len - pos)) if archive_size is not None else pos

        is_zip64 = False
        zip64_eocd_abs: Optional[int] = None

        if pos >= ZIP64_LOCATOR_SIZE:
            loc_pos = pos - ZIP64_LOCATOR_SIZE
            if buffer[loc_pos:loc_pos + 4] == ZIP64_LOCATOR_SIGNATURE:
                is_zip64 = True
                _, _, zip64_eocd_abs, _ = struct.unpack("<4sIQI", buffer[loc_pos:loc_pos + ZIP64_LOCATOR_SIZE])

                buf_start_abs = (archive_size - buf_len) if archive_size is not None else 0
                z64_rel = zip64_eocd_abs - buf_start_abs
                if 0 <= z64_rel and z64_rel + ZIP64_EOCD_MIN_SIZE <= buf_len:
                    if buffer[z64_rel:z64_rel + 4] == ZIP64_EOCD_SIGNATURE:
                        (
                            _zsig,
                            _zsize,
                            _vmade,
                            _vneed,
                            z_disk_num,
                            z_cd_disk,
                            z_recs_disk,
                            z_total_recs,
                            z_cd_size,
                            z_cd_offset,
                        ) = struct.unpack("<4sQHHIIQQQQ", buffer[z64_rel:z64_rel + ZIP64_EOCD_MIN_SIZE])
                        disk_num = z_disk_num
                        cd_start_disk = z_cd_disk
                        cd_records_this_disk = z_recs_disk
                        total_records = z_total_recs
                        cd_size = z_cd_size
                        cd_offset = z_cd_offset

        return EOCDRecord(
            disk_num=disk_num,
            cd_start_disk=cd_start_disk,
            cd_records_this_disk=cd_records_this_disk,
            total_records=total_records,
            cd_size=cd_size,
            cd_offset=cd_offset,
            comment_len=comment_len,
            comment=comment,
            eocd_offset_in_archive=eocd_abs_offset,
            is_zip64=is_zip64,
            zip64_eocd_offset=zip64_eocd_abs,
        )

    @staticmethod
    def parse_central_directory(buffer: bytes) -> List[CentralDirectoryEntry]:
        entries: List[CentralDirectoryEntry] = []
        offset = 0
        buf_len = len(buffer)

        while offset + CD_HEADER_MIN_SIZE <= buf_len:
            if buffer[offset:offset + 4] != CD_HEADER_SIGNATURE:
                break

            header_raw = buffer[offset:offset + CD_HEADER_MIN_SIZE]
            (
                sig,
                version_made_by,
                version_needed,
                flag_bits,
                compression_method,
                last_mod_time,
                last_mod_date,
                crc32,
                compressed_size,
                uncompressed_size,
                filename_len,
                extra_len,
                comment_len,
                disk_num_start,
                internal_attr,
                external_attr,
                local_header_offset,
            ) = struct.unpack("<4s6H3I5H2I", header_raw)

            entry_total_len = CD_HEADER_MIN_SIZE + filename_len + extra_len + comment_len
            if offset + entry_total_len > buf_len:
                raise ZipFormatError("Truncated Central Directory entry encountered")

            name_bytes = buffer[offset + CD_HEADER_MIN_SIZE:offset + CD_HEADER_MIN_SIZE + filename_len]
            try:
                filename = name_bytes.decode("utf-8")
            except UnicodeDecodeError:
                filename = name_bytes.decode("cp437", errors="replace")

            extra_start = offset + CD_HEADER_MIN_SIZE + filename_len
            extra_field = buffer[extra_start:extra_start + extra_len]

            comment_start = extra_start + extra_len
            comment = buffer[comment_start:comment_start + comment_len]

            if (
                uncompressed_size == 0xFFFFFFFF
                or compressed_size == 0xFFFFFFFF
                or local_header_offset == 0xFFFFFFFF
                or disk_num_start == 0xFFFF
            ):
                uncompressed_size, compressed_size, local_header_offset, disk_num_start = (
                    ZipBinaryParser._parse_zip64_extra(
                        extra_field,
                        uncompressed_size,
                        compressed_size,
                        local_header_offset,
                        disk_num_start,
                    )
                )

            entries.append(
                CentralDirectoryEntry(
                    version_made_by=version_made_by,
                    version_needed=version_needed,
                    flag_bits=flag_bits,
                    compression_method=compression_method,
                    last_mod_time=last_mod_time,
                    last_mod_date=last_mod_date,
                    crc32=crc32,
                    compressed_size=compressed_size,
                    uncompressed_size=uncompressed_size,
                    filename_len=filename_len,
                    extra_len=extra_len,
                    comment_len=comment_len,
                    disk_num_start=disk_num_start,
                    internal_attr=internal_attr,
                    external_attr=external_attr,
                    local_header_offset=local_header_offset,
                    filename=filename,
                    extra_field=extra_field,
                    comment=comment,
                )
            )

            offset += entry_total_len

        return entries

    @staticmethod
    def parse_local_header_prefix(buffer: bytes) -> Tuple[int, int]:
        if len(buffer) < LOCAL_HEADER_MIN_SIZE:
            raise ZipFormatError(f"Buffer too small for Local File Header ({len(buffer)} < {LOCAL_HEADER_MIN_SIZE})")
        if buffer[:4] != LOCAL_HEADER_SIGNATURE:
            raise ZipFormatError(f"Invalid Local File Header signature: {buffer[:4]!r}")
        filename_len, extra_len = struct.unpack("<HH", buffer[26:30])
        return filename_len, extra_len

    @staticmethod
    def parse_local_header(buffer: bytes, local_header_offset: int = 0) -> LocalFileHeader:
        if len(buffer) < LOCAL_HEADER_MIN_SIZE:
            raise ZipFormatError(f"Buffer too small for Local File Header ({len(buffer)} < {LOCAL_HEADER_MIN_SIZE})")

        if buffer[:4] != LOCAL_HEADER_SIGNATURE:
            raise ZipFormatError(f"Invalid Local File Header signature: {buffer[:4]!r}")

        (
            sig,
            version_needed,
            flag_bits,
            compression_method,
            last_mod_time,
            last_mod_date,
            crc32,
            compressed_size,
            uncompressed_size,
            filename_len,
            extra_len,
        ) = struct.unpack("<4s5H3I2H", buffer[:LOCAL_HEADER_MIN_SIZE])

        header_len = LOCAL_HEADER_MIN_SIZE + filename_len + extra_len
        if len(buffer) < header_len:
            raise ZipFormatError("Buffer truncated before filename/extra fields in Local File Header")

        name_bytes = buffer[LOCAL_HEADER_MIN_SIZE:LOCAL_HEADER_MIN_SIZE + filename_len]
        try:
            filename = name_bytes.decode("utf-8")
        except UnicodeDecodeError:
            filename = name_bytes.decode("cp437", errors="replace")

        extra_field = buffer[LOCAL_HEADER_MIN_SIZE + filename_len:header_len]
        raw_data_offset = local_header_offset + header_len

        return LocalFileHeader(
            version_needed=version_needed,
            flag_bits=flag_bits,
            compression_method=compression_method,
            last_mod_time=last_mod_time,
            last_mod_date=last_mod_date,
            crc32=crc32,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
            filename_len=filename_len,
            extra_len=extra_len,
            filename=filename,
            extra_field=extra_field,
            raw_data_offset=raw_data_offset,
        )
