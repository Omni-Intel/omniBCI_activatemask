# STM32 recording control implementation

Approved scope: STM32 product only; preserve 48-byte stream and both ESP32 products.

1. Test SD policy: autonomous boot permits polling; GUI start locks late insertion; stop permits mount-only polling; write failure disables current recording round.
2. SD worker owns all FAT operations. Acquisition publishes desired state without waiting; generation tags prevent old frames crossing recording boundaries.
3. Pair each BIN with append-only MET JSON lines: configuration/start, progress, trigger associations, clean end. Missing end means incomplete, never an invented end time. Absolute time remains explicitly unknown without synchronization.
4. Preserve legacy s/b commands; add AB status query for stopped-and-file-closed acknowledgment. GUI close waits for that acknowledgment, warns and remains open on timeout. Radio loss has no state transition.
5. Run host policy/protocol tests and signed NCS build outside Git. Hardware validation remains separate; no automatic flashing of this new version.

Trigger association is first software-produced sample after ISR event, not calibrated ADC-edge timing. Use 64-bit monotonic milliseconds in MET; retain the legacy wrapping microsecond field in BIN.

## Implementation status

- Steps 1-4 implemented in STM32 product only; GUI55 and QEMU7 tests pass.
- Signed NCS19.5.0 candidate validates with the persistent development key. Artifacts: D:/ncs-builds/stm32h563-19.5.0-final.
- 19.5.0 was flashed and boot-time SD recording verified. BCI00007 readback: 573944 frames, no CRC/header/sequence errors; MET progress boundaries match BIN. No clean end record was present, so graceful-stop acceptance remains pending.
- Late insertion failed on 19.5.0 but card-in-place reset worked. 19.5.1 forces disk deinitialization after failed mount to clear retained HAL error state. Nine tool tests and signed build/key verification pass; 19.5.1 has NOT been flashed or hardware-validated.
- Pending: late insertion retest, GUI close/end record, SD failure, calibrated sampling-rate checks and trigger verification. UTC synchronization and PC MET import remain out of scope. No full power-loss safety guarantee.
