# V16 Continuity Fix

Priority order for live acquisition:

1. receive and preserve every transport frame;
2. keep the live waveform moving continuously;
3. run filtering;
4. update PSD/quality metrics only with spare CPU.

## Saturation

- Saturated ADC samples are no longer converted to NaN before the live causal filter.
- Raw BIN, sequence counters, filter input and PSD all keep the real finite rail samples.
- The screen copy is clipped to each channel's visible y-range immediately before painting. This prevents full-scale rail toggling from creating huge QPainter paths while keeping the waveform continuous.
- PSD does not pause merely because a channel is saturated.

## PSD isolation

- Live PSD uses a fixed-cost path: one raw Welch plus one filtered Welch on the newest six seconds.
- The many-overlapping-4-second quality-window loop is retained only for the richer offline/capture path.
- PSD owns a private one-thread QThreadPool and remains strictly single-flight. If one calculation is still running, the next timer tick is ignored rather than queued.
- Live PSD refresh is 1.5 s. This is intentionally lower priority than transport and waveform continuity.

## BLE waveform continuity

- Removed the old behavior that deliberately paused painting when BLE/filter backlog crossed a threshold.
- Removed the full rebuffer wait that stopped the waveform until the jitter reserve refilled.
- BLE live display now slows smoothly when its reserve is low and resumes immediately when new data arrives.
- Target display reserve is ~0.72 s and capped below ~0.95 s.
- If the radio truly delivers no new EEG samples after the reserve is exhausted, software cannot invent missing samples; however the GUI no longer adds an extra refill freeze after data resumes.

## Rendering

- All live channel curves use clip-to-view and automatic peak downsampling.
- Rail values are clipped only on the paint copy; recorded/analysed values are unchanged.

## Firmware

This is a GUI-only continuity update. Keep the V16 BLE firmware. No reflash is required if V16 firmware is already installed.
