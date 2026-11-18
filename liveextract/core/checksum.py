"""
Streaming integrity and checksum utilities (CRC-32 and SHA-256).
"""

import zlib
import hashlib
from typing import Union


class StreamingCRC32:
    """
    Lockstep streaming CRC-32 accumulator.
    Computes CRC-32 on-the-fly as uncompressed chunks are emitted from the sliding window.
    """

    def __init__(self, initial_value: int = 0):
        self._value = initial_value

    def update(self, chunk: bytes) -> int:
        """Accumulate a new byte chunk into the running CRC-32."""
        self._value = zlib.crc32(chunk, self._value)
        return self._value & 0xFFFFFFFF

    @property
    def value(self) -> int:
        """Return the current 32-bit unsigned integer value."""
        return self._value & 0xFFFFFFFF

    def hexdigest(self) -> str:
        """Return formatted 8-character uppercase hex string (e.g., '3614F607')."""
        return f"{self.value:08X}"

    def verify(self, expected: Union[int, str]) -> bool:
        """
        Verify the accumulated CRC against an expected integer or hex string.
        """
        if isinstance(expected, str):
            try:
                expected_int = int(expected, 16)
            except ValueError:
                return False
        else:
            expected_int = expected & 0xFFFFFFFF
        return self.value == expected_int

    def reset(self) -> None:
        """Reset the accumulator to zero."""
        self._value = 0


class StreamingHasher:
    """
    Combined streaming CRC-32 and optional SHA-256 calculator for high-security environments.
    """

    def __init__(self, compute_sha256: bool = False):
        self.crc = StreamingCRC32()
        self.sha256 = hashlib.sha256() if compute_sha256 else None

    def update(self, chunk: bytes) -> None:
        self.crc.update(chunk)
        if self.sha256:
            self.sha256.update(chunk)

    @property
    def crc32_value(self) -> int:
        return self.crc.value

    @property
    def crc32_hex(self) -> str:
        return self.crc.hexdigest()

    @property
    def sha256_hex(self) -> str:
        return self.sha256.hexdigest() if self.sha256 else ""
