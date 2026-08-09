"""Local raw/filtered EEG stream contract and wire codec."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import threading
from typing import Any
import uuid

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


@dataclass(frozen=True)
class _WirePacket:
    header: str
    payload: bytes
    samples: int


class _Subscriber:
    def __init__(self, stream: str, queue_size: int):
        self.stream = stream
        self.queue: asyncio.Queue[_WirePacket] = asyncio.Queue(maxsize=queue_size)
        self.dropped_batches = 0
        self.dropped_samples = 0

    def enqueue(self, packet: _WirePacket) -> None:
        if self.queue.full():
            try:
                dropped = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped = None
            if dropped is not None:
                self.dropped_batches += 1
                self.dropped_samples += dropped.samples
        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.dropped_batches += 1
            self.dropped_samples += packet.samples

    def take_gap(self) -> GapEvent | None:
        if not self.dropped_batches:
            return None
        gap = GapEvent(self.stream, self.dropped_batches, self.dropped_samples)
        self.dropped_batches = 0
        self.dropped_samples = 0
        return gap


class LocalStreamServer:
    """Thread-owned localhost WebSocket fan-out server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        queue_size: int = 32,
        session_id: str | None = None,
    ):
        if host != "127.0.0.1":
            raise ValueError("the local stream server must bind to 127.0.0.1")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.host = host
        self.port = port
        self.queue_size = queue_size
        self.session_id = session_id or uuid.uuid4().hex
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._subscribers: set[_Subscriber] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="OmniBCI-StreamAPI",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("local stream server did not start")
        if self._startup_error is not None:
            raise RuntimeError("local stream server failed to start") from self._startup_error

    def stop(self, timeout: float = 3.0) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise RuntimeError("local stream server did not stop")
        self._thread = None
        self._loop = None
        self._stop_event = None

    def publish(self, batch: StreamBatch) -> None:
        if not isinstance(batch, StreamBatch):
            raise TypeError("publish expects a StreamBatch")
        if batch.session_id != self.session_id:
            raise ValueError("batch session_id does not match the stream server")
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._publish(batch), loop)
        future.add_done_callback(self._consume_publish_result)

    @staticmethod
    def _consume_publish_result(future: "asyncio.Future[Any]") -> None:
        try:
            future.result()
        except (asyncio.CancelledError, RuntimeError):
            pass

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # surfaced through start()
            self._startup_error = exc
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            async with serve(
                self._handle_client,
                self.host,
                self.port,
                max_size=None,
            ) as server:
                self.port = int(server.sockets[0].getsockname()[1])
                self._ready.set()
                await self._stop_event.wait()
        finally:
            self._subscribers.clear()

    async def _handle_client(self, websocket: Any) -> None:
        subscriber: _Subscriber | None = None
        sender: asyncio.Task[None] | None = None
        try:
            request = await websocket.recv()
            if not isinstance(request, str):
                await websocket.close(code=1003, reason="subscription must be JSON")
                return
            try:
                message = json.loads(request)
            except json.JSONDecodeError:
                await websocket.close(code=1003, reason="invalid subscription JSON")
                return
            if not isinstance(message, dict):
                await websocket.close(code=1008, reason="subscription must be an object")
                return
            stream = message.get("stream")
            if message.get("type") != "subscribe" or stream not in STREAMS:
                await websocket.close(code=1008, reason="invalid stream subscription")
                return

            subscriber = _Subscriber(stream, self.queue_size)
            self._subscribers.add(subscriber)
            await websocket.send(json.dumps(self._hello(stream), separators=(",", ":")))
            sender = asyncio.create_task(self._send_packets(websocket, subscriber))
            await websocket.wait_closed()
        finally:
            if subscriber is not None:
                self._subscribers.discard(subscriber)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    def _hello(self, stream: str) -> dict[str, Any]:
        return {
            "type": "hello",
            "schema_version": SCHEMA_VERSION,
            "stream": stream,
            "session_id": self.session_id,
            "sample_rate": SAMPLE_RATE,
            "channels": list(DEFAULT_CHANNELS),
            "unit": UNIT_UV,
        }

    async def _send_packets(self, websocket: Any, subscriber: _Subscriber) -> None:
        while True:
            packet = await subscriber.queue.get()
            gap = subscriber.take_gap()
            if gap is not None:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "gap",
                            "stream": gap.stream,
                            "dropped_batches": gap.dropped_batches,
                            "dropped_samples": gap.dropped_samples,
                        },
                        separators=(",", ":"),
                    )
                )
            await websocket.send(packet.header)
            await websocket.send(packet.payload)

    async def _publish(self, batch: StreamBatch) -> None:
        header, payload = batch.to_messages()
        packet = _WirePacket(header, payload, batch.values.shape[0])
        for subscriber in tuple(self._subscribers):
            if subscriber.stream == batch.stream:
                subscriber.enqueue(packet)
