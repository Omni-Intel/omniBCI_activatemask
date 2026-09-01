# STM32 Development and Recovery Environment Design

## Scope

Build a reproducible Windows environment for the existing STM32H563 Zephyr/NCS firmware before changing the work LED or external-trigger behavior. The environment must build the current MCUboot application, preserve the existing board before any write, and support J-Link recovery plus later USB DFU verification.

E73 and Dongle firmware are not modified or flashed in this stage. The stable ESP32 EEG product is outside this scope.

## Fixed Baseline

- Repository: `D:\PycharmProjects\onmiBCI_activatemask`.
- Firmware project: `products/stm32_bci/firmware/STM32_E73_DONGLE_V19/source/stm32`.
- SDK: nRF Connect SDK v3.4.0, installed under `D:\ncs` through official `nrfutil sdk-manager`.
- Probe: SEGGER J-Link V9.64 at `C:\Program Files\SEGGER\JLink_V964`.
- Target: `STM32H563VG`, SWD, 100 kHz diagnosis then 1 MHz development connection.
- Reference release: V19.2 factory `07` and update `08`; their original private signing key is unavailable.

## Architecture

One repository-local PowerShell entry point exposes deterministic commands: `doctor`, `build`, `keygen`, `backup`, `diagnose`, `flash-factory`, and `release`. It discovers the SDK through nrfutil instead of requiring permanent PATH edits, validates every path before execution, and refuses destructive commands unless their prerequisites are present.

Build output, device backups, generated releases, logs, and private keys remain outside Git. The script prints exact artifact paths and SHA-256 values so a second developer can reproduce every operation.

## Signing-Key Policy

Generate one ECDSA-P256 key at `source/stm32/keys/stm32-mcuboot-ecdsa-p256.pem`. Git already ignores PEM files. The key is not considered usable for board migration until the operator records two offline backups. The repository stores only instructions and a public fingerprint, never private-key contents.

Because the original private key is missing, an existing board cannot accept newly signed USB updates until J-Link installs a complete factory image containing the new public key. Before that migration, the full 1 MiB STM32 flash must be read and hashed.

## Safety and Failure Handling

- `doctor`, `build`, and script self-tests require no hardware.
- `diagnose` performs read-only connection checks.
- `backup` reads flash and never erases or resets into a new image.
- `flash-factory` requires an existing backup record, a factory HEX, and explicit confirmation text.
- No J-Link or PC USB may be connected while human electrodes are attached unless compliant medical isolation is present.
- A command failure returns a nonzero exit code and leaves an operation log.

## Verification Gates

1. Tool doctor identifies NCS v3.4.0, west, Zephyr SDK, CMake, Ninja, imgtool, mcumgr support, J-Link Commander, and GDB.
2. Current STM32 sysbuild completes from a clean build directory.
3. Factory HEX spans MCUboot at `0x08000000` and signed slot0 at `0x08010000`; update BIN has MCUboot magic and version greater than `19.1.0`.
4. Tests prove destructive operations are refused without a backup and explicit confirmation.
5. Hardware gates, performed later: flash backup, V19.2 identification, new-key factory migration, USB upload/list/test/reset/confirm, forced failure rollback, and J-Link recovery.

## Out of Scope

- Work LED behavior changes.
- External-trigger GPIO, timestamp, or protocol changes.
- SD card support.
- E73/Dongle rebuild or flashing.
- Automatic publication of private or generated artifacts.
