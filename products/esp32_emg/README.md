# OmniEMG Full Diff V20 integration.1

Dedicated ESP32-C3 / ADS1299 full-differential product. **Software revision:
V20 integration.1**. The separately versioned firmware is the unchanged **V20.0**
sketch, not a new firmware release. Independent software review is complete;
hardware acceptance is still pending. Integration is not a hardware release.

## Provenance

- Native software, assets, SDKs, tests and dependency declarations: Git
  `1402ec8f9d059afad56e2bfa204743bd90f54ef3`. The actual entry is
  `app/ads1299_eeg_gui_native.py`, using `app/omnibci/`.
- Firmware sketch **and its README**, imported byte-for-byte from
  `8779008d749927eb52c2d555221ce319c2047200`:
  `firmware/ESP32C3_ADS1299_FULL_DIFF_BLE_V20/`.
- Collaborator `onmibci_gui/channel_config.py`, `transport_control.py`, and
  `onmibci_ble_protocol.py` at that same commit informed the protocol port.
  The universal MCU GUI was **not** imported. No EEG firmware is included or changed.
- `pyproject.toml`, `uv.lock`, `.python-version`, and dependency lists retain the
  baseline contents. Their historical project metadata does not identify this
  software revision. The stable EEG environment is untouched; product environments are installed independently.

## Run

Windows and uv are required; the locked project uses Python 3.12.
Run `setup.bat` to install this product's locked dependencies, including BDF/FIF,
then `run.bat` to launch. The optional-export script calls the same locked setup.
These scripts never flash firmware or install into the EEG environment.

Alternatively, in `app/`: `uv sync --locked --all-extras`, then
`.venv\Scripts\python.exe ads1299_eeg_gui_native.py`.
Do not regenerate the lockfile to work around an unavailable package mirror.

Core packages: PySide6, pyqtgraph, numpy, scipy, pyserial, bleak, websockets.
Exports require pyedflib and MNE. Exact declarations are under `app/`.

Isolation:

- Environment: `app/.venv/`; never the repository-root EEG environment.
- Recordings and logs: `app/recordings/`, `app/logs/`.
- QSettings organization/application: `OmniBCI` / `ESP32_EMG_V20`.
- Single-instance lock: `app/logs/omniemg-v20.instance.lock`.
- Local API and both SDK defaults: `ws://127.0.0.1:8766/v1/stream`.
  See `app/public_sdk/API_SDK_GUIDE.md`. `hello.sample_rate` describes subscription
  time; each raw/filtered batch carries its own current `sample_rate` and labels.

## Supported Behavior

Only full-differential V20 / protocol 1 is intended. BLE verifies the V20 major
version and full-differential capability; USB requires `0xAB` profile 3 and the
same capability before synchronizing channels. Each channel measures INxP-INxN;
SRB1/SRB2 remain OFF. Modes 0 and 1 both use BIAS P+N. Frame flag bit 7 being clear
does **not** imply SRB2. No MCU or reference-topology selector is exposed.

Rates are 250/500/1000 SPS (codes 0/1/2, CONFIG1 0x96/0x95/0x94). Change rates only
while acquisition and impedance detection are stopped. USB sends `AA <code>` and
validates the 12-byte XOR-checked ACK. BLE sends message `0x06` with one code byte
and validates the 29-byte register snapshot, result, verified bit, rate, CONFIG1,
and full-differential BIAS/SRB state. Lost or invalid rate replies block acquisition
until a successful rate transaction or reconnect establishes device timing.

Timing flows through rings, filters, PSD, display axes, minute segmentation,
recording sidecars, BDF/FIF and both SDKs. BIN import restores saved rate/gain/name
metadata; BIN without metadata asks for the original rate. BDF import retains
supported source rates. Completed-recording BDF export uses saved rate/gains even
after the GUI rate changes. Device rate changes do not retime an already loaded
offline file; device timing and cached device gains are restored when acquisition
or impedance detection starts. Impedance detection also requires verified device
timing. Invalid BIN metadata
is rejected before replacing the currently loaded file's rate, gains or names.
Native software trigger, name-only edits without
hardware commands, current API labels, raw and filtered streams remain available.
Channel names remain session-local, matching the native baseline.

Impedance detection enables both P and N lead-off registers. `LOFF=0x02` selects
nominal 31.25 Hz (`fCLK/65536`), independent of sample rate, per the
[TI ADS1299 datasheet, section 9.6.1.5](https://www.ti.com/lit/ds/symlink/ads1299.pdf).
The displayed value is an uncalibrated P/N-loop estimate, not an independently
verified electrode resistance. External series compensation starts at zero:
calibrate with known loads on the actual EMG hardware before interpreting it.
Legacy alpha diagnostics and default 5-50 Hz display filtering are retained;
raw acquisition and raw exports are not band-limited by that display filter.

## Software Verification

From `app/`, use a Python with the declared dependencies:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONPATH = (Get-Location).Path
python -B -m unittest discover -s tests -v
```

Tests use fake transport responses, localhost sockets, temporary recordings and
isolated QSettings. They do not scan/connect BLE or flash devices. For this
integration, final verification used the product's independent `app/.venv`.
The complete suite passed 107 tests and 56 subtests; independent review also
passed 27 targeted tests. The stable EEG environment was not synchronized.

Copied tests are adapted to the native entry and V20 fixtures. Split-GUI-only
architecture/V4-rescue tests and split-only bulk-naming/persistence checks are not
applicable and are omitted, not replaced by a dependency on the EEG root GUI.
Native naming, trigger, transport reliability, raw/filter API and exports remain
tested. Firmware source checks describe the actual frozen V20, **not** later EEG
snapshot-copy reliability fixes: V20 uses shared retained-ring slots, and its
512-frame acquisition queue is only 0.512 seconds at 1000 SPS.

**No hardware was verified by this implementation.** Pending acceptance includes
actual register/rate readback at all rates over USB/BLE, known-input polarity and
gain, disconnect/reconnect continuity, sustained 1000-SPS throughput, simultaneous
recording/API/export, trigger timing and calibrated P/N impedance measurements.
