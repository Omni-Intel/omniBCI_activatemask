# STM32 Development Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish and verify a reproducible NCS v3.4.0 build, signing, J-Link backup, recovery, and DFU-development environment for the STM32H563 firmware.

**Architecture:** Use the official Nordic SDK manager for a pinned SDK and one repository-local PowerShell command surface. Keep SDKs, builds, private keys, backups, logs, and generated releases out of Git; enforce safety gates before any target write.

**Tech Stack:** Windows PowerShell 7, nrfutil sdk-manager, NCS v3.4.0/Zephyr/west, MCUboot imgtool, SEGGER J-Link V9.64, Python stdlib tests.

## Global Constraints

- Do not modify or flash E73, Dongle, ESP32 EEG, or EMG firmware.
- Do not write target flash until a complete backup exists and the board is connected in a later hardware step.
- Never commit a PEM private key, build output, device backup, or desktop artifact.
- New signed images must use a version greater than `19.1.0`.
- Commands must work from paths containing spaces and non-ASCII characters by using literal, resolved paths.

---

### Task 1: Pin the Environment Contract

**Files:**
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`

**Interfaces:**
- Produces: `fw.ps1 doctor`, returning zero only when the pinned tools can be launched.

- [x] Write a stdlib test that invokes `fw.ps1 doctor -Json` with temporary fake tools and asserts exact version/path checks plus nonzero failure on a missing tool.
- [x] Run the test and verify it fails because `fw.ps1` does not exist.
- [x] Implement only tool discovery, JSON output, exit codes, and `doctor` help.
- [x] Run the focused test and existing source-verifier tests.
- [x] Commit `feat: add STM32 firmware environment doctor`.

### Task 2: Install and Verify NCS v3.4.0

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1`

**Interfaces:**
- Consumes: official `nrfutil sdk-manager` installed under `D:\ncs`.
- Produces: a real `doctor` report with SDK, west, CMake, Ninja, Python/imgtool, J-Link, and GDB paths.

- [x] Install official nrfutil and the `sdk-manager` command without changing repository files.
- [x] Install NCS v3.4.0 to `D:\ncs` and list the resolved installation.
- [x] Add the real sdk-manager launch adapter to `fw.ps1`, preserving the fake-tool test interface.
- [x] Run `fw.ps1 doctor -Json`; verify every required tool executes and reports the pinned SDK.
- [ ] Commit `docs: pin the STM32 NCS development environment`.

### Task 3: Reproduce the MCUboot Build

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/prj.conf`
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`
- Test: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py`

**Interfaces:**
- Produces: `fw.ps1 build -Version 19.3.0` and artifact metadata from the clean sysbuild directory.

- [x] Add failing tests for semantic version rejection (`19.1.0` and malformed input) and build command construction with literal paths.
- [x] Run tests and verify the new cases fail.
- [x] Implement `build`, passing the requested version through an isolated staging copy without editing tracked files per build.
- [x] Generate a temporary development signing key, run a pristine sysbuild, and capture the first real compiler/configuration error if any.
- [x] Fix only reproducibility blockers found by the build; rerun until the build exits zero.
- [x] Validate factory/update image headers, addresses, sizes, and hashes.
- [ ] Commit `build: reproduce STM32 MCUboot firmware with NCS 3.4.0`.

### Task 4: Key Generation and Release Metadata

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/keys/README.md`
- Test: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py`

**Interfaces:**
- Produces: `keygen`, public fingerprint output, and `release -Version <semver>`; never emits private contents.

- [ ] Add failing tests that refuse overwrite, require a backup-acknowledgement marker, and ensure JSON/log output contains no PEM body.
- [ ] Implement minimal key generation via the SDK imgtool and SHA-256 public fingerprint recording.
- [ ] Generate the project key locally and create two user-visible offline backup copies outside the repository.
- [ ] Build version `19.3.0`; produce factory HEX, signed update BIN, manifest, and SHA-256 list in an ignored release directory.
- [ ] Commit `feat: add managed STM32 signing and release workflow` without staging the key or generated artifacts.

### Task 5: J-Link Backup, Diagnosis, and Flash Safety

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/fw.ps1`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/jlink_backup_stm32.template.jlink`
- Test: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/tools/test_fw_tools.py`

**Interfaces:**
- Produces: `diagnose`, `backup`, `identify`, and gated `flash-factory` commands.

- [ ] Add failing tests for safe J-Link script generation, exact 1 MiB backup range, backup SHA-256 record, known-release identification, and flash refusal without confirmation `FLASH STM32 FACTORY`.
- [ ] Implement script generation with paths escaped for J-Link Commander.
- [ ] Verify no-hardware tests and ensure `diagnose` never contains erase/load commands.
- [ ] Connect the board later; run diagnosis, backup, and identification before any write.
- [ ] Commit `feat: add guarded STM32 J-Link backup and recovery commands`.

### Task 6: DFU and Recovery Acceptance Procedure

**Files:**
- Modify: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/README.md`
- Create: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32/DFU_ACCEPTANCE.md`

**Interfaces:**
- Produces: a reproducible hardware checklist and machine-readable log locations for factory migration, USB DFU, rollback, and recovery.

- [ ] Document exact preconditions, expected CDC names, mcumgr commands, image-state transitions, and rollback observations.
- [ ] Perform new-key factory migration only after backup and offline key backup gates pass.
- [ ] Validate upload/list/test/reset/confirm with a good image.
- [ ] Validate one deliberately non-confirming trial image rolls back after reset without erasing the recovery image.
- [ ] Reflash the good factory image with J-Link and rerun acquisition smoke tests.
- [ ] Commit `docs: record STM32 DFU and J-Link recovery acceptance`.

## Self-Review

- Scope covers pinned tools, clean build, new key, protected release, read-first J-Link workflow, DFU and rollback.
- No task modifies work LED, trigger, E73, Dongle, or frozen ESP32 code.
- Private/generated files remain ignored and every destructive step has an explicit gate.
- Hardware-dependent steps are separated from the current no-board setup and cannot be claimed complete without logs.
