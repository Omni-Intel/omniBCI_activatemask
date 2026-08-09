# Local Raw and Filtered Stream API + Python SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a localhost WebSocket API and synchronous Python SDK that expose the GUI's decoded unfiltered EEG and existing live-filtered EEG as independently subscribable streams.

**Architecture:** Add a focused `onmibci_stream.py` module containing the validated batch contract, JSON-header/binary-float32 wire codec, and a thread-owned localhost WebSocket fan-out server. Add a small `onmibci_sdk.py` synchronous client that uses the same protocol. The GUI publishes raw timeline batches after the raw ring append and filtered batches after `LiveFilterWorker` results are accepted; the existing filter and recording paths stay unchanged.

**Tech Stack:** Python 3.12, NumPy, `websockets` synchronous client and asyncio server, `unittest`, PySide6 integration through the existing GUI thread.

## Global Constraints

- Bind the API to `127.0.0.1` only; no cloud, TLS, public/LAN binding, or replay in this version.
- Keep `raw` as decoded input-referred microvolts before filter-only saturation masking; do not expose the original 48-byte serial frame protocol.
- Keep `filtered` equal to the existing `LiveFilterWorker` result; do not re-run or approximate the filter in the API or SDK.
- Send JSON for hello/data metadata/gap events and a following binary C-order `float32` payload shaped `(samples, channels)`.
- Preserve `sequence`, `valid`, `modes`, `session_id`, and filtered `generation`; reject malformed shape or dtype instead of guessing.
- Publishing must not block Qt, acquisition, or filter threads; each subscriber has an independent bounded queue and explicit gap reporting.
- Use standard-library `unittest` for tests; do not add a test framework solely for this feature.

---

### Task 1: Define and test the stream batch contract and wire codec

**Files:**
- Create: `onmibci_stream.py`
- Create: `tests/__init__.py`
- Create: `tests/test_onmibci_stream.py`

**Interfaces:**
- `StreamBatch(stream, values, sequence, valid, modes, generation, session_id, sample_rate=250, channels=DEFAULT_CHANNELS, unit="uV")` stores one normalized batch with `values.shape == (N, 8)` and matching one-dimensional metadata arrays.
- `StreamBatch.from_gui_matrix(*, stream: str, values: np.ndarray, sequence: np.ndarray, valid: np.ndarray, modes: np.ndarray, generation: int | None, session_id: str, sample_rate: int = SAMPLE_RATE, channels: tuple[str, ...] = DEFAULT_CHANNELS, unit: str = "uV") -> StreamBatch` converts the GUI's `(channels, samples)` matrix to the wire `(samples, channels)` layout and copies arrays before cross-thread use.
- `StreamBatch.to_messages()` returns `(json_header: str, values_payload: bytes)`.
- `StreamBatch.from_messages(header: str, payload: bytes)` returns a validated `StreamBatch`.
- `publish_gui_matrix(server, *, stream: str, values: np.ndarray, sequence: np.ndarray, valid: np.ndarray, modes: np.ndarray, generation: int | None, session_id: str) -> StreamBatch` creates one copied batch, publishes it, and returns it for boundary tests.
- `GapEvent` stores `stream`, `dropped_batches`, and `dropped_samples`.
- Constants: `SCHEMA_VERSION = 1`, `SAMPLE_RATE = 250`, `CHANNELS = 8`, `STREAM_RAW = "raw"`, `STREAM_FILTERED = "filtered"`.

- [ ] **Step 1: Write the failing tests**

```python
class StreamBatchTests(unittest.TestCase):
    def test_raw_boundary_preserves_rail_values(self):
        class CaptureServer:
            def __init__(self):
                self.batch = None

            def publish(self, batch):
                self.batch = batch

        raw_values = np.zeros((8, 2), dtype=np.float32)
        raw_values[0, 0] = 8388607.0
        capture = CaptureServer()
        batch = publish_gui_matrix(
            capture,
            stream="raw",
            values=raw_values,
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, True]),
            modes=np.array([0, 0], dtype=np.uint8),
            generation=None,
            session_id="s1",
        )
        self.assertEqual(float(batch.values[0, 0]), 8388607.0)
        self.assertIs(capture.batch, batch)

    def test_gui_matrix_round_trip_preserves_raw_values_and_metadata(self):
        gui_values = np.arange(16, dtype=np.float32).reshape(8, 2)
        batch = StreamBatch.from_gui_matrix(
            stream="raw",
            values=gui_values,
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, False]),
            modes=np.array([0, 1], dtype=np.uint8),
            generation=None,
            session_id="s1",
        )
        header, payload = batch.to_messages()
        decoded = StreamBatch.from_messages(header, payload)
        np.testing.assert_array_equal(decoded.values, gui_values.T)
        np.testing.assert_array_equal(decoded.sequence, [10, 11])
        np.testing.assert_array_equal(decoded.valid, [True, False])
        np.testing.assert_array_equal(decoded.modes, [0, 1])

    def test_mismatched_metadata_length_is_rejected(self):
        with self.assertRaises(ValueError):
            StreamBatch(
                stream="raw",
                values=np.zeros((2, 8), dtype=np.float32),
                sequence=np.array([1], dtype=np.uint32),
                valid=np.array([True, True]),
                modes=np.array([0, 0]),
                generation=None,
                session_id="s1",
            )
```

- [ ] **Step 2: Run the tests to verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_stream -v`

Expected: FAIL because `onmibci_stream` and `StreamBatch` do not exist yet.

- [ ] **Step 3: Implement the minimal contract and codec**

Implement normalization and validation in `StreamBatch.__post_init__`, convert
GUI matrices with `np.asarray(values, dtype=np.float32).T.copy()`, serialize
metadata with `json.dumps`, and serialize only `values` with
`np.ascontiguousarray(values, dtype=np.float32).tobytes()`.

- [ ] **Step 4: Run the focused tests to verify green**

Run: `uv run python -m unittest tests.test_onmibci_stream -v`

Expected: all focused codec tests pass with zero failures.

- [ ] **Step 5: Commit the protocol unit**

```powershell
git add onmibci_stream.py tests
git commit -m "feat: add EEG stream batch protocol"
```

### Task 2: Add the localhost WebSocket fan-out server

**Files:**
- Modify: `onmibci_stream.py`
- Create: `tests/test_onmibci_server.py`

**Interfaces:**
- `LocalStreamServer(host="127.0.0.1", port=8765, queue_size=32)` owns its asyncio loop in a daemon thread.
- `start() -> None` starts the server and returns only after the bound port is ready.
- `stop(timeout=3.0) -> None` closes clients, stops the loop, and joins the thread.
- `publish(batch: StreamBatch) -> None` schedules a non-blocking broadcast to subscribers of `batch.stream`.
- Client protocol: connect to `/v1/stream`, send `{"type":"subscribe","stream":"raw"}` or `{"type":"subscribe","stream":"filtered"}`, receive `hello`, then repeated JSON-header/binary-payload pairs.
- A full subscriber queue drops the oldest complete batch and emits one `gap` JSON event before the next data packet.

- [ ] **Step 1: Write the failing server tests**

```python
class LocalStreamServerTests(unittest.TestCase):
    def make_batch(self, stream, generation=None):
        return StreamBatch.from_gui_matrix(
            stream=stream,
            values=np.arange(16, dtype=np.float32).reshape(8, 2),
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, False]),
            modes=np.array([0, 1], dtype=np.uint8),
            generation=generation,
            session_id="s1",
        )

    def test_subscriber_receives_hello_and_published_batch(self):
        server = LocalStreamServer(port=0)
        server.start()
        try:
            with connect(f"ws://127.0.0.1:{server.port}/v1/stream") as ws:
                ws.send(json.dumps({"type": "subscribe", "stream": "raw"}))
                hello = json.loads(ws.recv())
                self.assertEqual(hello["type"], "hello")
                server.publish(self.make_batch("raw"))
                header = ws.recv()
                payload = ws.recv()
                decoded = StreamBatch.from_messages(header, payload)
                np.testing.assert_array_equal(decoded.values, self.make_batch("raw").values)
        finally:
            server.stop()

    def test_two_subscribers_each_receive_the_same_stream_batch(self):
        server = LocalStreamServer(port=0)
        server.start()
        try:
            with ExitStack() as stack:
                clients = [
                    stack.enter_context(connect(f"ws://127.0.0.1:{server.port}/v1/stream")),
                    stack.enter_context(connect(f"ws://127.0.0.1:{server.port}/v1/stream")),
                ]
                for ws in clients:
                    ws.send(json.dumps({"type": "subscribe", "stream": "raw"}))
                    self.assertEqual(json.loads(ws.recv())["type"], "hello")
                server.publish(self.make_batch("raw"))
                decoded_batches = []
                for ws in clients:
                    decoded_batches.append(StreamBatch.from_messages(ws.recv(), ws.recv()))
                np.testing.assert_array_equal(decoded_batches[0].values, decoded_batches[1].values)
                np.testing.assert_array_equal(decoded_batches[0].sequence, decoded_batches[1].sequence)
        finally:
            server.stop()
```

- [ ] **Step 2: Run the tests to verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_server -v`

Expected: FAIL because `LocalStreamServer` is not defined.

- [ ] **Step 3: Implement the minimal server**

Use `websockets.asyncio.server.serve` in the server thread, an asyncio queue
per subscription, and a complete packet object containing the JSON header plus
binary payload. `publish()` must copy the `StreamBatch` reference safely and
must not wait for a send future. The handler validates the first subscription,
sends `hello`, and runs a sender task until the connection closes.

- [ ] **Step 4: Run the focused server tests to verify green**

Run: `uv run python -m unittest tests.test_onmibci_server -v`

Expected: all server tests pass with zero failures.

- [ ] **Step 5: Commit the server unit**

```powershell
git add onmibci_stream.py tests/test_onmibci_server.py
git commit -m "feat: add localhost EEG stream server"
```

### Task 3: Add the synchronous Python SDK

**Files:**
- Create: `onmibci_sdk.py`
- Create: `tests/test_onmibci_sdk.py`

**Interfaces:**
- `LocalClient(url="ws://127.0.0.1:8765/v1/stream", timeout=5.0)` connects to the local API.
- `connect_local(host="127.0.0.1", port=8765) -> LocalClient` returns the convenience client.
- `LocalClient.stream_raw() -> Iterator[StreamBatch | GapEvent]` subscribes to raw data.
- `LocalClient.stream_filtered() -> Iterator[StreamBatch | GapEvent]` subscribes to filtered data.
- `LocalClient.hello` exposes the validated hello metadata after a stream begins.
- The SDK uses `websockets.sync.client.connect`; it validates the header/payload pair through `StreamBatch.from_messages` and never silently converts malformed data.

- [ ] **Step 1: Write the failing SDK tests**

```python
class LocalClientTests(unittest.TestCase):
    def make_batch(self, stream, generation=None):
        return StreamBatch.from_gui_matrix(
            stream=stream,
            values=np.arange(16, dtype=np.float32).reshape(8, 2),
            sequence=np.array([10, 11], dtype=np.uint32),
            valid=np.array([True, False]),
            modes=np.array([0, 1], dtype=np.uint8),
            generation=generation,
            session_id="s1",
        )

    def test_stream_raw_decodes_a_server_batch(self):
        server = LocalStreamServer(port=0)
        server.start()
        try:
            client = connect_local(port=server.port)
            iterator = client.stream_raw()
            server.publish(self.make_batch("raw"))
            item = next(iterator)
            self.assertIsInstance(item, StreamBatch)
            np.testing.assert_array_equal(item.values, self.make_batch("raw").values)
        finally:
            server.stop()

    def test_stream_filtered_uses_the_filtered_subscription(self):
        server = LocalStreamServer(port=0)
        server.start()
        try:
            client = connect_local(port=server.port)
            iterator = client.stream_filtered()
            server.publish(self.make_batch("raw"))
            server.publish(self.make_batch("filtered", generation=7))
            item = next(iterator)
            self.assertIsInstance(item, StreamBatch)
            self.assertEqual(item.stream, "filtered")
            self.assertEqual(item.generation, 7)
        finally:
            server.stop()
```

- [ ] **Step 2: Run the tests to verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_sdk -v`

Expected: FAIL because `onmibci_sdk` and `connect_local` do not exist yet.

- [ ] **Step 3: Implement the minimal synchronous client**

Open one WebSocket per iterator, send the JSON subscription, validate `hello`,
then consume text/binary pairs. Yield `GapEvent` for explicit gap messages and
`StreamBatch` for data messages. Close the connection when the iterator exits.

- [ ] **Step 4: Run the focused SDK tests to verify green**

Run: `uv run python -m unittest tests.test_onmibci_sdk -v`

Expected: all SDK tests pass with zero failures.

- [ ] **Step 5: Commit the SDK unit**

```powershell
git add onmibci_sdk.py tests/test_onmibci_sdk.py
git commit -m "feat: add Python EEG stream SDK"
```

### Task 4: Connect raw and filtered GUI publish points

**Files:**
- Modify: `ads1299_eeg_gui_native.py:2537-2545`
- Modify: `ads1299_eeg_gui_native.py:6307-6335`
- Modify: `ads1299_eeg_gui_native.py:6352-6363`
- Modify: `ads1299_eeg_gui_native.py:7653-7668`
- Modify: `tests/test_onmibci_stream.py`

**Interfaces:**
- `MainWindow.stream_server` is a `LocalStreamServer` started with the GUI and stopped by `closeEvent`.
- Raw publish uses `timeline_values`, `timeline_sequence`, `timeline_valid`, and `timeline_modes` immediately after `self.ring.append_batch(...)`, before `filter_values` saturation masking.
- Filtered publish uses `FilteredBatch.filtered`, `.sequence`, `.valid`, `.modes`, and `.generation` immediately before/with `self.filtered_ring.append_batch(...)`.

- [ ] **Step 1: Re-run the raw-boundary test before changing the GUI**

Use the `test_raw_boundary_preserves_rail_values` test created in Task 1. It
must be green before wiring the GUI, proving that the publisher helper keeps a
raw rail value and does not apply the filter-only masking copy.

- [ ] **Step 2: Run the boundary test to verify the expected failure**

Run: `uv run python -m unittest tests.test_onmibci_stream.StreamBatchTests.test_raw_boundary_preserves_rail_values -v`

Expected: PASS from the Task 1 implementation; if it fails, stop and fix the
stream boundary before touching the GUI.

- [ ] **Step 3: Wire the existing GUI boundaries**

Import the stream types, start one server during `MainWindow` initialization,
publish copied batches at the two existing processing points, and stop the
server before the GUI's final shutdown. Do not alter filter coefficients,
recording writes, parser behavior, or plot data.

- [ ] **Step 4: Run syntax and focused integration checks**

Run: `uv run python -m py_compile ads1299_eeg_gui_native.py onmibci_stream.py onmibci_sdk.py`

Run: `uv run python -m unittest tests.test_onmibci_stream tests.test_onmibci_server tests.test_onmibci_sdk -v`

Expected: syntax compilation and all API/SDK tests pass.

- [ ] **Step 5: Commit the GUI integration**

```powershell
git add ads1299_eeg_gui_native.py tests/test_onmibci_stream.py
git commit -m "feat: publish raw and filtered EEG from GUI"
```

### Task 5: Add dependency, usage documentation, and final verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `README.md`
- Modify: `uv.lock` via `uv lock`

- [ ] **Step 1: Add the runtime dependency and run the lock update**

Add `websockets>=14,<16` to the project dependencies and core fallback
requirements, then run `uv lock` from the repository root.

- [ ] **Step 2: Document local API and SDK usage**

Add a short section showing `uv sync`, starting the GUI, and importing
`connect_local` for `stream_raw()` and `stream_filtered()`. State that the API
binds to localhost and that the raw stream is decoded microvolts, not wire
bytes.

- [ ] **Step 3: Run the full verification set**

Run: `uv lock --check`

Run: `uv run python -m unittest discover -s tests -v`

Run: `uv run python -m py_compile ads1299_eeg_gui_native.py onmibci_stream.py onmibci_sdk.py`

Run: `git diff --check`

Expected: all commands exit 0; the test output reports zero failures and no
unexpected errors.

- [ ] **Step 4: Review the complete diff and commit documentation/dependency changes**

```powershell
git status --short
git diff --stat
git add pyproject.toml requirements.txt README.md uv.lock
git commit -m "docs: document local EEG stream integration"
```
