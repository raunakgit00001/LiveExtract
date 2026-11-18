"""
Unit tests for RAR format detection and solid archive inspection.
"""

import unittest
import struct
from liveextract.core.rar_parser import (
    RarBinaryParser,
    RarFormatError,
    RAR4_SIGNATURE,
    RAR5_SIGNATURE,
)


def make_synthetic_rar4_bytes(is_solid: bool = False) -> bytes:
    """Build valid minimal RAR4 header buffer."""
    buf = bytearray(RAR4_SIGNATURE)
    # MAIN_HEAD (0x73)
    # crc: uint16, type: uint8 (0x73), flags: uint16, size: uint16 (7 + 6 = 13 bytes)
    flags = 0x0008 if is_solid else 0x0000
    head_size = 13
    main_head = struct.pack("<HBHH", 0x1234, 0x73, flags, head_size) + b"\x00" * 6
    buf.extend(main_head)

    # FILE_HEAD (0x74)
    # unp_size, host_os, crc, time, ver, method, name_len, attr
    filename = b"test_game/data.bin"
    file_flags = 0x0000
    file_head_core = struct.pack(
        "<IIBIIBBHI",
        1024,   # pack_size
        4096,   # unp_size
        0,      # host_os
        0xABCD1234, # crc
        0,      # time
        20,     # ver
        0x30,   # method (stored)
        len(filename),
        0x20,   # attr
    )
    file_head_size = 7 + len(file_head_core) + len(filename)
    file_head = struct.pack("<HBHH", 0x5678, 0x74, file_flags, file_head_size) + file_head_core + filename
    buf.extend(file_head)

    # Fake payload 1024 bytes
    buf.extend(b"\x00" * 1024)
    return bytes(buf)


def make_synthetic_rar5_bytes(is_solid: bool = False) -> bytes:
    """Build valid minimal RAR5 header buffer."""
    buf = bytearray(RAR5_SIGNATURE)
    # RAR5 block format:
    # CRC32 (4 bytes), size vint, type vint, flags vint, extra...
    # Main archive header: type 1. flags: 0x0004 if solid.
    flags = 0x0004 if is_solid else 0x0000
    main_head_payload = bytes([1, flags])  # type=1, flags
    main_head_size = len(main_head_payload)
    buf.extend(struct.pack("<I", 0xA1B2C3D4))  # crc
    buf.append(main_head_size)
    buf.extend(main_head_payload)
    return bytes(buf)


class TestRarParser(unittest.TestCase):
    def test_rar4_non_solid(self):
        data = make_synthetic_rar4_bytes(is_solid=False)
        self.assertTrue(RarBinaryParser.is_rar(data))
        info = RarBinaryParser.inspect(data)
        self.assertEqual(info.version, "RAR4")
        self.assertFalse(info.is_solid)
        self.assertTrue(info.can_stream_by_offset)
        self.assertEqual(len(info.files), 1)
        self.assertEqual(info.files[0].filename, "test_game/data.bin")

    def test_rar4_solid(self):
        data = make_synthetic_rar4_bytes(is_solid=True)
        info = RarBinaryParser.inspect(data)
        self.assertEqual(info.version, "RAR4")
        self.assertTrue(info.is_solid)
        self.assertFalse(info.can_stream_by_offset)

    def test_rar5_detection(self):
        data = make_synthetic_rar5_bytes(is_solid=True)
        self.assertTrue(RarBinaryParser.is_rar(data))
        info = RarBinaryParser.inspect(data)
        self.assertEqual(info.version, "RAR5")
        self.assertTrue(info.is_solid)
        self.assertFalse(info.can_stream_by_offset)

    def test_non_rar_raises(self):
        with self.assertRaises(RarFormatError):
            RarBinaryParser.inspect(b"PK\x03\x04not a rar file")


if __name__ == "__main__":
    unittest.main()
