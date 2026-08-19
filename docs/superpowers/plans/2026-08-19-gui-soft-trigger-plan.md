# GUI Soft Trigger Implementation Plan

**Goal:** Add a closable floating soft-trigger window that writes manual triggers through the existing SDK marker path.

**Architecture:** `LocalStreamServer` remains the single marker owner. A small public method creates and broadcasts a marker for in-process GUI callers, while the existing WebSocket command calls the same method. `SoftTriggerWindow` only validates user interaction and delegates marker creation to `MainWindow`.

**Tech Stack:** Python 3.12, PySide6, existing `MarkerEvent`/`LocalStreamServer`, unittest.

## Global Constraints

- Trigger numbers are integers from 1 through 255.
- SDK marker behavior remains unchanged and primary.
- GUI trigger uses the current recording and latest EEG sequence.
- No firmware or communication-protocol changes.
- No new dependency.

### Task 1: Shared Marker Insertion

**Files:**
- Modify: `onmibci_stream.py`
- Test: `tests/test_onmibci_server.py`

- [ ] Add a failing test for in-process marker insertion during an active recording.
- [ ] Add a failing test for rejection outside recording.
- [ ] Implement `LocalStreamServer.add_marker(...) -> MarkerEvent` and route the WebSocket handler through it.
- [ ] Run the server tests.

### Task 2: Floating Trigger Window

**Files:**
- Modify: `ads1299_eeg_gui_native.py`
- Test: `tests/test_gui_soft_trigger.py`

- [ ] Add failing tests for bounds, active-recording requirement, numeric value, and latest sequence.
- [ ] Implement a non-modal closable `SoftTriggerWindow` with a 1-255 spin box, Send button, Space shortcut, and result label.
- [ ] Add `MainWindow.send_soft_trigger(number)` using the shared marker insertion method.
- [ ] Add one toolbar/menu action that reopens and raises the window.

### Task 3: Verification

**Files:**
- Modify only if verification exposes a regression.

- [ ] Run targeted trigger tests.
- [ ] Run all unittest tests.
- [ ] Run Python syntax compilation and `git diff --check`.
