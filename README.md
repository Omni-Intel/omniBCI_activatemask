# ADS1299 Native Python GUI P0P1 V15

V15 keeps the V14 acquisition architecture and adds one more rule: **acquisition and recording must not wait for painting**.

## Data path

```text
ADS1299 DRDY/SPI
      ↓
ESP32-C3 frame queue (256 frames)
      ↓
RAM reliable retention ring (320 compact blocks, about 7.68 s)
      ↓
BLE compact block V2: 6 samples / notification
      ↓
Windows BLE receive thread
      ├── raw BIN writer thread
      ├── live filter worker thread
      └── Qt main thread: counters + already-computed waveform paint only
```

The BIN file remains the standard 48-byte P0P1 format. BLE removes only transport-only diagnostic bytes; the GUI reconstructs each 48-byte frame and CRC before parsing and saving.

## Why BLE can look stuck even when the board is still sampling

BLE delivery is not a continuous byte pipe. Connection events, Windows' Bluetooth stack, GATT notification queues and retransmission bursts can deliver several notifications together and then pause. If parsing, filtering, disk writes and eight plots share one Qt callback, one burst can starve window events and look like a frozen app.

V14 therefore separates receive, filtering, recording and painting. Painting/PSD may temporarily yield, but raw receive and BIN recording continue.

## OpenBCI/BrainFlow-inspired changes

- Fixed compact BLE records and multi-sample packets, similar in spirit to Ganglion's compressed packet/ring-buffer design.
- Acquisition ring buffer with independent consumers, matching BrainFlow's streaming-thread + internal-ringbuffer + file-streamer architecture.
- Cumulative ACK, NACK repair, bounded in-flight window and retained blocks.
- Six samples per compact BLE block: about 41.7 DATA notifications/s instead of 62.5/s in V12.
- 320 retained blocks now cover about 7.68 s rather than about 5.12 s.
- The in-flight window is reduced to 16 blocks so Windows is not flooded during catch-up.

## Why internal Flash is not used as the real-time FIFO

ESP32 internal Flash needs page programming and sector erasing. Those operations have variable latency and finite erase life; using it for every EEG sample can create the exact stalls we are trying to remove. V14 uses RAM retention for short BLE interruptions.

For guaranteed recovery after an arbitrarily long disconnect, use the board's external TF/microSD as an independent local recorder, then reconcile by sample sequence after the session. That is a storage feature, not a substitute for BLE flow control.

## Firmware

- SRB1: `firmware/ESP32C3_ADS1299_SRB1_BLE_V14/ESP32C3_ADS1299_SRB1_BLE_V14.ino`
- SRB2: `firmware/ESP32C3_ADS1299_SRB2_BLE_V14/ESP32C3_ADS1299_SRB2_BLE_V14.ino`
- USB-only control: `firmware/ESP32C3_ADS1299_SERIAL_STABLE_V14/ESP32C3_ADS1299_SERIAL_STABLE_V14.ino`

The V14 GUI requires compact reliable protocol V2, which is provided by both the V13 and V14 BLE firmware. The V14 GUI also remains able to decode old reliable V1 blocks for comparison.

## Diagnostics

- `Filter queue/out`: samples waiting for or returned by the background filter.
- `Filter peak/batch`: worst calculation backlog and processed batch count.
- `Raw queue`: bytes waiting for the BIN writer thread.
- `Reliable pending`: out-of-order BLE blocks waiting for repair.
- `FW retained/flight`: blocks retained on the board and currently unacknowledged.
- `Paint / PSD skip`: intentional UI work skipped to protect acquisition.

An occasional paint/PSD skip is healthy. Rising raw-writer errors, forced skips, firmware overflow or permanent filter backlog is not.

## Start

1. Extract to a short ASCII path such as `C:\OmniBCI\V15` (avoid parentheses).
2. Keep the working V13 BLE firmware, or burn the included matching SRB1/SRB2 V14 firmware.
3. Run `install_and_run.bat` or `python ads1299_eeg_gui_native.py`.
4. Test for at least 30 minutes and save the diagnostics page if any gap occurs.

## V14 low-latency profile

V14 keeps recording lossless and independent from painting, while targeting about 0.65 s live-display delay. The adaptive screen buffer is capped near 1.0 s. After a Windows BLE burst, the display cursor temporarily catches up faster; if the screen alone becomes too stale, it jumps to the latest buffered region. This affects only what is currently painted. The raw BIN stream and sequence diagnostics are unchanged.

The V14 GUI is wire-compatible with V13 compact reliable BLE firmware, so reflashing is optional when V13 is already installed and stable.


## V15 饱和保护

图中那种密集实心色块通常是输入在正负满量程之间快速跳变。每个采样点都会形成一条跨越整个纵轴的竖线，8 个通道以 20 FPS 重画时会让 Qt 绘图线程被 QPainter 路径占满，看起来像整窗卡死。饱和值同时还会污染连续 IIR 状态，使信号恢复后出现很长的滤波瞬态。

V15 保留原始 BIN 和序号，只在实时滤波副本与屏幕副本中把饱和点变成空白段。一个通道饱和不会再拖住其他通道；状态栏会显示 `饱和保护：CHx`。选中通道仍在饱和时，PSD 暂停，恢复后自动继续。

这是 GUI-only 更新，V13/V14 BLE 固件无需重新烧录。

## V15 Split-BIN 修复

之前重构后台写盘线程时，误把 V9/V10 的自动分包逻辑替换成了单文件写入。这个修正版已恢复：每 60 秒自动生成一个 `MMDD_HHMM_ID_minuteNN.bin`，同时生成会话 `manifest.json` 和每包 `.meta.json`。分包、换文件、flush 和元数据写入全部仍在独立写盘线程中，不会重新把磁盘 I/O 压回 Qt 主线程。V15 饱和保护与低延迟显示保持不变，无需重新烧录固件。
