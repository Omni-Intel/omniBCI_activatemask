#!/usr/bin/env python3
from __future__ import annotations

from active_mask_gui import FRAME_LEN, SYNC, crc16_ccitt_false, parse_active_mask_ack, parse_frame


def u16le(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def u32le(value: int) -> bytes:
    return bytes(
        (
            value & 0xFF,
            (value >> 8) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 24) & 0xFF,
        )
    )


def build_frame(seq: int) -> bytes:
    frame = bytearray(FRAME_LEN)
    frame[0:2] = SYNC
    frame[2] = 1
    frame[3] = 1
    frame[4:8] = u32le(seq)
    frame[8:12] = u32le(123456)
    frame[12:15] = b"\xC0\x00\x00"
    frame[15] = 0xA3
    for index in range(8):
        base = 16 + 3 * index
        frame[base : base + 3] = bytes((0, 0, index + 1))
    frame[40:42] = u16le(700)
    frame[42] = 1
    frame[43] = 1
    frame[44] = 0
    frame[45] = 0
    frame[46:48] = u16le(crc16_ccitt_false(frame[:46]))
    return bytes(frame)


def main() -> None:
    frame = build_frame(42)
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed["seq"] == 42
    assert parsed["mode"] == 1
    assert parsed["flags"] == 0xA3

    damaged = bytearray(frame)
    damaged[20] ^= 0x01
    assert parse_frame(bytes(damaged)) is None

    assert parse_active_mask_ack("#ACK activeMask=0x01 streaming=0 wasStreaming=1") == (0x01, True)
    assert parse_active_mask_ack("activeMask=0xFF") == (0xFF, False)
    assert parse_active_mask_ack("#ACK no mask") is None
    print("self_check ok")


if __name__ == "__main__":
    main()
