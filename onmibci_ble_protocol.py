"""OmniBCI BLE device-control protocol V1."""

from dataclasses import dataclass


MAGIC = b"\xBC\x52"
PROTOCOL_VERSION = 1
HEADER_BYTES = 8
CRC_BYTES = 2

MSG_HELLO = 0x01
MSG_GET_CONFIG = 0x02
MSG_SET_CONFIG = 0x03
MSG_PING = 0x04
MSG_RESPONSE = 0x80


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    message_type: int
    request_id: int
    payload: bytes


@dataclass(frozen=True)
class ConfigSnapshot:
    generation: int
    mode: int
    verified: bool
    enabled_mask: int
    bias_mask: int
    lead_off_mask: int
    config1: int
    config2: int
    config3: int
    channel_registers: tuple[int, ...]
    bias_p: int
    bias_n: int
    lead_off_p: int
    lead_off_n: int
    misc1: int


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_packet(message_type: int, request_id: int, payload: bytes = b"") -> bytes:
    payload = bytes(payload)
    if len(payload) > 0xFFFF:
        raise ProtocolError("payload length exceeds 65535 bytes")
    header = bytes((
        MAGIC[0], MAGIC[1], PROTOCOL_VERSION, message_type & 0xFF,
        request_id & 0xFF, (request_id >> 8) & 0xFF,
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF,
    ))
    body = header + payload
    return body + crc16_ccitt(body).to_bytes(2, "little")


def decode_packet(data: bytes) -> Packet:
    data = bytes(data)
    if len(data) < HEADER_BYTES + CRC_BYTES:
        raise ProtocolError("packet length is too short")
    if data[:2] != MAGIC:
        raise ProtocolError("invalid packet magic")
    if data[2] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {data[2]}")
    payload_length = int.from_bytes(data[6:8], "little")
    expected_length = HEADER_BYTES + payload_length + CRC_BYTES
    if len(data) != expected_length:
        raise ProtocolError(f"packet length {len(data)} != {expected_length}")
    expected_crc = int.from_bytes(data[-2:], "little")
    if crc16_ccitt(data[:-2]) != expected_crc:
        raise ProtocolError("packet CRC mismatch")
    return Packet(
        message_type=data[3],
        request_id=int.from_bytes(data[4:6], "little"),
        payload=data[8:-2],
    )


def encode_set_config(mode: int, enabled_mask: int, bias_mask: int,
                      lead_off_mask: int, gains) -> bytes:
    gains = tuple(int(value) for value in gains)
    if len(gains) != 8:
        raise ProtocolError("SET_CONFIG requires eight gains")
    if any(value not in (1, 2, 4, 6, 8, 12, 24) for value in gains):
        raise ProtocolError("invalid ADS1299 gain")
    return bytes((mode & 0xFF, enabled_mask & 0xFF, bias_mask & 0xFF,
                  lead_off_mask & 0xFF, *gains))


def decode_config_snapshot(payload: bytes) -> ConfigSnapshot:
    payload = bytes(payload)
    if len(payload) != 26:
        raise ProtocolError(f"config snapshot length {len(payload)} != 26")
    if payload[0] != 0:
        raise ProtocolError(f"device rejected request with result {payload[0]}")
    return ConfigSnapshot(
        generation=int.from_bytes(payload[1:5], "little"),
        mode=payload[5],
        verified=bool(payload[6]),
        enabled_mask=payload[7],
        bias_mask=payload[8],
        lead_off_mask=payload[9],
        config1=payload[10],
        config2=payload[11],
        config3=payload[12],
        channel_registers=tuple(payload[13:21]),
        bias_p=payload[21],
        bias_n=payload[22],
        lead_off_p=payload[23],
        lead_off_n=payload[24],
        misc1=payload[25],
    )
