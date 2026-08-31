# STM32 BCI: Isolated Reference Snapshot

**REFERENCE ONLY, not a final feature-parity release.** This product archives
the STM32 GUI and firmware from
`c2ecbd6423083ae5a196f86d24ce82119ed27752`. SD recording, hard-trigger support,
and the later STM32 GUI update are pending and are not developed here.
The separate stable ESP32 EEG and ESP32 EMG products are not selected by this GUI.

## Hardware and Versions

The pairing is ADS1299 -> STM32H563VGT6 -> E73-2G4M08S1C -> private 2.4 GHz ->
nRF52840 Dongle -> Dongle USB COM -> this reference GUI. Acquisition and control
must use the **Dongle COM port**. Native STM32 USB is for diagnostics/update only;
neither BCI-Band Data CDC nor BCI-Band DFU CDC is a GUI acquisition-control port.
Identify and select the Dongle COM manually. Existing VID hints are not a reliable
Dongle/native-USB distinction. No new port auto-detection or identity command is added.

The imported package is `firmware/STM32_E73_DONGLE_V19/`:

| Component | Archived version/artifact |
| --- | --- |
| GUI | Source commit above; inherited V19 release label |
| STM32 | V19.2 hardware-SPI MCUboot factory: `07_stm32h563_v19_2_hwspi_factory.hex` |
| STM32 update | `08_stm32h563_v19_2_hwspi_update.bin` and `09_stm32h563_v19_2_hwspi_update.zip` |
| E73 | V19: `02_e73_v19_full_control.hex` |
| Dongle | V19: `03_dongle_v19_full_control.hex` |
| Online data | Existing V1 48-byte frames; 250/500/1000 SPS controls |

These are imported versions, not newly built releases. Consult the integration
manifest for exact provenance/hashes and the archived firmware READMEs for image
layout and update procedures. The archived legacy `01_...hex` is not the
MCUboot factory image. **No imported HEX was rebuilt or flashed, and no hardware
test was performed for this integration.** Historical validation claims in the
archived firmware README are upstream claims, not new acceptance results.

## Setup and Launch

Run these PowerShell commands from this product directory. Python 3.12 is the
software-check environment. With uv installed, use the archived lockfile and
this product's own `app/.venv`; do not install into the stable environment.

```powershell
uv sync --locked --all-extras --project app
.\run.bat
```

`--all-extras` includes the already-declared export dependencies. The snapshot's
configured package mirror must be reachable; do not regenerate its lockfile to
work around installation failures. Direct launch is
`.\app\.venv\Scripts\python.exe -B .\run_stm32.py`.
Always use the product wrapper; starting the archived app directly bypasses
isolation and restores its original universal-MCU behavior/default API port.
No new dependencies are introduced.

## Isolation

`app/` and `firmware/` source files remain unchanged. The wrapper makes only
process-local startup adaptations:

- QSettings uses `app/stm32_bci.ini` with no settings fallbacks. It does not read,
  migrate, or write the stable `OmniBCI/ADS1299EEGWorkbench` registry settings.
- The fixed family is STM32, with one MCU entry and serial-only behavior, even
  if an old product INI contains `esp32`. No BLE worker is started.
- The single-instance lock is `%TEMP%/omnibci_stm32_bci.lock`, separate from the
  original `omnibci_v18_gui.lock`. Multiple copies of this STM32 wrapper still
  share its product lock within the same user's temporary directory.
- The working directory is this product's `app/`; default logs and recordings
  stay in `app/logs/` and `app/recordings/`. User-selected import/export paths
  remain user-controlled. Python bytecode writes are disabled by the wrapper.
- The local API listens on `127.0.0.1:8767`, not the stable default `8765`.
  The wrapper does not discover ports or fall back to another product's API.

This is a narrow adaptation for the exact archived revision, not a shared GUI
framework. Inherited UI text may still say USB, EEG, or V19; those labels do not
authorize connecting native STM32 USB for acquisition.

## SDK and Known API Limits

Use the SDK from this product's `app/` in a separate Python process and always
pass its URL explicitly. For example, from this product directory:

```python
import sys
sys.path.insert(0, "app")
from onmibci_sdk import LocalClient

client = LocalClient(url="ws://127.0.0.1:8767/v1/stream")
with client.stream_raw() as stream:
    for event in stream:
        print(event)
```

The SDK derives `ws://127.0.0.1:8767/v1/control` from that URL. Its unchanged
default URL still targets port 8765; do not use `LocalClient()` without a URL.
If 8767 is occupied, the archived GUI reports the API as unavailable; it does
not move to another port. Resolve that conflict before using the SDK.

**The reference API/SDK inherits fixed 250 Hz sample-rate metadata.** Selecting
500 or 1000 SPS in the GUI does not make this API metadata reliable. Do not use
it for timing-dependent 500/1000 SPS processing. Runtime metadata consistency
and full parity with the other product GUIs require the later STM32 GUI stage;
this wrapper changes only the API port, not the protocol or sample-rate logic.

## Software Check

```powershell
.\app\.venv\Scripts\python.exe -B .\test_launcher.py
.\app\.venv\Scripts\python.exe -B .\test_launcher.py --screenshot .superpowers\stm32-reference.png
```

The stdlib assert-based check copies the app into a temporary directory, uses
Qt offscreen, and checks isolated INI settings, single-instance locking, fixed
STM32/serial behavior, unchanged runtime directory rules, GUI construction,
and an actual SDK handshake on port 8767. That port must be free. It opens no
hardware connection and does not use stable settings or recordings. The product
`.gitignore` excludes its INI (including Qt temporary/lock suffixes) and
`.superpowers/` screenshots; root ignore rules already exclude virtual
environments, bytecode, logs, recordings, and Ruff caches.
This is not hardware acquisition, firmware-build, SD, trigger, or final GUI
feature-parity acceptance.
