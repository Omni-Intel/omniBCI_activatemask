# Event Markers and BDF Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing localhost EEG stream API and Python SDK with real-time event markers and a post-measurement command that exports the complete recording as BDF+ with standard annotations.

**Architecture:** Keep the existing `/v1/stream` raw/filtered data protocol and add a separate `/v1/control` WebSocket for one-request/one-response commands. `LocalStreamServer` owns validated marker-session state and broadcasts marker JSON events to stream subscribers. The GUI starts and ends that state with the existing recording lifecycle; export reads the completed segmented BIN files, reconstructs the same USB/BLE timeline, and writes BDF+ annotations using sequence alignment first and wall-clock timestamps as fallback. The SDK remains synchronous and opens a short-lived control connection for marking, stopping, and exporting.

**Tech Stack:** Python 3.12, NumPy, `websockets` 14-15, pyEDFlib optional export dependency, PySide6 GUI, standard-library `unittest`.

## Global Constraints

- Keep the server bound to `127.0.0.1`; do not add LAN, cloud, TLS, or authentication scope.
- Preserve existing raw/filtered stream framing and USB/BLE shared `process_frames(..., live=True)` publishing.
- A marker has a validated code, JSON scalar value, UNIX timestamp, optional acquisition sequence, non-negative duration, description, event ID, API session ID, and recording ID.
- Markers are accepted only while a recording is active, retained until the next recording starts, broadcast to raw/filtered subscribers, and written to the exported BDF+ Annotation channel.
- BDF onset uses `(marker.sequence - first_recording_sequence) / 250` when the sequence is present and plausible; otherwise it uses `marker.timestamp - recording_started_at`.
- SDK export asks the GUI/API to export completed recording data; it must not reconstruct a BDF from the SDK's streamed batches.
- Use `unittest`; do not add pytest or another test framework. Write each new behavior test before its implementation and run it red first.
- Do not overwrite an existing export path unless the SDK/API request explicitly sets `overwrite=True`.

---

### Task 1: Define marker and export control contracts

**Files:**
- Modify: `onmibci_stream.py`
- Test: `tests/test_onmibci_stream.py`

**Interfaces:**
- `MarkerEvent` is a frozen dataclass with `event_id`, `session_id`, `recording_id`, `code`, `value`, `timestamp`, `sequence`, `duration`, and `description`.
- `MarkerEvent.to_dict() -> dict` and `MarkerEvent.from_dict(mapping) -> MarkerEvent` validate JSON-compatible scalar values and finite numeric fields.
- `MarkerEvent.to_message() -> str` emits a marker JSON event with `type="marker"` and `schema_version=1`.
- `bdf_annotation_for_marker(marker, *, recording_started_at, first_sequence, sample_rate=250, sample_count=None) -> tuple[float, float, str]` returns the BDF onset, duration, and text without importing pyEDFlib.

- [ ] **Step 1: Write the failing tests**

```python
def test_marker_round_trip_preserves_event_fields(self):
    marker = MarkerEvent(
        event_id="evt-1", session_id="api-1", recording_id="rec-1",
        code="stimulus_on", value=1, timestamp=100.25,
        sequence=125, duration=0.2, description="left",
    )
    decoded = MarkerEvent.from_dict(json.loads(marker.to_message()))
    self.assertEqual(decoded, marker)

def test_bdf_annotation_prefers_sequence_alignment(self):
    marker = MarkerEvent(
        event_id="evt-1", session_id="api-1", recording_id="rec-1",
        code="button", value="A", timestamp=999.0,
        sequence=1125, duration=0.0, description="press",
    )
    onset, duration, text = bdf_annotation_for_marker(
        marker, recording_started_at=100.0, first_sequence=1000,
        sample_rate=250, sample_count=2000,
    )
    self.assertAlmostEqual(onset, 0.5)
    self.assertEqual(duration, 0.0)
    self.assertIn("button", text)

def test_marker_rejects_complex_value_and_negative_duration(self):
    with self.assertRaises(ValueError):
        MarkerEvent("e", "s", "r", "x", {"not": "scalar"}, 1.0, None, 0.0, "")
    with self.assertRaises(ValueError):
        MarkerEvent("e", "s", "r", "x", 1, 1.0, None, -1.0, "")
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_stream -v`

Expected: FAIL because `MarkerEvent` and `bdf_annotation_for_marker` do not yet exist.

- [ ] **Step 3: Implement the minimal contract**

Add strict validation for non-empty bounded strings, JSON scalar `value`, finite timestamp/duration, and non-negative `sequence`. Serialize `None` sequence explicitly. Use sequence onset only when the unsigned sequence delta is no greater than `sample_count` (when supplied); otherwise use the recording-relative timestamp and reject a negative final onset. Format annotation text as `code|value=<JSON value>` with `|<description>` only when a description exists.

- [ ] **Step 4: Run focused tests and verify green**

Run: `uv run python -m unittest tests.test_onmibci_stream -v`

Expected: all existing stream tests and the new marker/annotation tests pass.

### Task 2: Add the control WebSocket and marker fan-out

**Files:**
- Modify: `onmibci_stream.py`
- Modify: `tests/test_onmibci_server.py`

**Interfaces:**
- `LocalStreamServer(..., stop_handler=None, export_handler=None)` accepts optional command callbacks.
- `begin_recording(recording_id, started_at=None)`, `set_first_sequence(sequence)`, and `end_recording()` manage the current marker session.
- `/v1/control` accepts one JSON command and returns `{"type":"control_response","ok":true,"result":...}` or a structured error.
- Commands are `marker`, `stop_measurement`, and `export_bdf`.
- A valid marker is returned in the control result and enqueued as one JSON event to every raw/filtered subscriber. A full subscriber queue reports dropped data/marker counts explicitly rather than failing the control request.

- [ ] **Step 1: Write the failing server tests**

```python
def test_control_marker_is_acknowledged_and_broadcast(self):
    server = LocalStreamServer(port=0, session_id="api-1")
    server.start()
    server.begin_recording("rec-1", started_at=100.0)
    try:
        with connect(f"ws://127.0.0.1:{server.port}/v1/stream") as stream_ws, \
             connect(f"ws://127.0.0.1:{server.port}/v1/control") as control_ws:
            self.subscribe(stream_ws, "raw")
            control_ws.send(json.dumps({
                "type": "marker", "code": "stimulus_on", "value": 1,
                "sequence": 12, "description": "left",
            }))
            response = json.loads(control_ws.recv())
            self.assertTrue(response["ok"])
            marker = json.loads(stream_ws.recv())
            self.assertEqual(marker["type"], "marker")
            self.assertEqual(marker["code"], "stimulus_on")
    finally:
        server.stop()

def test_marker_is_rejected_outside_a_recording(self):
    server = LocalStreamServer(port=0)
    server.start()
    try:
        with connect(f"ws://127.0.0.1:{server.port}/v1/control") as ws:
            ws.send(json.dumps({"type": "marker", "code": "x", "value": 1}))
            response = json.loads(ws.recv())
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "not_recording")
    finally:
        server.stop()
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_server -v`

Expected: FAIL because `/v1/control`, recording state, and marker fan-out do not yet exist.

- [ ] **Step 3: Implement the minimal control path**

Allow only `STREAM_PATH` and `CONTROL_PATH` in the request validator. Generalize the subscriber packet to support JSON-only marker packets, keep data packets as JSON-plus-binary pairs, and preserve bounded non-blocking enqueue behavior. Protect recording state and marker lists with a lock. Run stop/export callbacks only for their matching command and return callback errors as structured control errors.

- [ ] **Step 4: Run focused server tests and verify green**

Run: `uv run python -m unittest tests.test_onmibci_server -v`

Expected: all server tests, including existing backpressure/path tests, pass.

### Task 3: Extend the synchronous SDK

**Files:**
- Modify: `onmibci_sdk.py`
- Modify: `tests/test_onmibci_sdk.py`

**Interfaces:**
- `MarkerEvent` is yielded by stream iterators alongside `StreamBatch` and `GapEvent`.
- `LocalClient.send_marker(code, value=None, *, timestamp=None, sequence=None, duration=0.0, description="") -> MarkerEvent` sends and acknowledges a marker.
- `LocalClient.stop_measurement() -> dict` sends the stop command.
- `ExportResult` contains `path`, `recording_id`, `event_count`, and `sample_count`.
- `LocalClient.export_bdf(path, *, overwrite=False) -> ExportResult` sends the export command and validates its result.
- `connect_local(...)` continues to return a `LocalClient` with the derived `/v1/control` endpoint.

- [ ] **Step 1: Write the failing SDK tests**

```python
def test_send_marker_returns_acknowledged_event_and_streams_it(self):
    server = LocalStreamServer(port=0, session_id="api-1")
    server.start(); server.begin_recording("rec-1", started_at=100.0)
    iterator = None
    try:
        client = connect_local(port=server.port)
        iterator = client.stream_raw()
        marker = client.send_marker("stimulus_on", 1, sequence=10)
        self.assertEqual(marker.recording_id, "rec-1")
        self.assertEqual(next(iterator).code, "stimulus_on")
    finally:
        if iterator is not None: iterator.close()
        server.stop()

def test_export_bdf_validates_control_result(self):
    server = LocalStreamServer(
        port=0,
        export_handler=lambda request, markers: {
            "path": request["path"], "recording_id": "rec-1",
            "event_count": len(markers), "sample_count": 250,
        },
    )
    server.start(); server.begin_recording("rec-1")
    server.end_recording()
    try:
        result = connect_local(port=server.port).export_bdf("out.bdf")
        self.assertEqual(result.path, "out.bdf")
        self.assertEqual(result.event_count, 0)
    finally:
        server.stop()
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_sdk -v`

Expected: FAIL because the control client methods and `ExportResult` do not yet exist.

- [ ] **Step 3: Implement the synchronous control client**

Open `/v1/control` with the same websockets compatibility options as stream connections, add a request ID, require a matching successful response, and translate structured failures into `ProtocolError`. Parse marker responses with `MarkerEvent.from_dict`; reject invalid export result fields instead of returning arbitrary JSON.

- [ ] **Step 4: Run focused SDK tests and verify green**

Run: `uv run python -m unittest tests.test_onmibci_sdk -v`

Expected: all SDK and existing stream iterator tests pass.

### Task 4: Persist the recording marker session in the GUI

**Files:**
- Modify: `ads1299_eeg_gui_native.py`
- Modify: `tests/test_onmibci_stream.py`

**Interfaces:**
- `MainWindow` starts the server recording session immediately after `AsyncRawWriter.start_session()` returns its recording ID.
- `process_frames(..., live=True)` sets the first acquisition sequence once at the shared raw publish boundary; both USB and BLE therefore share the same marker alignment.
- `stop_stream()` ends the API recording session after the BIN writer is closed, leaving its marker snapshot available for export.
- `closeEvent()` stops acquisition before stopping the API server.
- `MainWindow._api_control_handler(request)` dispatches stop/export work safely and delegates marker validation/storage to `LocalStreamServer`.

- [ ] **Step 1: Write the failing wiring tests**

Add a source-level test that extracts the USB and BLE `process_frames(..., live=True)` calls and asserts both are still present, plus a small fake-server test for the start/stop calls if the existing GUI test helpers permit it. The test must fail if a transport branch gets its own divergent marker path.

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_stream -v`

Expected: the new GUI wiring assertion fails before the new recording lifecycle calls are present.

- [ ] **Step 3: Implement recording lifecycle wiring**

Initialize the server with the control callback. On start, call `begin_recording(recording_session_id, time.time())`, clear any previous markers, and keep the server session ID used by data batches unchanged. At the first live timeline, call `set_first_sequence(int(timeline_sequence[0]))`. On stop, close the raw writer before `end_recording()`. The control handler must return `not_recording`, `not_stopped`, or export errors without touching Qt widgets from the server thread; use a queued Qt signal plus a `threading.Event` for stop/export callbacks.

- [ ] **Step 4: Run syntax and GUI-independent integration checks**

Run: `uv run python -m py_compile ads1299_eeg_gui_native.py onmibci_stream.py onmibci_sdk.py`

Run: `uv run python -m unittest tests.test_onmibci_stream tests.test_onmibci_server tests.test_onmibci_sdk -v`

Expected: syntax compilation succeeds and the full API/SDK suite passes.

### Task 5: Export the complete segmented recording as BDF+

**Files:**
- Modify: `ads1299_eeg_gui_native.py`
- Modify: `tests/test_onmibci_stream.py`
- Modify: `tests/test_onmibci_sdk.py`

**Interfaces:**
- `AsyncRawWriter.snapshot()` includes an ordered `segments` list so an export never silently stops at minute 01.
- `MainWindow.export_recording_bdf(path, markers, recording_id, recording_started_at, first_sequence, overwrite=False) -> dict` reads all completed BIN segments, reconstructs the USB/BLE timeline, writes the existing eight raw channels as BDF+, and writes every marker as a standard annotation.
- Existing manual `save_bdf()` continues to export `offline_uv` and its existing padding/BAD-frame annotations; it may call a shared writer helper but must not lose current behavior.

- [ ] **Step 1: Write the failing export tests**

Test the export data path without requiring a real GUI window: create a short valid ADS BIN fixture from existing frame helpers, create two segment paths, pass two markers, and assert the export result reports the complete sample count and marker count. When pyEDFlib is installed, read the resulting BDF+ annotations and assert both marker descriptions are present; otherwise run the pure annotation/unit assertion and mark the file-read test skipped.

- [ ] **Step 2: Run export tests and verify the expected failure**

Run: `uv run python -m unittest tests.test_bdf_export -v`

Expected: FAIL because `segments` is absent from the writer snapshot and `export_recording_bdf` is not implemented.

- [ ] **Step 3: Implement complete-recording export**

Add ordered segment metadata to `AsyncRawWriter.snapshot()`. Parse all segment bytes with one `AdsFrameParser`, call `expand_frames_to_timeline` once with the full frame list, and pass the reconstructed arrays to a shared BDF writer. Write event annotations after `BAD_frame` annotations using `bdf_annotation_for_marker`; reject missing recordings, empty data, missing optional pyEDFlib, existing paths without overwrite, and events that cannot be placed non-negatively. Return absolute output path, recording ID, sample count, and event count.

- [ ] **Step 4: Run focused export tests and verify green**

Run: `uv run python -m unittest tests.test_bdf_export tests.test_onmibci_sdk -v`

Expected: export logic and SDK result parsing pass; optional BDF-reader checks pass when the export extra is installed.

### Task 6: Document and perform final verification

**Files:**
- Modify: `docs/SDK_USAGE.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-10-local-stream-api-sdk-design.md`

- [ ] **Step 1: Document marker, stop, and BDF export usage**

Add examples for `send_marker`, handling `MarkerEvent` in both streams, `stop_measurement`, and `export_bdf`. State that markers are accepted only during active acquisition, `sequence` is preferred for alignment, and export requires the optional `pyedflib` dependency (`uv sync --extra export`).

- [ ] **Step 2: Run the complete verification set**

Run: `uv lock --check`

Run: `uv run python -m unittest discover -s tests -v`

Run: `uv run python -m py_compile ads1299_eeg_gui_native.py onmibci_stream.py onmibci_sdk.py`

Run: `git diff --check`

Expected: all commands exit 0; no test failures, syntax errors, or whitespace errors.

- [ ] **Step 3: Review the final diff and report remaining physical checks**

Inspect `git status --short`, `git diff --stat`, and the USB/BLE branches around `poll_transport()`. Report that hardware validation still requires one USB and one BLE recording with an external marker and a BDF reader, even after automated tests pass.
