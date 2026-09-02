# STM32 SD, Status LED, and External Trigger Design

## Scope

Add three board-level functions to the STM32H563 Zephyr/NCS firmware:

- automatic offline recording to a removable FAT32 SD card;
- explicit PA1 status LED states;
- first-stage PB7 external-trigger hardware validation.

The existing ADS1299 to E73 to Dongle acquisition and control path remains the
primary real-time path. STM32 USB remains limited to update and debugging. This
change does not modify the E73 firmware, Dongle firmware, GUI, frozen ESP32 EEG
firmware, or EMG product.

## Confirmed Hardware Mapping

| Function | STM32 pin | Package pad | Net behavior |
| --- | --- | ---: | --- |
| SDMMC1 D0 | PC8 | 65 | 10 kOhm external pull-up |
| SDMMC1 D1 | PC9 | 66 | 10 kOhm external pull-up |
| SDMMC1 D2 | PC10 | 78 | 10 kOhm external pull-up |
| SDMMC1 D3 | PC11 | 79 | 10 kOhm external pull-up |
| SDMMC1 CK | PC12 | 80 | direct clock net |
| SDMMC1 CMD | PD2 | 83 | 10 kOhm external pull-up |
| Work LED | PA1 | 24 | active high |
| External trigger | PB7 | 93 | optocoupler collector, 10 kOhm pull-up, active low |

The installed TF socket has no independent card-detect contact. Card presence
is therefore inferred only by successful SD initialization and FAT mount.

## SD Recording Architecture

### Data Flow

Each successfully built 48-byte V19 stream frame is offered to a fixed-capacity
SD queue without waiting. The existing ADS and E73 path never performs a file
operation. A dedicated Zephyr SD writer thread consumes queued frames, combines
128 frames into 6144-byte writes, and writes them through FatFs over four-bit
SDMMC1. A 6144-byte batch is both a whole number of 48-byte frames and 512-byte
SD sectors, so the file needs no padding.

The queue stores 64 KiB, which buffers about 1.3 seconds at 1000 samples per
second. Queue insertion uses a no-wait operation. If the queue is full, the
real-time frame still goes to E73 and the firmware increments
`sd_drop_frames`.

### Mount and File Lifecycle

- Firmware attempts to initialize and mount a FAT32 card at startup.
- It never formats a card automatically.
- Missing media or mount failure is non-fatal and does not prevent acquisition.
- Every acquisition start creates the next available file named
  `/SD:/BCI00001.BIN` through `/SD:/BCI99999.BIN`.
- A recording file contains consecutive, unchanged 48-byte V19 frames and no
  additional header. Existing BIN readers can consume it directly.
- The writer uses 6144-byte writes and calls `fs_sync()` about once per second.
- Acquisition stop drains already queued frames, synchronizes, and closes the
  file.
- Write failure, full media, or I/O error closes and disables only the current
  recording. Acquisition and radio transmission continue.
- Hot removal during recording is outside the first implementation scope.
- Sudden power loss may discard approximately the final one second of buffered
  data.

The firmware reports mount state, open/write/sync errors, bytes written,
`sd_drop_frames`, and the current filename through SEGGER RTT logs. SD faults
do not alter the 48-byte radio protocol in this stage.

## External Trigger

PB7 is configured as an active-low input with a falling-edge interrupt. The
external 10 kOhm pull-up is authoritative; an internal pull-up may also be
enabled as a benign fallback.

The GPIO interrupt performs only minimal work: it rejects edges occurring less
than 5 ms after the accepted edge, increments a monotonic event counter, and
signals deferred processing. First-stage deferred handling prints the event as
`EXT_TRIG event=N level=0` through RTT.

This stage proves the PCB input path and interrupt behavior only. It does not
start or stop acquisition, modify the 48-byte frame, assign an acquisition
timestamp, write trigger records to the SD file, or update the GUI. Timestamp
and end-to-end marker transport will be designed after hardware validation.

## Status LED

PA1 is controlled by a small state machine:

| State | LED behavior |
| --- | --- |
| Boot and initialization | slow blink |
| Acquisition active and ADS-to-E73 frames succeeding | solid on |
| Acquisition stopped | off |
| Fatal initialization failure | fast blink |

A healthy acquisition indication expires when the existing successful-frame
timeout is exceeded. Missing SD media and SD write errors do not change the LED
because offline storage is optional and the radio acquisition remains usable.

## Concurrency and Failure Isolation

- ADS acquisition and E73 exchange retain their current execution priority and
  behavior.
- The SD writer runs in a separate lower-priority thread.
- The trigger ISR never logs, writes a file, or waits on a kernel object.
- All buffers are statically allocated; the streaming path does not allocate
  heap memory.
- SD queue overflow and SD I/O failure are observable counters, not fatal
  acquisition errors.
- A fatal core peripheral initialization failure enters the fast-blink state
  instead of returning silently from `main()`.

## Verification

### Host Checks

- Build the MCUboot sysbuild with NCS v3.4.0 using the existing `fw.ps1` flow.
- Add small host-runnable checks for file naming, frame batching, queue overflow
  accounting, LED state transitions, and trigger debounce logic.
- Confirm the application and signed update fit the existing flash partitions.

### Hardware Checks

1. With no card installed, verify Dongle acquisition remains continuous and RTT
   reports a non-fatal SD mount failure.
2. With a FAT32 card installed, start acquisition and verify a new BIN file is
   created automatically.
3. At 1000 SPS, record for at least 30 minutes. Verify file length is divisible
   by 48 and scan CRC plus sequence continuity.
4. Fill or fault the card and verify E73/Dongle acquisition continues while RTT
   reports the SD error.
5. Apply 100 external trigger pulses spaced more than 5 ms apart. Verify RTT
   reports exactly events 1 through 100 without duplicates.
6. Apply contact bounce or repeated edges within 5 ms and verify they count as
   one event.
7. Verify slow boot blink, solid healthy-acquisition indication, stopped-off
   indication, and fast fatal-error blink.

## Deferred Work

- Trigger timestamps and association with an ADS sample sequence.
- Trigger records in the SD format.
- Trigger transport through E73 and Dongle to the STM32 GUI.
- GUI display and BDF event export for hardware triggers.
- exFAT support, SD hot-plug detection, and safe removal during recording.
