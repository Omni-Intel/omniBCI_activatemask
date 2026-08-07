# V18 BLE capture/TX isolation

V18 targets the long-run failure shown by the diagnostic snapshot where `Sequence gaps=771`, firmware `queue_drop=771`, `notify_error=238`, and max Notify gap was about 5.1 s while GUI/filter/raw queues remained empty.

## Firmware

- Split BLE DATA notification out of `transportTask` into a dedicated low-priority `bleTxTask`.
- `transportTask` priority is raised and now owns frameQueue -> compact block -> reliable retention only; it never performs DATA notify during continuous acquisition.
- A Windows/BLE stack pause can therefore fill the ~9.2 s reliable retention ring without starving the 250 SPS ADS frameQueue.
- Periodic STATUS notify is also handed to the BLE TX task so STATUS traffic cannot stall frameQueue draining.
- Reliable ring slots publish `valid=true` only after the complete payload has been copied, making pack/TX task overlap safe.
- Reliable reset briefly suspends the TX task to avoid resetting a block while it is being copied for transmission.
- Stopping a stream no longer calls the DATA transmitter from a second task; BLE TX has one owner only.

## GUI

- BLE gap attribution now uses the full 32-bit firmware `queue_drop` STATUS counter when available. The old 8-bit in-frame hint can wrap during a large burst and incorrectly label real MCU frameQueue loss as host loss.
- Diagnostic label changed from `FW queue/notify` to `FW frameQ/notifyErr`.
- Verdict now explicitly distinguishes MCU frameQueue loss from host reliable/decode loss.

## Compatibility

The BLE reliable wire protocol remains unchanged (compact DATA protocol V2 / STATUS V4), so the V18 GUI remains compatible with the prior V16/V17 reliable firmware. For this continuity fix, use the bundled V18 firmware.

## V18 stale-NACK / long-run diagnostic hotfix

- GUI revalidates each queued NACK immediately before the GATT write. If the missing block has already arrived or the cumulative ACK has advanced past the requested range, the stale NACK is suppressed locally.
- Duplicate/older cumulative ACK writes are coalesced before they reach Windows BLE.
- Reliable ACK/NACK controls use write-without-response; RESET still uses write-with-response. The control characteristic already advertises both WRITE and WRITE_NR.
- Firmware no longer counts a control from an old session, or a NACK overtaken by a cumulative ACK, as a protocol error/unknown NACK.
- A cumulative ACK now cancels or trims a queued firmware NACK range immediately so stale repair traffic cannot consume BLE TX slots.
- `reliable_unknown_nack` is now reserved for a genuinely unacked, already-produced block that is absent from retention.
- GUI diagnostics show `Stale ctrl suppressed` and `FW recent o/u/p` (new overflow / unknown NACK / protocol errors since the previous STATUS update).
- Historical nonzero unknown/protocol counters no longer permanently force the red "protocol mismatch" verdict for the rest of a long recording.
