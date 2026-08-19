# GUI Soft Trigger Design

## Goal

Add an optional manual soft-trigger window while keeping the SDK as the primary trigger interface.

## User Interface

- Add a non-modal floating window titled `软 Trigger`.
- The window can be opened from the main GUI and closed independently.
- It contains one integer input constrained to `1-255`, a send button, and a latest-result label.
- Clicking Send or pressing Space while this window has focus sends the current trigger number.
- Space outside this window does not send a trigger.
- Sending is disabled unless an acquisition recording is active.

## Data Flow

- GUI triggers reuse the existing `MarkerEvent` and `LocalStreamServer` marker store.
- GUI and SDK markers share the same recording ID, ordering, subscriber broadcast, and BDF annotation export.
- A GUI trigger uses code `soft_trigger`, the numeric trigger as `value`, the current wall-clock timestamp, and the latest acquired EEG sequence when available.
- No firmware or BLE protocol changes are required.

## Error Handling

- Outside acquisition, the window shows that recording must be started first.
- Invalid values cannot leave the `1-255` range.
- Marker failures are shown in the window and written to the existing application log.

## Verification

- Test direct GUI marker insertion through the same server store used by SDK markers.
- Test rejection outside active recording.
- Test trigger number bounds and latest sequence propagation.
- Run the existing full unittest suite.
