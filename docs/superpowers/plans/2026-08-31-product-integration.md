# Three-Product Integration Plan

**Goal:** Collect the three supported hardware/software pairings on master without changing the working EEG runtime or firmware.

**Architecture:** Preserve the 1402ec8 runtime and firmware. Add isolated product snapshots beneath products/esp32_emg and products/stm32_bci. Port only necessary full-differential and sampling-rate behavior into the EMG copy of the baseline GUI; do not merge shared GUI files wholesale. Five explicitly authorized packaging-only changes are recorded separately in the source manifest.

**Tech Stack:** Existing Python 3.12/PySide6 stack, existing ESP32 Arduino firmware, and existing STM32/E73/Dongle Zephyr sources.

## Global Constraints

- EEG baseline: 1402ec8f9d059afad56e2bfa204743bd90f54ef3; runtime, firmware, lockfile and run.bat remain unchanged. Packaging exceptions are enumerated in products/manifest.json.
- EMG firmware source: 8779008d749927eb52c2d555221ce319c2047200, keep its validated sketch unchanged.
- STM32 reference: c2ecbd6423083ae5a196f86d24ce82119ed27752, no new SD/trigger implementation or GUI feature work in this stage.
- Dongle owns STM32 acquisition control; native STM32 USB is diagnostics/update only.
- Do not change the stable Python environment, settings, or recordings. The user has authorized adding the integrated product directories to the running master checkout.
- No universal MCU selector; no new identity query in the frozen EEG firmware.
- Preserve baseline GUI features including software trigger, channel-name-only edits, API labels and exports.
- Runtime sample rate must agree across EMG acquisition, filters, PSD, recording, exports, API and SDK.
- Use independent per-product settings, logs, recordings and environments.
- No hardware flashing. On 2026-08-31 the user explicitly authorized final code review, commit, master merge, post-merge checks and push. Hardware acceptance remains a separate release gate.

## Task 1: Baseline and Product Snapshots

- [x] Create external worktree from the baseline and run baseline tests using temporary Qt settings.
- [x] Copy the baseline runtime, assets, tests and dependency declarations into the EMG product only; exclude .git, .venv, recordings and local settings.
- [x] Import exact EMG sketch and the STM32 three-target source/release tree using Git object provenance.
- [x] Archive the STM32 GUI source as a reference pending its SD/trigger stage.
- [x] Add a manifest and a stdlib verifier that compares baseline Git blobs and imported firmware SHA-256 values.

## Task 2: Isolated EMG GUI

- [x] First add failing tests for supported rates, fixed full-differential semantics, state isolation, channel settings and API metadata.
- [x] Adapt only products/esp32_emg/app; use the existing native baseline GUI and modules, not the universal-MCU branch GUI.
- [x] Copy the collaborator's existing sampling-rate command semantics: USB AA rate-code and BLE MSG_SET_SAMPLE_RATE=0x06, CONFIG1 and rate readback.
- [x] Handle full differential P/N BIAS and disabled SRB semantics without changing firmware.
- [x] Preserve software trigger, name-only edits and user-facing baseline workflows.
- [x] Test all 250/500/1000 SPS paths, bad ACK rejection, file/export/API metadata and software-only operation.

## Task 3: Launch, Documentation, Review

- [x] Add per-product launch/setup instructions and configuration isolation; keep root launchers unchanged.
- [x] Document the exact hardware/GUI/firmware pairing and separate software checks from hardware acceptance.
- [x] Run unchanged baseline suite, product suite, source integrity checks and offscreen desktop GUI screenshots.
- [x] Independently review provenance and the STM32 launcher; resolve findings and rerun checks.
- [x] Complete independent review of the EMG adaptation before merging. Four final findings were reproduced and fixed; independent re-review passed 27 targeted tests with no new blocker. Full product suite: 107 tests and 56 subtests passed.
- [x] Commit the local candidate on codex/product-integration (1f0110b).

## Final Integration

- [x] Commit the authorized EEG-only packaging repair and updated integration records (d6157aa, 60d4708, 15edb8f).
- [x] Preserve 8779008 and c2ecbd6 in master ancestry without changing the reviewed snapshot tree (7c6e245; c2ecbd6 is already an ancestor of 8779008).
- [x] Integrate into master, preserving the existing packaging changes and all local recordings/environments. The six local packaging files match their pre-merge backup exactly.
- [x] Recheck all three product entries, test suites, frozen sources and absence of desktop build artifacts. Post-merge: EEG 99, EMG 107, STM32 50 tests; source verifier 164 files and 5 verifier tests; Ruff and STM32 launcher pass. A full master-only clone also passes source verification.
- [x] Push master without force and verify that the remote commit matches (7c6e2457d12af17d16964a4461bf33c5993ac357). Subsequent documentation-only commits do not change the tested runtime tree.
