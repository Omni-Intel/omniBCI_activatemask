# STM32 SD, Status LED, and External Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add non-blocking FAT32 recording, explicit PA1 status indications, and debounced PB7 external-trigger event counting to the existing STM32H563 Zephyr firmware.

**Architecture:** The ADS/E73 loop remains the real-time owner of 48-byte frames. It offers copies to a fixed 64 KiB queue while a lower-priority SD thread performs all FatFs operations in 6144-byte aligned batches. Portable state helpers define trigger debounce and LED output, while thin Zephyr modules own GPIO interrupts, deferred RTT logging, and LED timing.

**Tech Stack:** nRF Connect SDK v3.4.0, Zephyr RTOS, STM32H563 SDMMC1, FatFs, Zephyr GPIO/thread/message-queue APIs, ztest on QEMU, MCUboot, PowerShell build wrapper.

## Global Constraints

- Modify only the STM32 product; do not change E73, Dongle, GUI, ESP32 EEG, or ESP32 EMG sources.
- Dongle remains the acquisition-control path; STM32 USB remains update/debug only.
- Preserve the existing 48-byte V19 frame and E73 protocol byte-for-byte.
- SD absence or failure must never stop ADS acquisition or radio transmission.
- Use FAT32 only, never auto-format media, and do not add card-hot-removal support.
- Use PC8/PC9/PC10/PC11/PC12/PD2 for four-bit SDMMC1, active-high PA1 for the LED, and active-low PB7 for external trigger.
- Use static allocation in the streaming path and preserve the existing MCUboot partition map.
- Generated factory/update binaries remain outside Git.

---

## File Map

- Create `source/stm32/src/feature_logic.h`: portable trigger and LED-state API.
- Create `source/stm32/src/feature_logic.c`: wrap-safe debounce and LED waveform logic.
- Create `source/stm32/src/status_io.h`: status LED and external-trigger Zephyr API.
- Create `source/stm32/src/status_io.c`: PA1 worker, PB7 ISR, event queue, and RTT logs.
- Create `source/stm32/src/sd_recorder.h`: asynchronous recorder API and statistics.
- Create `source/stm32/src/sd_recorder.c`: SD mount, filename selection, frame queue, batching, sync, and close.
- Create `source/stm32/tests/feature_logic/CMakeLists.txt`: QEMU ztest build.
- Create `source/stm32/tests/feature_logic/prj.conf`: ztest configuration.
- Create `source/stm32/tests/feature_logic/src/main.c`: portable logic unit tests.
- Modify `source/stm32/src/main.c`: initialize and feed the three modules.
- Modify `source/stm32/CMakeLists.txt`: compile the new production modules.
- Modify `source/stm32/prj.conf`: enable FatFs, SD disk, and required stacks.
- Modify `source/stm32/boards/bciband_h563vg/bciband_h563vg.dts`: declare SDMMC1 and board GPIOs.
- Modify `source/stm32/README.md`: document behavior and hardware checks.
- Modify `products/manifest.json`: authorize exact STM32 development-source hashes.

### Task 1: Portable Feature Logic

**Files:**
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/feature_logic.h`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/feature_logic.c`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic/CMakeLists.txt`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic/prj.conf`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic/src/main.c`

**Interfaces:**
- Produces: `enum bci_led_state`, `bool bci_led_output(enum bci_led_state, uint32_t)`, and `bool bci_trigger_accept(uint32_t, uint32_t *, bool *)`.
- Consumes: only fixed-width integer and boolean standard C types.

- [ ] **Step 1: Write failing ztests for debounce and LED timing**

```c
ZTEST(feature_logic, test_trigger_debounce_and_wrap)
{
	uint32_t last = 0U;
	bool seen = false;
	zassert_true(bci_trigger_accept(100U, &last, &seen));
	zassert_false(bci_trigger_accept(104U, &last, &seen));
	zassert_true(bci_trigger_accept(105U, &last, &seen));
	last = UINT32_MAX - 2U;
	seen = true;
	zassert_true(bci_trigger_accept(3U, &last, &seen));
}

ZTEST(feature_logic, test_led_waveforms)
{
	zassert_false(bci_led_output(BCI_LED_STOPPED, 0U));
	zassert_true(bci_led_output(BCI_LED_ACQUIRING, 999U));
	zassert_true(bci_led_output(BCI_LED_BOOT, 0U));
	zassert_false(bci_led_output(BCI_LED_BOOT, 500U));
	zassert_true(bci_led_output(BCI_LED_FATAL, 0U));
	zassert_false(bci_led_output(BCI_LED_FATAL, 100U));
}
```

- [ ] **Step 2: Run the QEMU test and verify it fails before the API exists**

Use this test project configuration:

```cmake
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bciband_feature_logic_tests)
target_sources(app PRIVATE src/main.c ../../src/feature_logic.c)
target_include_directories(app PRIVATE ../../src)
```

Set `CONFIG_ZTEST=y` in its `prj.conf`, then run through the NCS v3.4.0
toolchain launcher. Stage the tiny test under an ASCII path because the bundled
CMake crashes when a source path contains Chinese characters:

```powershell
$source = 'D:\高博_采集板优化\worktrees\stm32-dev-environment\products\stm32_bci\firmware\STM32_E73_DONGLE_V19\source\stm32'
$stage = 'D:\ncs-builds\stm32-feature-logic-source'
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$stage\tests", "$stage\src" | Out-Null
Copy-Item -LiteralPath "$source\tests\feature_logic" -Destination "$stage\tests" -Recurse
Copy-Item -LiteralPath "$source\src\feature_logic.c", "$source\src\feature_logic.h" -Destination "$stage\src"
D:\nrfutil\nrfutil.exe sdk-manager toolchain launch `
  --ncs-version v3.4.0 --install-dir D:\ncs --chdir D:\ncs\v3.4.0 `
  -- west build -p always -b qemu_cortex_m3 `
  -d D:\ncs-builds\stm32-feature-logic-tests `
  D:\ncs-builds\stm32-feature-logic-source\tests\feature_logic
```

Expected: compilation fails because `feature_logic.h` or its functions are missing.

- [ ] **Step 3: Implement the minimal portable helpers**

```c
#define BCI_TRIGGER_DEBOUNCE_MS 5U

bool bci_trigger_accept(uint32_t now_ms, uint32_t *last_ms, bool *seen)
{
	if (*seen && (uint32_t)(now_ms - *last_ms) < BCI_TRIGGER_DEBOUNCE_MS) {
		return false;
	}
	*seen = true;
	*last_ms = now_ms;
	return true;
}

bool bci_led_output(enum bci_led_state state, uint32_t elapsed_ms)
{
	switch (state) {
	case BCI_LED_ACQUIRING: return true;
	case BCI_LED_BOOT: return (elapsed_ms % 1000U) < 500U;
	case BCI_LED_FATAL: return (elapsed_ms % 200U) < 100U;
	default: return false;
	}
}
```

- [ ] **Step 4: Build and run the ztests**

Run:

```powershell
$source = 'D:\高博_采集板优化\worktrees\stm32-dev-environment\products\stm32_bci\firmware\STM32_E73_DONGLE_V19\source\stm32'
$stage = 'D:\ncs-builds\stm32-feature-logic-source'
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$stage\tests", "$stage\src" | Out-Null
Copy-Item -LiteralPath "$source\tests\feature_logic" -Destination "$stage\tests" -Recurse
Copy-Item -LiteralPath "$source\src\feature_logic.c", "$source\src\feature_logic.h" -Destination "$stage\src"
D:\nrfutil\nrfutil.exe sdk-manager toolchain launch `
  --ncs-version v3.4.0 --install-dir D:\ncs --chdir D:\ncs\v3.4.0 `
  -- west build -p always -b qemu_cortex_m3 `
  -d D:\ncs-builds\stm32-feature-logic-tests `
  D:\ncs-builds\stm32-feature-logic-source\tests\feature_logic
D:\nrfutil\nrfutil.exe sdk-manager toolchain launch `
  --ncs-version v3.4.0 --install-dir D:\ncs --chdir D:\ncs\v3.4.0 `
  -- west build -d D:\ncs-builds\stm32-feature-logic-tests -t run
```

Expected: all ztests pass.

- [ ] **Step 5: Commit the portable logic**

```powershell
git add products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/feature_logic.* `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic
git commit -m "test: define STM32 trigger and LED state logic"
```

### Task 2: Board Device Tree and Storage Configuration

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/boards/bciband_h563vg/bciband_h563vg.dts`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/prj.conf`

**Interfaces:**
- Produces: `work-led-gpios`, `ext-trigger-gpios`, and enabled `sdmmc1` disk named `SD`.
- Consumes: STM32H5 pinctrl definitions already provided by `stm32h563vgtx-pinctrl.dtsi`.

- [ ] **Step 1: Add a source assertion test for the exact PCB pin map**

Add a Python unittest in `source/stm32/tools/test_fw_tools.py` that reads the DTS and asserts these tokens:

```python
for token in (
    "work-led-gpios = <&gpioa 1 GPIO_ACTIVE_HIGH>",
    "ext-trigger-gpios = <&gpiob 7 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>",
    "&sdmmc1_d0_pc8", "&sdmmc1_d1_pc9", "&sdmmc1_d2_pc10",
    "&sdmmc1_d3_pc11", "&sdmmc1_ck_pc12", "&sdmmc1_cmd_pd2",
    'disk-name = "SD"', "bus-width = <4>",
):
    self.assertIn(token, dts)
```

- [ ] **Step 2: Run the tool tests and verify the new pin-map test fails**

Run:

```powershell
python -m unittest test_fw_tools.py
```

Expected: the new test fails because the board nodes are absent.

- [ ] **Step 3: Add the exact device-tree nodes and FatFs configuration**

Add to `bci-control`:

```dts
work-led-gpios = <&gpioa 1 GPIO_ACTIVE_HIGH>;
ext-trigger-gpios = <&gpiob 7 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>;
```

Enable the controller:

```dts
&sdmmc1 {
	pinctrl-0 = <&sdmmc1_d0_pc8 &sdmmc1_d1_pc9
		     &sdmmc1_d2_pc10 &sdmmc1_d3_pc11
		     &sdmmc1_ck_pc12 &sdmmc1_cmd_pd2>;
	pinctrl-names = "default";
	disk-name = "SD";
	bus-width = <4>;
	status = "okay";
};
```

Add the required Kconfig settings:

```conf
CONFIG_DISK_ACCESS=y
CONFIG_DISK_DRIVER_SDMMC=y
CONFIG_FILE_SYSTEM=y
CONFIG_FAT_FILESYSTEM_ELM=y
CONFIG_FS_FATFS_MOUNT_MKFS=n
CONFIG_FS_FATFS_NUM_FILES=2
CONFIG_FS_FATFS_NUM_DIRS=1
```

- [ ] **Step 4: Run source tests and a pristine signed configuration build**

Run `python -m unittest test_fw_tools.py`, then run the existing signed build wrapper with the temporary development key and a new external build directory. Expected: tests pass, DTS accepts all six SDMMC pins, and Zephyr selects the STM32 SDMMC disk driver without changing flash partitions.

- [ ] **Step 5: Commit board configuration**

```powershell
git add products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/boards/bciband_h563vg/bciband_h563vg.dts `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/prj.conf `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py
git commit -m "feat: configure STM32 SDMMC and board status IO"
```

### Task 3: Status LED and External Trigger Driver

**Files:**
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/status_io.h`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/status_io.c`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/CMakeLists.txt`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/main.c`

**Interfaces:**
- Consumes: `bci_led_output()` and `bci_trigger_accept()` from Task 1 plus DTS GPIO properties from Task 2.
- Produces: `int status_io_init(void)`, `void status_led_set_state(enum bci_led_state)`, `void status_led_note_frame_success(void)`, and `uint32_t status_trigger_count(void)`.

- [ ] **Step 1: Extend ztests for acquisition-health expiry**

Add `bool bci_acquisition_healthy(uint32_t now_ms, uint32_t last_success_ms)` and test:

```c
zassert_true(bci_acquisition_healthy(1099U, 1000U));
zassert_false(bci_acquisition_healthy(1100U, 1000U));
zassert_true(bci_acquisition_healthy(50U, UINT32_MAX - 20U));
```

- [ ] **Step 2: Run ztests and verify the missing helper fails**

Expected: compile failure for `bci_acquisition_healthy`.

- [ ] **Step 3: Implement the helper and the Zephyr adapter**

The GPIO ISR must perform only debounce, atomic event-number allocation, and a no-wait message-queue put:

```c
static void trigger_isr(const struct device *port,
			struct gpio_callback *cb, gpio_port_pins_t pins)
{
	uint32_t now = k_uptime_get_32();
	if (!bci_trigger_accept(now, &trigger_last_ms, &trigger_seen)) return;
	struct trigger_event event = { .number = (uint32_t)atomic_inc(&trigger_count) + 1U };
	if (k_msgq_put(&trigger_events, &event, K_NO_WAIT) != 0) {
		atomic_inc(&trigger_drops);
	}
}
```

The status thread updates PA1 every 50 ms. It logs queued trigger events as
`EXT_TRIG event=%u level=%d` outside interrupt context. For acquisition state,
it keeps PA1 on only while the latest successful ADS-to-E73 frame is less than
100 ms old. Boot and fatal patterns come from `bci_led_output()`.

- [ ] **Step 4: Replace direct PA1 manipulation in `main.c`**

- Initialize status IO before PWM/SPI startup and enter `BCI_LED_BOOT`.
- On unrecoverable initialization failure, set `BCI_LED_FATAL` and sleep forever.
- On acquisition start, set `BCI_LED_ACQUIRING`.
- After successful E73 exchange, call `status_led_note_frame_success()`.
- On acquisition stop, set `BCI_LED_STOPPED`.
- Remove `work_led_set()`, `work_led_update()`, and the direct PA1 GPIO spec from `main.c`.

- [ ] **Step 5: Run ztests and build the full MCUboot image**

Expected: ztests pass; full STM32 sysbuild exits zero; no E73, Dongle, GUI, or protocol source changes appear in `git diff`.

- [ ] **Step 6: Commit status IO**

```powershell
git add products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/feature_logic.* `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/status_io.* `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/main.c `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/CMakeLists.txt `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic
git commit -m "feat: add STM32 status LED and external trigger input"
```

### Task 4: Non-Blocking SD Recorder

**Files:**
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/sd_recorder.h`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/sd_recorder.c`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/CMakeLists.txt`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/main.c`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic/src/main.c`

**Interfaces:**
- Produces: `int sd_recorder_init(void)`, `int sd_recorder_start(void)`, `void sd_recorder_submit(const uint8_t frame[48])`, `int sd_recorder_stop(void)`, and `void sd_recorder_get_stats(struct sd_recorder_stats *)`.
- Consumes: unchanged 48-byte V19 frames after `build_stream_frame()`.

- [ ] **Step 1: Add tests for filename formatting and batch geometry**

```c
char name[24];
zassert_equal(bci_sd_filename(name, sizeof(name), 1U), 0);
zassert_mem_equal(name, "/SD:/BCI00001.BIN", 18U);
zassert_equal(BCI_SD_BATCH_FRAMES, 128U);
zassert_equal(BCI_SD_BATCH_BYTES, 6144U);
zassert_equal(BCI_SD_BATCH_BYTES % 48U, 0U);
zassert_equal(BCI_SD_BATCH_BYTES % 512U, 0U);
```

- [ ] **Step 2: Run ztests and verify filename/batch symbols are missing**

Expected: compilation fails on the new declarations.

- [ ] **Step 3: Implement filename and batching constants in portable logic**

`bci_sd_filename()` accepts indexes 1 through 99999 and returns `-EINVAL` for
zero, values above 99999, a null output pointer, or a buffer shorter than 18
bytes including the terminator.

- [ ] **Step 4: Implement the recorder thread and fixed queues**

Use these exact capacities:

```c
#define SD_FRAME_SIZE 48U
#define SD_QUEUE_FRAMES 1365U
#define SD_BATCH_FRAMES 128U
#define SD_BATCH_BYTES (SD_FRAME_SIZE * SD_BATCH_FRAMES)
K_MSGQ_DEFINE(sd_frames, SD_FRAME_SIZE, SD_QUEUE_FRAMES, 4);
```

The worker owns `FATFS`, `fs_mount_t`, and `fs_file_t`. It retries disk init and
mount at startup and at each recording start, scans `BCI00001.BIN` through
`BCI99999.BIN` with `fs_stat()`, opens with `FS_O_CREATE | FS_O_WRITE`, and
writes only from the worker context. It batches 128 frames, accepts a final
partial batch on stop, calls `fs_sync()` every 1000 ms, and closes on stop or
any short/failed write. `CONFIG_FS_FATFS_MOUNT_MKFS=n` prevents formatting.

`sd_recorder_submit()` checks an atomic accepting flag and calls
`k_msgq_put(..., K_NO_WAIT)`. Failure increments `sd_drop_frames`; it never
waits. Separate start/stop command and completion objects ensure control
commands cannot be lost when the data queue is full. Stop clears acceptance,
drains queued frames, writes the partial batch, syncs, closes, and acknowledges.

- [ ] **Step 5: Integrate recorder lifecycle without changing radio behavior**

- Call `sd_recorder_init()` once during boot; log failure and continue.
- Call `sd_recorder_start()` at every acquisition start; ignore a negative return after logging it.
- Call `sd_recorder_submit(stream_frame)` immediately after building each frame and before `rf_exchange()`.
- Call `sd_recorder_stop()` after acquisition has stopped producing frames.
- Extend the five-second RTT report with mount/recording state, bytes, dropped frames, and last SD error.

- [ ] **Step 6: Run ztests and full signed build**

Expected: portable tests pass; STM32 sysbuild exits zero; the signed application remains within the 448 KiB slot; build metadata contains all expected images; the staging private key is removed.

- [ ] **Step 7: Commit SD recording**

```powershell
git add products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/sd_recorder.* `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/feature_logic.* `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/src/main.c `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/CMakeLists.txt `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tests/feature_logic
git commit -m "feat: record STM32 acquisition frames to SDMMC"
```

### Task 5: Documentation, Source Controls, and Release-Candidate Verification

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`
- Modify: `products/manifest.json`
- Modify: `docs/superpowers/plans/2026-09-02-stm32-sd-led-external-trigger.md`

**Interfaces:**
- Consumes: all production source and generated `build-metadata.json` from Tasks 1-4.
- Produces: reproducible developer instructions and exact source-verifier authorization.

- [ ] **Step 1: Document the final behavior and hardware test commands**

Add the confirmed pin table, FAT32 requirements, automatic filename behavior,
RTT trigger line format, LED state table, SD error isolation, and the 30-minute
1000 SPS acceptance procedure. State explicitly that timestamped trigger
storage and GUI marker transport are deferred.

- [ ] **Step 2: Update exact source hashes in `products/manifest.json`**

For every modified or added STM32 development source, run:

```powershell
git hash-object -- products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/<path>
```

Record the returned blob hash under `stm32_development_overrides`. Do not modify
the frozen source-reference hashes or release-image hashes.

- [ ] **Step 3: Run all automated checks**

```powershell
python -m unittest products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py
python -m unittest products/test_verify_sources.py
python products/verify_sources.py
pwsh -File products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1 doctor -Json
```

Expected: every test passes, source verification passes with only authorized
STM32 updates, and doctor reports `ready: true`.

- [ ] **Step 4: Produce and inspect a clean external release-candidate build**

Use version `19.4.0` and a development key until the production key is created:

```powershell
pwsh -File products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1 build `
  -Version 19.4.0 `
  -BuildDir D:\ncs-builds\stm32h563-v19.4.0-rc `
  -KeyPath D:\ncs-builds\temporary-keys\stm32-repro-only-ecdsa-p256.pem
```

Verify `build-metadata.json`, partition addresses, signed image size, hashes,
and absence of the staged key. Do not copy generated HEX, BIN, or ZIP files
into the repository.

- [ ] **Step 5: Review scope and commit documentation/control updates**

Run `git diff --name-only` and verify no files under E73, Dongle, GUI, ESP32 EEG,
or ESP32 EMG changed. Then commit:

```powershell
git add products/manifest.json `
  products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md `
  docs/superpowers/plans/2026-09-02-stm32-sd-led-external-trigger.md
git commit -m "docs: verify STM32 SD LED and trigger firmware"
```

### Task 6: Deferred Hardware Acceptance

**Files:**
- Modify after testing: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`

**Interfaces:**
- Consumes: the external `19.4.0` release-candidate factory image and connected STM32 board.
- Produces: recorded pass/fail evidence; no protocol or GUI changes.

- [ ] **Step 1: Back up the programmed board before writing**

Use J-Link to read the full STM32 flash before flashing the candidate. Store the
backup outside Git with its SHA-256 hash and board identifier.

- [ ] **Step 2: Flash the factory candidate and capture RTT boot logs**

Verify slow boot blinking, SD mount result, ADS self-check, MCUboot confirmation,
and healthy acquisition indication.

- [ ] **Step 3: Verify PB7 hardware trigger**

Apply 100 pulses more than 5 ms apart and verify exact RTT events 1 through 100.
Apply repeated edges within 5 ms and verify one accepted event.

- [ ] **Step 4: Verify optional SD behavior and long recording**

Boot without a card and confirm Dongle acquisition remains usable. Insert a
FAT32 card before boot, record at 1000 SPS for at least 30 minutes, and validate
that the BIN size is divisible by 48 with valid CRC and sequence continuity.

- [ ] **Step 5: Verify SD fault isolation and firmware recovery**

Use a full test card to force a write failure, confirm the radio path continues,
then validate signed USB DFU success, failed-image rollback, and J-Link recovery.

- [ ] **Step 6: Record evidence without committing generated firmware**

Add concise results, board revision, test duration, error counters, and artifact
SHA-256 values to the README. Keep flash dumps and HEX/BIN/ZIP artifacts outside
Git.
