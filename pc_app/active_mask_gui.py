#!/usr/bin/env python3
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk

import serial
from serial.tools import list_ports


APP_VERSION = "active-mask-gui-v1"
BAUD_DEFAULT = 921600
FRAME_LEN = 48
SYNC = b"\xA5\x5A"
PROTOCOL_VERSION = 1
FRAME_TYPE_DATA = 1
ENABLE_COMMANDS = "!@#$%^&*"
BIAS_MASK_5CH = 0x1F
BIAS_MASK_8CH = 0xFF


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def u16_le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def u32_le(data: bytes, offset: int) -> int:
    return (
        data[offset]
        | (data[offset + 1] << 8)
        | (data[offset + 2] << 16)
        | (data[offset + 3] << 24)
    )


def parse_frame(frame: bytes) -> dict[str, int] | None:
    if len(frame) != FRAME_LEN:
        return None
    if frame[:2] != SYNC:
        return None
    if frame[2] != PROTOCOL_VERSION or frame[3] != FRAME_TYPE_DATA:
        return None
    if crc16_ccitt_false(frame[:46]) != u16_le(frame, 46):
        return None
    return {
        "seq": u32_le(frame, 4),
        "micros": u32_le(frame, 8),
        "flags": frame[15],
        "mode": frame[43],
        "queue_depth": frame[44],
        "queue_drop_lsb": frame[45],
    }


def parse_active_mask_ack(text: str) -> tuple[int, bool] | None:
    match = re.search(r"activeMask=0x([0-9A-Fa-f]{2})(?:.*?\bwasStreaming=([01]))?", text)
    if not match:
        return None
    was_streaming = match.group(2) == "1"
    return int(match.group(1), 16), was_streaming


@dataclass
class FrameStats:
    valid_frames: int = 0
    crc_errors: int = 0
    bad_bytes: int = 0
    seq_gap_count: int = 0
    seq_gap_lost_frames: int = 0
    last_seq: int | None = None
    last_mode: int | None = None
    last_flags: int | None = None
    last_queue_depth: int = 0
    queue_drop_changes: int = 0
    last_queue_drop_lsb: int | None = None
    first_wall_time: float | None = None
    last_wall_time: float | None = None
    bytes_saved: int = 0

    def add_frame(self, parsed: dict[str, int]) -> None:
        now = time.time()
        if self.first_wall_time is None:
            self.first_wall_time = now
        self.last_wall_time = now
        self.valid_frames += 1
        self.last_mode = parsed["mode"]
        self.last_flags = parsed["flags"]
        self.last_queue_depth = parsed["queue_depth"]

        queue_drop_lsb = parsed["queue_drop_lsb"]
        if self.last_queue_drop_lsb is not None and queue_drop_lsb != self.last_queue_drop_lsb:
            self.queue_drop_changes += 1
        self.last_queue_drop_lsb = queue_drop_lsb

        seq = parsed["seq"]
        if self.last_seq is not None:
            diff = (seq - self.last_seq) & 0xFFFFFFFF
            if diff != 1:
                self.seq_gap_count += 1
                self.seq_gap_lost_frames += max(0, diff - 1)
        self.last_seq = seq

    def frame_rate(self) -> float:
        if self.first_wall_time is None or self.last_wall_time is None:
            return 0.0
        elapsed = self.last_wall_time - self.first_wall_time
        if elapsed <= 0:
            return 0.0
        return self.valid_frames / elapsed


@dataclass
class RecordingState:
    path: Path | None = None
    handle: object | None = None
    started_at: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class ActiveMaskApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Gaobo ADS1299 activeMask Control")

        self.port_var = StringVar(value="")
        self.baud_var = StringVar(value=str(BAUD_DEFAULT))
        self.status_var = StringVar(value="Disconnected")
        self.record_var = StringVar(value="Not recording")
        self.bias_mode_var = StringVar(value="CH1-CH5")
        self.mask_vars = [BooleanVar(value=index < 5) for index in range(8)]

        self.ser: serial.Serial | None = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

        self.stats = FrameStats()
        self.stats_lock = threading.Lock()
        self.recording = RecordingState()
        self.record_lock = threading.Lock()
        self.mask_history: list[dict[str, object]] = []
        self.active_mask = BIAS_MASK_5CH
        self.stream_requested = False
        self.resume_after_mask_ack = False
        self.auto_impedance_inflight = False

        self.build_ui()
        self.refresh_ports()
        self.root.after(100, self.poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        conn = ttk.Frame(main)
        conn.grid(row=0, column=0, sticky="ew")
        ttk.Label(conn, text="Port").grid(row=0, column=0, padx=(0, 4))
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=12)
        self.port_combo.grid(row=0, column=1, padx=(0, 8))
        ttk.Label(conn, text="Baud").grid(row=0, column=2, padx=(0, 4))
        ttk.Entry(conn, textvariable=self.baud_var, width=10).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(conn, text="Refresh", command=self.refresh_ports).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(conn, text="Connect", command=self.connect).grid(row=0, column=5, padx=(0, 4))
        ttk.Button(conn, text="Disconnect", command=self.disconnect).grid(row=0, column=6)

        channels = ttk.LabelFrame(main, text="BIAS_SENSP mask")
        channels.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(channels, text="Mode").grid(row=0, column=0, padx=(4, 2), pady=6)
        self.bias_mode_combo = ttk.Combobox(
            channels,
            textvariable=self.bias_mode_var,
            width=10,
            state="readonly",
            values=("CH1-CH5", "CH1-CH8"),
        )
        self.bias_mode_combo.grid(row=0, column=1, padx=(0, 8), pady=6)
        self.bias_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_bias_mode_changed())
        for index, var in enumerate(self.mask_vars):
            ttk.Checkbutton(channels, text=f"CH{index + 1}", variable=var).grid(
                row=0, column=index + 2, padx=4, pady=6
            )
        ttk.Button(channels, text="Apply Mask", command=self.apply_mask).grid(
            row=0, column=10, padx=(12, 4)
        )
        ttk.Button(channels, text="All On", command=lambda: self.set_all(True)).grid(row=0, column=11, padx=4)
        ttk.Button(channels, text="All Off", command=lambda: self.set_all(False)).grid(row=0, column=12, padx=4)

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Start Stream", command=self.start_stream).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(actions, text="Stop Stream", command=self.stop_stream).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(actions, text="Query ?", command=self.query_status).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(actions, text="Short q", command=lambda: self.send_command("q")).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(actions, text="Test t", command=lambda: self.send_command("t")).grid(row=0, column=4, padx=(0, 4))
        ttk.Button(actions, text="Bias Off o", command=lambda: self.send_command("o")).grid(row=0, column=5, padx=(0, 4))
        ttk.Button(actions, text="Initial Impedance Mask", command=self.impedance_auto_mask).grid(
            row=0, column=6, padx=(8, 4)
        )
        ttk.Button(actions, text="Record Bin", command=self.toggle_recording).grid(row=0, column=7, padx=(8, 4))

        status = ttk.LabelFrame(main, text="Status")
        status.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Label(status, textvariable=self.record_var).grid(row=1, column=0, sticky="w", padx=6, pady=4)

        log_frame = ttk.LabelFrame(main, text="Log")
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        main.rowconfigure(4, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = self._make_text(log_frame)
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def _make_text(self, parent: ttk.Frame):
        import tkinter as tk

        text = tk.Text(parent, height=14, width=100, wrap="word")
        text.configure(state="disabled")
        return text

    def refresh_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        self.port_combo["values"] = ports
        current = self.port_var.get()
        if current in ports:
            return
        if ports:
            self.port_var.set(ports[0])

    def connect(self) -> None:
        if self.ser and self.ser.is_open:
            return
        try:
            self.ser = serial.Serial(self.port_var.get(), int(self.baud_var.get()), timeout=0.1)
        except Exception as exc:
            messagebox.showerror("Serial error", str(exc))
            return
        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self.reader_loop, daemon=True)
        self.reader_thread.start()
        self.log(f"Connected {self.port_var.get()} @ {self.baud_var.get()}")

    def disconnect(self) -> None:
        self.stop_event.set()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.close_recording()
        self.stream_requested = False
        self.resume_after_mask_ack = False
        self.auto_impedance_inflight = False
        self.status_var.set("Disconnected")

    def send_command(self, command: str) -> None:
        if not self.ser or not self.ser.is_open:
            self.log("Not connected")
            return
        try:
            self.ser.write(command.encode("ascii"))
        except Exception as exc:
            self.log(f"Serial write failed: {exc}")

    def apply_mask(self) -> None:
        mask = self.mask_from_checks()
        self.active_mask = mask
        self.mask_history.append({"time": self.now_iso(), "activeMask": f"0x{mask:02X}"})
        if not self.ser or not self.ser.is_open:
            self.log(f"Prepared activeMask=0x{mask:02X}; not connected")
            return
        try:
            self.resume_after_mask_ack = self.stream_requested
            self.stream_requested = False
            self.ser.write(b"s")
            self.ser.write(f"M{mask:02X}\n".encode("ascii"))
            self.log(f"Sent activeMask=0x{mask:02X}")
        except Exception as exc:
            self.log(f"Set activeMask failed: {exc}")

    def start_stream(self) -> None:
        self.send_command("b")
        if self.ser and self.ser.is_open:
            self.stream_requested = True
        self.log("Sent start stream")

    def stop_stream(self) -> None:
        self.send_command("s")
        self.stream_requested = False
        self.resume_after_mask_ack = False
        self.log("Sent stop stream")

    def query_status(self) -> None:
        if not self.ser or not self.ser.is_open:
            self.log("Not connected")
            return
        try:
            self.ser.write(b"s?")
            self.log("Sent stop + query")
        except Exception as exc:
            self.log(f"Query failed: {exc}")

    def impedance_auto_mask(self) -> None:
        self.send_impedance_auto_mask(manual=True)

    def send_impedance_auto_mask(self, manual: bool = False) -> bool:
        if not self.ser or not self.ser.is_open:
            if manual:
                self.log("Not connected")
            return False
        if self.auto_impedance_inflight:
            if manual:
                self.log("Impedance auto mask already in progress")
            return False
        try:
            self.resume_after_mask_ack = self.stream_requested
            self.stream_requested = False
            self.auto_impedance_inflight = True
            self.ser.write(b"i")
            self.root.after(3000, self.clear_auto_impedance_timeout)
            self.log("Sent impedance/lead-off auto mask")
            return True
        except Exception as exc:
            self.auto_impedance_inflight = False
            self.log(f"Impedance auto mask failed: {exc}")
            return False

    def clear_auto_impedance_timeout(self) -> None:
        if self.auto_impedance_inflight:
            self.auto_impedance_inflight = False
            self.log("Impedance auto mask timed out")

    def set_all(self, value: bool) -> None:
        for index, var in enumerate(self.mask_vars):
            var.set(value if self.channel_allowed_in_bias(index) else False)

    def mask_from_checks(self) -> int:
        mask = 0
        for index, var in enumerate(self.mask_vars):
            if self.channel_allowed_in_bias(index) and var.get():
                mask |= 1 << index
        return mask & self.current_bias_mask_limit()

    def current_bias_mask_limit(self) -> int:
        return BIAS_MASK_8CH if self.bias_mode_var.get() == "CH1-CH8" else BIAS_MASK_5CH

    def channel_allowed_in_bias(self, index: int) -> bool:
        return index < 8 if self.bias_mode_var.get() == "CH1-CH8" else index < 5

    def on_bias_mode_changed(self) -> None:
        limit = self.current_bias_mask_limit()
        self.active_mask &= limit
        if self.active_mask == 0:
            self.active_mask = limit
        for index, var in enumerate(self.mask_vars):
            var.set(self.channel_allowed_in_bias(index) and bool(self.active_mask & (1 << index)))
        self.log(f"BIAS mode set to {self.bias_mode_var.get()} mask_limit=0x{limit:02X}")
        if limit == BIAS_MASK_8CH:
            self.log("Use the 8ch-bias firmware variant for CH1-CH8 BIAS_SENSP control")

    def reader_loop(self) -> None:
        buffer = bytearray()
        while not self.stop_event.is_set():
            try:
                if not self.ser:
                    return
                data = self.ser.read(4096)
            except Exception as exc:
                self.events.put(("log", f"Serial read stopped: {exc}"))
                return
            if data:
                buffer.extend(data)
                self.consume_buffer(buffer)
            else:
                time.sleep(0.01)

    def consume_buffer(self, buffer: bytearray) -> None:
        while buffer:
            if len(buffer) >= 2 and bytes(buffer[:2]) == SYNC:
                if len(buffer) < FRAME_LEN:
                    return
                frame = bytes(buffer[:FRAME_LEN])
                parsed = parse_frame(frame)
                if parsed is None:
                    with self.stats_lock:
                        self.stats.crc_errors += 1
                    del buffer[0]
                    continue
                self.handle_frame(frame, parsed)
                del buffer[:FRAME_LEN]
                continue

            sync_at = buffer.find(SYNC)
            newline_at = buffer.find(b"\n")
            if sync_at > 0 and (newline_at < 0 or sync_at < newline_at):
                prefix = bytes(buffer[:sync_at])
                del buffer[:sync_at]
                self.handle_text(prefix)
                continue
            if newline_at >= 0:
                line = bytes(buffer[: newline_at + 1])
                del buffer[: newline_at + 1]
                self.handle_text(line)
                continue
            if sync_at < 0:
                if len(buffer) > FRAME_LEN * 2:
                    prefix = bytes(buffer[:-1])
                    del buffer[:-1]
                    self.handle_text(prefix)
                return
            return

    def handle_frame(self, frame: bytes, parsed: dict[str, int]) -> None:
        with self.stats_lock:
            self.stats.add_frame(parsed)
        with self.record_lock:
            if self.recording.handle:
                self.recording.handle.write(frame)
                self.stats.bytes_saved += len(frame)
        if self.stats.valid_frames % 25 == 0:
            self.events.put(("stats", None))

    def handle_text(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace").strip()
        if not text:
            return
        self.events.put(("log", text))
        ack = parse_active_mask_ack(text)
        if ack:
            self.events.put(("ack_mask", ack))

    def toggle_recording(self) -> None:
        if self.recording.handle:
            self.close_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to the serial port before recording.")
            self.log("Recording not started: serial port is not connected")
            return

        default = f"ads1299_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"
        path_text = filedialog.asksaveasfilename(
            title="Save binary stream",
            initialfile=default,
            defaultextension=".bin",
            filetypes=[("ADS1299 binary", "*.bin"), ("All files", "*.*")],
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            handle = path.open("wb")
        except OSError as exc:
            messagebox.showerror("Record error", str(exc))
            self.log(f"Recording open failed: {exc}")
            return

        with self.stats_lock:
            self.stats = FrameStats()
        with self.record_lock:
            self.recording = RecordingState(
                path=path,
                handle=handle,
                started_at=self.now_iso(),
                metadata={
                    "app_version": APP_VERSION,
                    "serial_port": self.port_var.get(),
                    "baud": int(self.baud_var.get()),
                    "activeMask_at_start": f"0x{self.active_mask:02X}",
                    "mask_history": list(self.mask_history),
                    "started_at": self.now_iso(),
                    "binary_frame": "48-byte ESP32C3_ADS1299_active_mask frame",
                },
            )
        self.record_var.set(f"Recording: {path}")
        self.log(f"Recording to {path}")
        self.start_stream()

    def close_recording(self) -> None:
        if self.ser and self.ser.is_open:
            self.stop_stream()

        with self.record_lock:
            if not self.recording.handle:
                return
            path = self.recording.path
            metadata = dict(self.recording.metadata)
            self.recording.handle.flush()
            self.recording.handle.close()
            self.recording = RecordingState()
        if path:
            metadata["ended_at"] = self.now_iso()
            metadata["activeMask_at_end"] = f"0x{self.active_mask:02X}"
            metadata["mask_history"] = list(self.mask_history)
            with self.stats_lock:
                metadata["stats"] = {
                    "valid_frames": self.stats.valid_frames,
                    "crc_errors": self.stats.crc_errors,
                    "bad_bytes": self.stats.bad_bytes,
                    "seq_gap_count": self.stats.seq_gap_count,
                    "seq_gap_lost_frames": self.stats.seq_gap_lost_frames,
                    "queue_drop_changes": self.stats.queue_drop_changes,
                    "bytes_saved": self.stats.bytes_saved,
                }
            path.with_suffix(path.suffix + ".json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.log(f"Recording closed: {path}")
            if metadata["stats"]["bytes_saved"] == 0:
                self.log("Warning: recording saved 0 bytes; check stream/start, firmware, and serial port")
        self.record_var.set("Not recording")

    def poll_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(str(value))
            elif kind == "stats":
                self.update_status()
            elif kind == "ack_mask":
                mask, was_streaming = value
                self.apply_ack_mask(int(mask), bool(was_streaming))
        self.update_status()
        self.root.after(250, self.poll_events)

    def apply_ack_mask(self, mask: int, was_streaming: bool) -> None:
        self.auto_impedance_inflight = False
        self.active_mask = mask & self.current_bias_mask_limit()
        for index, var in enumerate(self.mask_vars):
            var.set(self.channel_allowed_in_bias(index) and bool(self.active_mask & (1 << index)))
        should_resume = self.resume_after_mask_ack or was_streaming
        self.resume_after_mask_ack = False
        if should_resume and self.ser and self.ser.is_open:
            try:
                self.ser.write(b"b")
                self.stream_requested = True
                self.log("ACK received; resumed stream")
            except Exception as exc:
                self.log(f"Resume stream failed: {exc}")

    def update_status(self) -> None:
        with self.stats_lock:
            self.status_var.set(
                "frames={frames} rate={rate:.1f}Hz crc={crc} gaps={gaps}/{lost} "
                "mode={mode} flags={flags} q={queue}".format(
                    frames=self.stats.valid_frames,
                    rate=self.stats.frame_rate(),
                    crc=self.stats.crc_errors,
                    gaps=self.stats.seq_gap_count,
                    lost=self.stats.seq_gap_lost_frames,
                    mode=self.stats.last_mode if self.stats.last_mode is not None else "-",
                    flags=f"0x{self.stats.last_flags:02X}" if self.stats.last_flags is not None else "-",
                    queue=self.stats.last_queue_depth,
                )
            )

    def log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        self.disconnect()
        self.root.destroy()

    @staticmethod
    def now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


def main() -> None:
    root = Tk()
    ActiveMaskApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
