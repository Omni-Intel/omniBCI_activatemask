# V14 low-latency changes

V14 keeps V13's separate BLE receive, raw BIN writer, live filter worker and Qt paint paths, but reduces display latency.

- BLE GUI poll: 8 ms -> 4 ms.
- BLE coalescing: 16 frames / 60 ms -> 8 frames / 25 ms.
- Waveform paint: 10 FPS -> 20 FPS.
- Filter result poll: 5 ms -> 3 ms.
- Wireless display reserve: 2.0 s -> 0.65 s target, 0.55 s startup, adaptive cap 1.0 s.
- The display cursor accelerates up to 1.75x after a delivery burst.
- If screen history becomes older than about 1.12 s, only the live display cursor resynchronises to the 0.65 s target. Raw BIN bytes, sequence accounting and filtered ring data are not deleted.

The compact reliable BLE V2 wire protocol is unchanged from V13. A board already running V13 firmware does not need to be reflashed for the V14 GUI.
