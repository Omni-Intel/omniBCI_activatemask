# Local Raw and Filtered Stream API + Python SDK

## Goal

Expose the GUI's live EEG processing results to local Python programs without
making those programs import the GUI implementation or read a recording file.
The first version provides two real-time streams: decoded unfiltered EEG
(`raw`) and the existing causal live-filter output (`filtered`). Cloud
transport, public network binding, and durable replay are out of scope.

## Existing boundary

The GUI receives ADS1299 frames, converts them to input-referred microvolts,
and appends the unfiltered timeline to `MainWindow.ring`. It then creates a
separate filter input copy and sends that copy through `LiveFilterWorker`.
`FilteredBatch` already carries the filtered matrix, validity flags, sample
sequence, and acquisition modes. The new API must publish at these existing
boundaries so the colleague's program receives the same signal semantics as
the GUI without re-running or approximating the filter.

## USB and BLE coverage

The API is deliberately above the physical transport. `poll_transport()` has
separate USB-serial and BLE receive branches, but both branches call the same
`process_frames(..., live=True)` path. The raw publisher runs after the common
timeline is appended to `MainWindow.ring`, and the filtered publisher runs when
the common `LiveFilterWorker` result is accepted. Therefore the colleague uses
the same `connect_local()`, `stream_raw()`, and `stream_filtered()` code for
both GUI modes.

The two input paths retain their existing timeline semantics. USB publishes
the decoded frames received from the serial worker. BLE publishes its expanded
timeline, including explicit invalid samples for recoverable sequence gaps.
`sequence` and `valid` must be used together: an API queue `GapEvent` means the
subscriber fell behind, while `valid=false` describes an acquisition-timeline
sample that was not available. Neither path turns a missing sample into a
fabricated EEG value.

## Architecture

```text
ADS frame parser
    ├── decoded timeline (uV) ───────> raw publisher ───────┐
    └── LiveFilterWorker ────────────> filtered publisher ──┤
                                                           │
                                      localhost WebSocket API
                                                           │
                                      Python SDK subscriber
```

The GUI owns a WebSocket server bound to `127.0.0.1` on port `8765`. Each
client subscribes to exactly one logical stream per WebSocket connection using
`/v1/stream` and a JSON subscribe message. The server keeps a bounded queue per
client. Publishing never waits for a client; if a client falls behind, the
server drops the oldest queued batch for that client and sends an explicit gap
event before the next data batch.

The transport uses JSON text messages for handshake, metadata, and events. EEG
values are sent as a following binary WebSocket message containing C-order
`float32` values, because serializing every numeric sample as JSON would add
work to the GUI and the model client. The SDK hides this two-message framing.

## Stream semantics

Both streams use the same batch metadata:

```json
{
  "type": "data",
  "schema_version": 1,
  "stream": "raw",
  "session_id": "...",
  "generation": null,
  "sample_rate": 250,
  "channels": ["CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7", "CH8"],
  "unit": "uV",
  "dtype": "float32",
  "shape": [16, 8],
  "sequence": [1000, 1001, 1002],
  "valid": [true, true, true],
  "modes": [0, 0, 0]
}
```

The actual `values` binary message has the declared `shape` and is interpreted
as `float32` with shape `(samples, channels)`.

`raw` means values already decoded to microvolts but not passed through the
GUI's live band/notch filter. It preserves the input-referred values, including
rail values; `valid` and `sequence` make missing or invalid timeline samples
explicit. It is not the original 48-byte wire frame stream.

`filtered` means the output of `LiveFilterWorker`. It may contain channel-level
`NaN` values where the GUI intentionally masks invalid/saturated filter input.
`generation` identifies the filter configuration generation; it changes when
the GUI resets or reconfigures the live filter. The SDK exposes this value so a
model can reject or segment data across a filter change.

The server sends a hello message after a valid subscription:

```json
{
  "type": "hello",
  "schema_version": 1,
  "stream": "filtered",
  "session_id": "...",
  "sample_rate": 250,
  "channels": ["CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7", "CH8"],
  "unit": "uV"
}
```

When a per-client queue drops batches, the server sends:

```json
{
  "type": "gap",
  "stream": "raw",
  "dropped_batches": 1,
  "dropped_samples": 16
}
```

## Python SDK

The SDK is synchronous at the public boundary so a normal model loop does not
need to manage an asyncio event loop:

```python
from onmibci_sdk import connect_local

client = connect_local()
for batch in client.stream_raw():
    prediction = model.predict(batch.values)
```

The same client exposes `stream_filtered()`. A returned batch has NumPy arrays
for `values`, `sequence`, `valid`, and `modes`, plus `stream`, `sample_rate`,
`channels`, `unit`, `session_id`, and `generation`. The SDK raises a protocol
error for malformed frames and surfaces gap events instead of silently
inventing samples.

## GUI integration

1. Start the local stream server with the GUI and stop it during GUI shutdown.
2. Keep USB serial and BLE receive code above the shared `process_frames`
   boundary; do not duplicate API publishing in either transport branch.
3. Publish the decoded timeline immediately after the raw ring append and
   before filter-only saturation masking.
4. Publish each accepted `FilteredBatch` at the existing filter-result drain
   point.
5. Copy arrays before crossing from the Qt/filter threads into the API server
   thread.
6. Keep raw and filtered subscribers independent so one slow model cannot
   starve the other stream or the GUI.

## Testing and acceptance

- Protocol tests cover raw and filtered header construction, binary float32
  round-trips, shape validation, sequence/valid/mode preservation, and gap
  events.
- SDK tests connect to a local in-process server, subscribe to both streams,
  and assert that decoded NumPy arrays equal the published batches.
- Fan-out tests prove two subscribers each receive a full copy of a batch.
- Backpressure tests prove a slow subscriber does not block publishing and
  receives an explicit gap event.
- Transport-wiring review proves both the USB and BLE receive branches enter
  the shared publish path; no hardware is required for the API/SDK tests.
- A GUI-independent integration test verifies the server and SDK without
  hardware or a visible Qt window.
- Existing Python syntax checks and project tests remain green.

## Non-goals

- No cloud endpoint, TLS, public/LAN binding, or cloud authentication.
- No JSON file polling.
- No raw serial-frame API.
- No model execution inside the GUI.
- No change to the existing filter algorithm or recording format.

## Marker and BDF export extension

The API keeps `/v1/stream` for subscriptions and adds a localhost-only
`/v1/control` WebSocket for one-request/one-response commands. The SDK exposes
`send_marker()`, `stop_measurement()`, and `export_bdf()`. A marker contains a
code, JSON scalar value, UNIX timestamp, optional acquisition sequence,
duration, description, event ID, API session ID, and recording ID.

The GUI opens a marker session with the same recording ID as the segmented BIN
writer. Markers are broadcast to raw and filtered subscribers and retained
until the next recording starts. After measurement stops, the export command
parses every completed BIN segment, reconstructs the USB/BLE timeline, and
writes BDF+ Annotation entries. Sequence alignment is preferred for onset;
timestamp-relative alignment is the fallback. BDF padding annotations are not
added to an event-bearing export because some pyEDFlib readers treat the
padding interval as covering later user events.
