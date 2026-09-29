"""A tiny Matroska writer for tests.

Writes a valid MKV with PCM audio tracks (with optional language and name) and a
single Cluster, which both MediaInfo and ffprobe can read. Note that Matroska's
default language is "eng", so pass "und" for an unlabeled track.
"""
from __future__ import annotations

import struct
from pathlib import Path


def _id(element_id: int) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def element(element_id: int, payload: bytes) -> bytes:
    return _id(element_id) + b"\x01" + len(payload).to_bytes(7, "big") + payload  # 8-byte size


def uint(element_id: int, value: int) -> bytes:
    return element(element_id, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def text(element_id: int, value: str) -> bytes:
    return element(element_id, value.encode("utf-8"))


def float64(element_id: int, value: float) -> bytes:
    return element(element_id, struct.pack(">d", value))


def write_mkv(path: Path | str, tracks: list[tuple[str | None, str | None]], channels: int = 2) -> Path:
    """``tracks`` is a list of (language, name); None leaves the element out."""
    header = element(0x1A45DFA3, uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4) + uint(0x42F3, 8)
                     + text(0x4282, "matroska") + uint(0x4287, 4) + uint(0x4285, 2))
    info = element(0x1549A966, uint(0x2AD7B1, 1_000_000) + text(0x4D80, "dubchecker-tests")
                   + text(0x5741, "dubchecker-tests") + float64(0x4489, 1.0))
    entries = b""
    for number, (language, name) in enumerate(tracks, 1):
        body = uint(0xD7, number) + uint(0x73C5, number) + uint(0x83, 2) + text(0x86, "A_PCM/INT/LIT")
        if language is not None:
            body += text(0x22B59C, language)
        if name:
            body += text(0x536E, name)
        body += element(0xE1, float64(0xB5, 48000.0) + uint(0x9F, channels) + uint(0x6264, 16))
        entries += element(0xAE, body)
    frame = bytes(48 * channels * 2)  # 1 ms of 16-bit silence
    blocks = b"".join(element(0xA3, bytes([0x80 | n]) + struct.pack(">h", 0) + b"\x80" + frame)
                      for n in range(1, len(tracks) + 1))
    cluster = element(0x1F43B675, uint(0xE7, 0) + blocks)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + element(0x18538067, info + element(0x1654AE6B, entries) + cluster))
    return path


JPN = ("jpn", "Japanese")
ENG = ("eng", "English")
UND = ("und", None)
