"""Local raw/filtered EEG stream contract and wire codec."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import numpy as np


SCHEMA_VERSION = 1
SAMPLE_RATE = 250
CHANNELS = 8
DEFAULT_CHANNELS = tuple(f"CH{i}" for i in range(1, CHANNELS + 1))
STREAM_RAW = "raw"
STREAM_FILTERED = "filtered"
STREAMS = frozenset((STREAM_RAW, STREAM_FILTERED))
UNIT_UV = "uV"
WIRE_DTYPE = "float32"
_WIRE_FLOAT_DTYPE = np.dtype("<f4")


def _one_dimensional_array(value: Any, dtype: np.dtype, name: str) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    return np.asarray(array, dtype=dtype).copy()


@dataclass(frozen=True)
class GapEvent:
    stream: str
    dropped_batches: int
    dropped_samples: int

    def __post_init__(self) -> None:
        if self.stream not in STREAMS:
            raise ValueError(f"unsupported stream: {self.stream!r}")
        if self.dropped_batches < 0 or self.dropped_samples < 0:
            raise ValueError("gap counts must be non-negative")


@dataclass(frozen=True)
class StreamBatch:
    stream: str
    values: np.ndarray
    sequence: np.ndarray
    valid: np.ndarray
    modes: np.ndarray
    generation: int | None
    session_id: str
    sample_rate: int = SAMPLE_RATE
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    unit: str = UNIT_UV

    def __post_init__(self) -> None:
        if self.stream not in STREAMS:
            raise ValueError(f"unsupported stream: {self.stream!r}")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

        channels = tuple(str(channel) for channel in self.channels)
        if not channels:
            raise ValueError("channels must not be empty")

        values = np.asarray(self.values)
        if values.ndim != 2 or values.shape[1] != len(channels):
            raise ValueError("values must have shape (samples, channels)")
        values = np.ascontiguousarray(values, dtype=_WIRE_FLOAT_DTYPE).copy()

        sequence = _one_dimensional_array(self.sequence, np.dtype("<u4"), "sequence")
        valid = _one_dimensional_array(self.valid, np.dtype(bool), "valid")
        modes = _one_dimensional_array(self.modes, np.dtype("u1"), "modes")
        samples = values.shape[0]
        if any(array.size != samples for array in (sequence, valid, modes)):
            raise ValueError("sequence, valid, and modes must match values length")

        if self.generation is not None and self.generation < 0:
            raise ValueError("generation must be non-negative or None")
        if not self.unit:
            raise ValueError("unit must not be empty")

        object.__setattr__(self, "values", values)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "valid", valid)
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "channels", channels)

    @classmethod
    def from_gui_matrix(
        cls,
        *,
        stream: str,
        values: np.ndarray,
        sequence: np.ndarray,
        valid: np.ndarray,
        modes: np.ndarray,
        generation: int | None,
        session_id: str,
        sample_rate: int = SAMPLE_RATE,
        channels: tuple[str, ...] = DEFAULT_CHANNELS,
        unit: str = UNIT_UV,
    ) -> "StreamBatch":
        gui_values = np.asarray(values)
        if gui_values.ndim != 2 or gui_values.shape[0] != len(channels):
            raise ValueError("GUI values must have shape (channels, samples)")
        wire_values = np.ascontiguousarray(gui_values.T, dtype=_WIRE_FLOAT_DTYPE).copy()
        return cls(
            stream=stream,
            values=wire_values,
            sequence=sequence,
            valid=valid,
            modes=modes,
            generation=generation,
            session_id=session_id,
            sample_rate=sample_rate,
            channels=channels,
            unit=unit,
        )

    def to_messages(self) -> tuple[str, bytes]:
        header = {
            "type": "data",
            "schema_version": SCHEMA_VERSION,
            "stream": self.stream,
            "session_id": self.session_id,
            "generation": self.generation,
            "sample_rate": self.sample_rate,
            "channels": list(self.channels),
            "unit": self.unit,
            "dtype": WIRE_DTYPE,
            "shape": list(self.values.shape),
            "sequence": [int(value) for value in self.sequence],
            "valid": [bool(value) for value in self.valid],
            "modes": [int(value) for value in self.modes],
        }
        payload = np.ascontiguousarray(self.values, dtype=_WIRE_FLOAT_DTYPE).tobytes()
        return json.dumps(header, separators=(",", ":")), payload

    @classmethod
    def from_messages(cls, header: str, payload: bytes) -> "StreamBatch":
        if not isinstance(header, str):
            raise TypeError("data header must be a JSON string")
        try:
            metadata = json.loads(header)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid data header JSON") from exc
        if not isinstance(metadata, dict) or metadata.get("type") != "data":
            raise ValueError("data header must have type=data")
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported stream schema version")
        if metadata.get("dtype") != WIRE_DTYPE:
            raise ValueError("unsupported stream data type")

        shape = metadata.get("shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or not all(isinstance(value, int) and value >= 0 for value in shape)
        ):
            raise ValueError("invalid data shape")
        expected_bytes = shape[0] * shape[1] * _WIRE_FLOAT_DTYPE.itemsize
        if len(payload) != expected_bytes:
            raise ValueError("binary payload length does not match data shape")

        values = np.frombuffer(payload, dtype=_WIRE_FLOAT_DTYPE).reshape(shape).copy()
        return cls(
            stream=metadata.get("stream"),
            values=values,
            sequence=metadata.get("sequence"),
            valid=metadata.get("valid"),
            modes=metadata.get("modes"),
            generation=metadata.get("generation"),
            session_id=metadata.get("session_id"),
            sample_rate=metadata.get("sample_rate", SAMPLE_RATE),
            channels=tuple(metadata.get("channels", DEFAULT_CHANNELS)),
            unit=metadata.get("unit", UNIT_UV),
        )


def publish_gui_matrix(
    server: Any,
    *,
    stream: str,
    values: np.ndarray,
    sequence: np.ndarray,
    valid: np.ndarray,
    modes: np.ndarray,
    generation: int | None,
    session_id: str,
) -> StreamBatch:
    """Copy one GUI matrix, publish it, and return the immutable batch."""

    batch = StreamBatch.from_gui_matrix(
        stream=stream,
        values=values,
        sequence=sequence,
        valid=valid,
        modes=modes,
        generation=generation,
        session_id=session_id,
    )
    server.publish(batch)
    return batch
