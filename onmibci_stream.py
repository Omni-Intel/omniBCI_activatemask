"""Local raw/filtered EEG stream contract and wire codec."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http import HTTPStatus
import json
import math
import queue
import threading
import time
from typing import Any, cast
import uuid

import numpy as np


SCHEMA_VERSION = 1
SAMPLE_RATE = 250
CHANNELS = 8
DEFAULT_CHANNELS = tuple(f"CH{i}" for i in range(1, CHANNELS + 1))
STREAM_RAW = "raw"
STREAM_FILTERED = "filtered"
STREAMS = frozenset((STREAM_RAW, STREAM_FILTERED))
STREAM_PATH = "/v1/stream"
CONTROL_PATH = "/v1/control"
UNIT_UV = "uV"
WIRE_DTYPE = "float32"
_WIRE_FLOAT_DTYPE = np.dtype("<f4")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer_array(
    value: Any,
    dtype: np.dtype,
    name: str,
    *,
    maximum: int,
) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if array.size:
        if not np.issubdtype(array.dtype, np.integer) or np.issubdtype(array.dtype, np.bool_):
            raise ValueError(f"{name} must contain integers")
        if np.any(array < 0) or np.any(array > maximum):
            raise ValueError(f"{name} contains an out-of-range value")
    return np.asarray(array, dtype=dtype).copy()


def _boolean_array(value: Any, name: str) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if array.size and array.dtype != np.dtype(bool):
        raise ValueError(f"{name} must contain booleans")
    return array.astype(bool, copy=True)


@dataclass(frozen=True)
class GapEvent:
    stream: str
    dropped_batches: int
    dropped_samples: int
    dropped_markers: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.stream, str) or self.stream not in STREAMS:
            raise ValueError(f"unsupported stream: {self.stream!r}")
        if (
            not _is_integer(self.dropped_batches)
            or not _is_integer(self.dropped_samples)
            or not _is_integer(self.dropped_markers)
            or self.dropped_batches < 0
            or self.dropped_samples < 0
            or self.dropped_markers < 0
        ):
            raise ValueError("gap counts must be non-negative")


@dataclass(frozen=True)
class MarkerEvent:
    event_id: str
    session_id: str
    recording_id: str
    code: str
    value: None | bool | int | float | str
    timestamp: float
    sequence: int | None
    duration: float
    description: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("session_id", self.session_id),
            ("recording_id", self.recording_id),
            ("code", self.code),
            ("description", self.description),
        ):
            if not isinstance(value, str) or not value:
                if name == "description" and value == "":
                    continue
                raise ValueError(f"{name} must be a non-empty string")
            if len(value) > 1024:
                raise ValueError(f"{name} is too long")

        if not isinstance(self.value, (type(None), bool, int, float, str)):
            raise ValueError("marker value must be a JSON scalar")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("marker value must be finite")

        if (
            not isinstance(self.timestamp, (int, float))
            or isinstance(self.timestamp, bool)
            or not math.isfinite(float(self.timestamp))
        ):
            raise ValueError("timestamp must be finite")
        if self.sequence is not None and (
            not _is_integer(self.sequence) or not 0 <= self.sequence <= 0xFFFFFFFF
        ):
            raise ValueError("sequence must be a uint32 or None")
        if (
            not isinstance(self.duration, (int, float))
            or isinstance(self.duration, bool)
            or not math.isfinite(float(self.duration))
            or self.duration < 0
        ):
            raise ValueError("duration must be a non-negative finite number")

        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "duration", float(self.duration))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "marker",
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "recording_id": self.recording_id,
            "code": self.code,
            "value": self.value,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "duration": self.duration,
            "description": self.description,
        }

    def to_message(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Any) -> "MarkerEvent":
        if not isinstance(value, dict) or value.get("type") != "marker":
            raise ValueError("marker event must have type=marker")
        if value.get("schema_version") != SCHEMA_VERSION or not _is_integer(
            value.get("schema_version")
        ):
            raise ValueError("unsupported marker schema version")
        required = (
            "event_id",
            "session_id",
            "recording_id",
            "code",
            "value",
            "timestamp",
            "sequence",
            "duration",
            "description",
        )
        if any(key not in value for key in required):
            raise ValueError("marker event is missing a required field")
        return cls(
            event_id=value["event_id"],
            session_id=value["session_id"],
            recording_id=value["recording_id"],
            code=value["code"],
            value=value["value"],
            timestamp=value["timestamp"],
            sequence=value["sequence"],
            duration=value["duration"],
            description=value["description"],
        )


def bdf_annotation_for_marker(
    marker: MarkerEvent,
    *,
    recording_started_at: float,
    first_sequence: int | None,
    sample_rate: int = SAMPLE_RATE,
    sample_count: int | None = None,
) -> tuple[float, float, str]:
    """Convert one marker into pyEDFlib's onset/duration/text tuple."""

    if not _is_integer(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if (
        not isinstance(recording_started_at, (int, float))
        or isinstance(recording_started_at, bool)
        or not math.isfinite(float(recording_started_at))
    ):
        raise ValueError("recording_started_at must be finite")
    if first_sequence is not None and (
        not _is_integer(first_sequence) or not 0 <= first_sequence <= 0xFFFFFFFF
    ):
        raise ValueError("first_sequence must be a uint32 or None")
    if sample_count is not None and (not _is_integer(sample_count) or sample_count < 0):
        raise ValueError("sample_count must be non-negative or None")

    onset: float
    if marker.sequence is not None and first_sequence is not None:
        delta = (marker.sequence - first_sequence) & 0xFFFFFFFF
        if sample_count is None or delta <= sample_count:
            onset = delta / float(sample_rate)
        else:
            onset = marker.timestamp - float(recording_started_at)
    else:
        onset = marker.timestamp - float(recording_started_at)
    if not math.isfinite(onset) or onset < 0:
        raise ValueError("marker onset is outside the recording")

    value_text = json.dumps(marker.value, ensure_ascii=False, separators=(",", ":"))
    text = f"{marker.code}|value={value_text}"
    if marker.description:
        text += f"|{marker.description}"
    return float(onset), float(marker.duration), text


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
        if not isinstance(self.stream, str) or self.stream not in STREAMS:
            raise ValueError(f"unsupported stream: {self.stream!r}")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must not be empty")
        if not _is_integer(self.sample_rate) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

        if isinstance(self.channels, (str, bytes)):
            raise ValueError("channels must be a sequence of strings")
        try:
            channels = tuple(self.channels)
        except TypeError as exc:
            raise ValueError("channels must be a sequence of strings") from exc
        if not channels or any(not isinstance(channel, str) or not channel for channel in channels):
            raise ValueError("channels must not be empty")

        values = np.asarray(self.values)
        if values.ndim != 2 or values.shape[1] != len(channels):
            raise ValueError("values must have shape (samples, channels)")
        values = np.ascontiguousarray(values, dtype=_WIRE_FLOAT_DTYPE).copy()

        sequence = _integer_array(
            self.sequence,
            np.dtype("<u4"),
            "sequence",
            maximum=0xFFFFFFFF,
        )
        valid = _boolean_array(self.valid, "valid")
        modes = _integer_array(
            self.modes,
            np.dtype("u1"),
            "modes",
            maximum=0xFF,
        )
        samples = values.shape[0]
        if any(array.size != samples for array in (sequence, valid, modes)):
            raise ValueError("sequence, valid, and modes must match values length")

        if self.generation is not None and (
            not _is_integer(self.generation) or self.generation < 0
        ):
            raise ValueError("generation must be non-negative or None")
        if not isinstance(self.unit, str) or not self.unit:
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
        if metadata.get("schema_version") != SCHEMA_VERSION or not _is_integer(
            metadata.get("schema_version")
        ):
            raise ValueError("unsupported stream schema version")
        if metadata.get("dtype") != WIRE_DTYPE:
            raise ValueError("unsupported stream data type")

        shape = metadata.get("shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or not all(_is_integer(value) and value >= 0 for value in shape)
        ):
            raise ValueError("invalid data shape")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("binary payload must be bytes-like")
        payload = bytes(payload)
        expected_bytes = shape[0] * shape[1] * _WIRE_FLOAT_DTYPE.itemsize
        if len(payload) != expected_bytes:
            raise ValueError("binary payload length does not match data shape")

        values = np.frombuffer(payload, dtype=_WIRE_FLOAT_DTYPE).reshape(shape).copy()
        return cls(
            stream=cast(str, metadata.get("stream")),
            values=values,
            sequence=cast(np.ndarray, metadata.get("sequence")),
            valid=cast(np.ndarray, metadata.get("valid")),
            modes=cast(np.ndarray, metadata.get("modes")),
            generation=metadata.get("generation"),
            session_id=cast(str, metadata.get("session_id")),
            sample_rate=metadata.get("sample_rate", SAMPLE_RATE),
            channels=metadata.get("channels", DEFAULT_CHANNELS),
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
    payload: bytes | None
    samples: int
    kind: str = "data"


class _Subscriber:
    def __init__(self, stream: str, queue_size: int):
        self.stream = stream
        self.queue: queue.Queue[_WirePacket] = queue.Queue(maxsize=queue_size)
        self.wakeup: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._wake_pending = False
        self.dropped_batches = 0
        self.dropped_samples = 0
        self.dropped_markers = 0

    def enqueue(self, packet: _WirePacket) -> bool:
        """Queue a packet and return whether the sender needs a wakeup."""

        with self._lock:
            try:
                self.queue.put_nowait(packet)
            except queue.Full:
                try:
                    dropped = self.queue.get_nowait()
                except queue.Empty:
                    dropped = None
                if dropped is not None:
                    self._record_drop_locked(dropped)
                try:
                    self.queue.put_nowait(packet)
                except queue.Full:
                    self._record_drop_locked(packet)

            if self._wake_pending:
                return False
            self._wake_pending = True
            return True

    def next_packet(self) -> tuple[_WirePacket, GapEvent | None] | None:
        with self._lock:
            try:
                packet = self.queue.get_nowait()
            except queue.Empty:
                self._wake_pending = False
                return None
            gap = self._take_gap_locked()
            return packet, gap

    def _take_gap_locked(self) -> GapEvent | None:
        if not self.dropped_batches and not self.dropped_markers:
            return None
        gap = GapEvent(
            self.stream,
            self.dropped_batches,
            self.dropped_samples,
            self.dropped_markers,
        )
        self.dropped_batches = 0
        self.dropped_samples = 0
        self.dropped_markers = 0
        return gap

    def take_gap(self) -> GapEvent | None:
        with self._lock:
            return self._take_gap_locked()

    def _record_drop_locked(self, packet: _WirePacket) -> None:
        if packet.kind == "marker":
            self.dropped_markers += 1
        else:
            self.dropped_batches += 1
            self.dropped_samples += packet.samples


class LocalStreamServer:
    """Thread-owned localhost WebSocket fan-out server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        queue_size: int = 32,
        session_id: str | None = None,
        stop_handler: Any = None,
        export_handler: Any = None,
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
        self.stop_handler = stop_handler
        self.export_handler = export_handler
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._subscribers: set[_Subscriber] = set()
        self._subscribers_lock = threading.Lock()
        self._recording_lock = threading.RLock()
        self._recording_id: str | None = None
        self._recording_started_at: float | None = None
        self._first_sequence: int | None = None
        self._recording_active = False
        self._markers: list[MarkerEvent] = []

    def begin_recording(self, recording_id: str, started_at: float | None = None) -> None:
        if not isinstance(recording_id, str) or not recording_id:
            raise ValueError("recording_id must not be empty")
        if started_at is None:
            started_at = time.time()
        if (
            not isinstance(started_at, (int, float))
            or isinstance(started_at, bool)
            or not math.isfinite(float(started_at))
        ):
            raise ValueError("started_at must be finite")
        with self._recording_lock:
            self._recording_id = recording_id
            self._recording_started_at = float(started_at)
            self._first_sequence = None
            self._markers = []
            self._recording_active = True

    def set_first_sequence(self, sequence: int) -> None:
        if not _is_integer(sequence) or not 0 <= sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must be a uint32")
        with self._recording_lock:
            if self._recording_active and self._first_sequence is None:
                self._first_sequence = int(sequence)

    def end_recording(self) -> None:
        with self._recording_lock:
            self._recording_active = False

    def marker_snapshot(self) -> tuple[MarkerEvent, ...]:
        with self._recording_lock:
            return tuple(self._markers)

    def recording_snapshot(self) -> dict[str, Any]:
        with self._recording_lock:
            return {
                "recording_id": self._recording_id,
                "recording_started_at": self._recording_started_at,
                "first_sequence": self._first_sequence,
                "recording_active": self._recording_active,
                "markers": tuple(self._markers),
            }

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
        with self._subscribers_lock:
            subscribers = tuple(
                subscriber for subscriber in self._subscribers if subscriber.stream == batch.stream
            )
        if not subscribers:
            return
        header, payload = batch.to_messages()
        packet = _WirePacket(header, payload, batch.values.shape[0], "data")
        self._enqueue_packet(subscribers, packet)

    def _enqueue_packet(
        self,
        subscribers: tuple[_Subscriber, ...],
        packet: _WirePacket,
    ) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        for subscriber in subscribers:
            if not subscriber.enqueue(packet):
                continue
            if subscriber.wakeup is None:
                continue
            try:
                loop.call_soon_threadsafe(subscriber.wakeup.set)
            except RuntimeError:
                return

    def accept_marker(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("marker request must be an object")
        with self._recording_lock:
            if not self._recording_active or self._recording_id is None:
                raise RuntimeError("not_recording")
            marker = MarkerEvent(
                event_id=request.get("event_id") or uuid.uuid4().hex,
                session_id=self.session_id,
                recording_id=self._recording_id,
                code=cast(str, request.get("code")),
                value=request.get("value"),
                timestamp=request.get("timestamp", time.time()),
                sequence=request.get("sequence"),
                duration=request.get("duration", 0.0),
                description=request.get("description", ""),
            )
            self._markers.append(marker)

        with self._subscribers_lock:
            subscribers = tuple(self._subscribers)
        packet = _WirePacket(marker.to_message(), None, 0, "marker")
        self._enqueue_packet(subscribers, packet)
        return marker.to_dict()

    def _control_response(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = request.get("request_id")
        response: dict[str, Any] = {
            "type": "control_response",
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
        }
        try:
            command = request.get("type")
            if command == "marker":
                result = self.accept_marker(request)
            elif command == "stop_measurement":
                if self.stop_handler is None:
                    raise RuntimeError("unsupported")
                result = self.stop_handler()
            elif command == "export_bdf":
                state = self.recording_snapshot()
                if state["recording_active"]:
                    raise RuntimeError("not_stopped")
                if self.export_handler is None:
                    raise RuntimeError("unsupported")
                result = self.export_handler(request, state["markers"])
            else:
                raise ValueError("unsupported command")
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise RuntimeError("control handler must return an object")
            response["ok"] = True
            response["result"] = result
        except RuntimeError as exc:
            code = str(exc) or "handler_error"
            response["ok"] = False
            response["error"] = {"code": code, "message": code}
        except (TypeError, ValueError) as exc:
            response["ok"] = False
            response["error"] = {"code": "invalid_request", "message": str(exc)}
        except Exception as exc:
            response["ok"] = False
            response["error"] = {"code": "handler_error", "message": str(exc)}
        return response

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
                process_request=self._process_request,
                max_size=64 * 1024,
            ) as server:
                server_socket = next(iter(server.sockets))
                self.port = int(server_socket.getsockname()[1])
                self._ready.set()
                await self._stop_event.wait()
        finally:
            with self._subscribers_lock:
                self._subscribers.clear()

    @staticmethod
    async def _process_request(connection: Any, request: Any) -> Any:
        if request.path not in (STREAM_PATH, CONTROL_PATH):
            return connection.respond(HTTPStatus.NOT_FOUND, "invalid stream path\n")
        return None

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

            request_path = getattr(getattr(websocket, "request", None), "path", None)
            if request_path == CONTROL_PATH and message.get("type") != "subscribe":
                await websocket.send(
                    json.dumps(
                        self._control_response(message),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return

            stream = message.get("stream")
            if message.get("type") != "subscribe" or stream not in STREAMS:
                await websocket.close(code=1008, reason="invalid stream subscription")
                return

            subscriber = _Subscriber(stream, self.queue_size)
            subscriber.wakeup = asyncio.Event()
            with self._subscribers_lock:
                self._subscribers.add(subscriber)
            await websocket.send(json.dumps(self._hello(stream), separators=(",", ":")))
            sender = asyncio.create_task(self._send_packets(websocket, subscriber))
            await websocket.wait_closed()
        finally:
            if subscriber is not None:
                with self._subscribers_lock:
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
            assert subscriber.wakeup is not None
            await subscriber.wakeup.wait()
            subscriber.wakeup.clear()
            while True:
                item = subscriber.next_packet()
                if item is None:
                    break
                packet, gap = item
                if gap is not None:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "gap",
                                "stream": gap.stream,
                                "dropped_batches": gap.dropped_batches,
                                "dropped_samples": gap.dropped_samples,
                                "dropped_markers": gap.dropped_markers,
                            },
                            separators=(",", ":"),
                        )
                    )
                await websocket.send(packet.header)
                if packet.payload is not None:
                    await websocket.send(packet.payload)
