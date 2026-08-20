"""Synchronous client for the local OmniBCI EEG stream API."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import os
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
import uuid

from websockets.exceptions import ConnectionClosedOK

from onmibci_stream import (
    CONTROL_PATH,
    DEFAULT_CHANNELS,
    GapEvent,
    MarkerEvent,
    SAMPLE_RATE,
    SCHEMA_VERSION,
    STREAM_PATH,
    STREAM_FILTERED,
    STREAM_RAW,
    StreamBatch,
    UNIT_UV,
)


class ProtocolError(RuntimeError):
    """Raised when the API sends an invalid or unexpected message."""


@dataclass(frozen=True)
class ExportResult:
    path: str
    recording_id: str
    event_count: int
    sample_count: int


@dataclass
class _StreamIterator:
    client: "LocalClient"
    stream: str
    connection: Any

    def __iter__(self) -> "_StreamIterator":
        return self

    def __next__(self) -> StreamBatch | GapEvent | MarkerEvent:
        try:
            message = self.connection.recv()
        except ConnectionClosedOK:
            self.close()
            raise StopIteration
        if isinstance(message, bytes):
            self.close()
            raise ProtocolError("expected a JSON event before binary data")
        try:
            event = json.loads(message)
        except json.JSONDecodeError as exc:
            self.close()
            raise ProtocolError("API sent invalid JSON") from exc
        if not isinstance(event, dict):
            self.close()
            raise ProtocolError("API event must be a JSON object")

        event_type = event.get("type")
        if event_type == "gap":
            if event.get("stream") != self.stream:
                self.close()
                raise ProtocolError("API gap stream does not match subscription")
            try:
                return GapEvent(
                    stream=event["stream"],
                    dropped_batches=event["dropped_batches"],
                    dropped_samples=event["dropped_samples"],
                    dropped_markers=event.get("dropped_markers", 0),
                )
            except (KeyError, TypeError, ValueError) as exc:
                self.close()
                raise ProtocolError("API sent an invalid gap event") from exc

        if event_type == "marker":
            try:
                return MarkerEvent.from_dict(event)
            except (TypeError, ValueError, KeyError) as exc:
                self.close()
                raise ProtocolError("API sent an invalid marker event") from exc

        if event_type != "data":
            self.close()
            raise ProtocolError(f"unexpected API event type: {event_type!r}")
        if event.get("stream") != self.stream:
            self.close()
            raise ProtocolError("API data stream does not match subscription")

        payload = self.connection.recv()
        if not isinstance(payload, bytes):
            self.close()
            raise ProtocolError("API data event was not followed by binary payload")
        try:
            return StreamBatch.from_messages(message, payload)
        except (TypeError, ValueError, KeyError) as exc:
            self.close()
            raise ProtocolError("API sent an invalid data batch") from exc

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "_StreamIterator":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class LocalClient:
    def __init__(
        self,
        url: str = "ws://127.0.0.1:8765/v1/stream",
        timeout: float = 5.0,
    ):
        self.url = url
        self.timeout = timeout
        self.control_url = self._derive_control_url(url)
        self.hello: dict[str, Any] | None = None

    def stream_raw(self) -> Iterator[StreamBatch | GapEvent | MarkerEvent]:
        return self._open_stream(STREAM_RAW)

    def stream_filtered(self) -> Iterator[StreamBatch | GapEvent | MarkerEvent]:
        return self._open_stream(STREAM_FILTERED)

    def send_marker(
        self,
        code: str,
        value: None | bool | int | float | str = None,
        *,
        timestamp: float | None = None,
        sequence: int | None = None,
        duration: float = 0.0,
        description: str = "",
    ) -> MarkerEvent:
        request: dict[str, Any] = {
            "type": "marker",
            "code": code,
            "value": value,
            "sequence": sequence,
            "duration": duration,
            "description": description,
        }
        if timestamp is not None:
            request["timestamp"] = timestamp
        result = self._control_request(request)
        try:
            return MarkerEvent.from_dict(result)
        except (TypeError, ValueError, KeyError) as exc:
            raise ProtocolError("API returned an invalid marker result") from exc

    def stop_measurement(self) -> dict[str, Any]:
        return self._control_request({"type": "stop_measurement"})

    def export_bdf(self, path: os.PathLike[str] | str, *, overwrite: bool = False) -> ExportResult:
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")
        result = self._control_request(
            {
                "type": "export_bdf",
                "path": os.fspath(path),
                "overwrite": overwrite,
            }
        )
        if (
            not isinstance(result.get("path"), str)
            or not isinstance(result.get("recording_id"), str)
            or not result["recording_id"]
            or not isinstance(result.get("event_count"), int)
            or isinstance(result["event_count"], bool)
            or result["event_count"] < 0
            or not isinstance(result.get("sample_count"), int)
            or isinstance(result["sample_count"], bool)
            or result["sample_count"] < 0
        ):
            raise ProtocolError("API returned an invalid BDF export result")
        return ExportResult(
            path=result["path"],
            recording_id=result["recording_id"],
            event_count=result["event_count"],
            sample_count=result["sample_count"],
        )

    @staticmethod
    def _derive_control_url(url: str) -> str:
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        parsed = urlsplit(url)
        if parsed.path != STREAM_PATH:
            raise ValueError(f"stream URL must end with {STREAM_PATH}")
        return urlunsplit(
            (parsed.scheme, parsed.netloc, CONTROL_PATH, parsed.query, parsed.fragment)
        )

    def _control_request(self, request: dict[str, Any]) -> dict[str, Any]:
        from websockets.sync.client import connect

        request = dict(request)
        request_id = uuid.uuid4().hex
        request["request_id"] = request_id
        connect_options: dict[str, Any] = {
            "open_timeout": self.timeout,
            "close_timeout": self.timeout,
            "max_size": None,
        }
        if "proxy" in inspect.signature(connect).parameters:
            connect_options["proxy"] = None
        try:
            with connect(self.control_url, **connect_options) as connection:
                connection.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
                message = connection.recv()
        except Exception as exc:
            raise ProtocolError("could not complete API control request") from exc

        if not isinstance(message, str):
            raise ProtocolError("API control response must be JSON text")
        try:
            response = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError("API sent invalid control JSON") from exc
        if not isinstance(response, dict):
            raise ProtocolError("API control response must be an object")
        if response.get("type") != "control_response":
            raise ProtocolError("API did not send a control response")
        if response.get("schema_version") != SCHEMA_VERSION:
            raise ProtocolError("API control response has an unsupported schema")
        if response.get("request_id") != request_id:
            raise ProtocolError("API control response request ID does not match")
        if not response.get("ok"):
            error = response.get("error")
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message = error.get("message", "control request failed")
                raise ProtocolError(f"API control error {code}: {message}")
            raise ProtocolError("API control request failed")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise ProtocolError("API control result must be an object")
        return result

    def _open_stream(self, stream: str) -> _StreamIterator:
        from websockets.sync.client import connect

        connect_options: dict[str, Any] = {
            "open_timeout": self.timeout,
            "close_timeout": self.timeout,
            "max_size": None,
        }
        if "proxy" in inspect.signature(connect).parameters:
            connect_options["proxy"] = None
        connection = connect(self.url, **connect_options)
        try:
            connection.send(
                json.dumps(
                    {"type": "subscribe", "stream": stream},
                    separators=(",", ":"),
                )
            )
            hello_message = connection.recv()
            self.hello = self._validate_hello(hello_message, stream)
            return _StreamIterator(self, stream, connection)
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate_hello(message: Any, stream: str) -> dict[str, Any]:
        if not isinstance(message, str):
            raise ProtocolError("API hello must be a JSON string")
        try:
            hello = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError("API sent invalid hello JSON") from exc
        if not isinstance(hello, dict):
            raise ProtocolError("API hello must be a JSON object")
        if hello.get("type") != "hello":
            raise ProtocolError("API did not send hello")
        if (
            hello.get("schema_version") != SCHEMA_VERSION
            or not isinstance(hello.get("schema_version"), int)
            or isinstance(hello.get("schema_version"), bool)
        ):
            raise ProtocolError("API hello has an unsupported schema version")
        if hello.get("stream") != stream:
            raise ProtocolError("API hello stream does not match subscription")
        if not isinstance(hello.get("session_id"), str) or not hello.get("session_id"):
            raise ProtocolError("API hello has no session_id")
        if (
            hello.get("sample_rate") != SAMPLE_RATE
            or not isinstance(hello.get("sample_rate"), int)
            or isinstance(hello.get("sample_rate"), bool)
        ):
            raise ProtocolError("API hello has an unsupported sample rate")
        if hello.get("channels") != list(DEFAULT_CHANNELS):
            raise ProtocolError("API hello has unsupported channels")
        if hello.get("unit") != UNIT_UV:
            raise ProtocolError("API hello has an unsupported unit")
        return hello


def connect_local(
    host: str = "127.0.0.1",
    port: int = 8765,
    timeout: float = 5.0,
) -> LocalClient:
    """Create a client for the GUI's localhost API."""

    if host != "127.0.0.1":
        raise ValueError("the local SDK only connects to 127.0.0.1")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return LocalClient(f"ws://{host}:{port}/v1/stream", timeout=timeout)


__all__ = [
    "ExportResult",
    "GapEvent",
    "LocalClient",
    "MarkerEvent",
    "ProtocolError",
    "StreamBatch",
    "connect_local",
]
