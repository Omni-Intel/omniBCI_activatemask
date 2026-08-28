"""Main-window behavior grouped by responsibility."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace


class TransportControlMixin:
    def calc_lsb_uv(self, gain: Optional[float] = None) -> float:
        actual_gain = float(self.gain if gain is None else gain)
        return VREF / (actual_gain * (2**23 - 1)) * 1e6

    def channel_lsb_uv(self) -> np.ndarray:
        return VREF / (self.channel_gains.astype(float) * (2**23 - 1)) * 1e6

    def adc_saturation_limits_uv(self) -> np.ndarray:
        """Per-channel input-referred rail guard in microvolts."""
        return (
            ADC_SATURATION_FRACTION * (2**23 - 1) * np.asarray(self.channel_lsb_uv(), dtype=float)
        )

    def saturation_mask_uv(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=float)
        if values.ndim != 2 or values.shape[0] != CHANNELS:
            return np.zeros_like(values, dtype=bool)
        limits = self.adc_saturation_limits_uv()[:, None]
        return np.isfinite(values) & (np.abs(values) >= limits)

    def set_status(self, text: str):
        text = str(text)
        self.status_label.setText(text)
        if text != getattr(self, "_last_logged_status", None):
            APP_LOGGER.info("status: %s", text)
            self._last_logged_status = text
            self.log_event("gui_status", message=text[:500])

    def open_log_directory(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        APP_LOGGER.info("opening log directory: %s", LOG_DIR)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(LOG_DIR)))

    def log_event(self, event: str, level: str = "info", **fields) -> None:
        logger = getattr(self, "event_logger", None)
        if logger is None:
            return
        common = {
            "transport": str(self.active_transport or "none"),
            "streaming": bool(self.streaming),
            "packet_count": int(self.packet_count),
        }
        logger.log(event, level=level, **common, **fields)

    def action_correlation(self) -> dict:
        return {
            "last_gui_action": str(self._last_user_action_text),
            "ms_since_gui_action": round(
                max(0.0, time.monotonic() - self._last_user_action_monotonic) * 1000.0,
                3,
            ),
        }

    def selected_transport(self) -> str:
        if hasattr(self, "transport_combo"):
            return str(self.transport_combo.currentData() or "serial")
        return "serial"

    def transport_connected(self) -> bool:
        if self.active_transport == "serial":
            return bool(self.ser and self.ser.is_open)
        if self.active_transport == "ble":
            return bool(self.ble_connected)
        return False

    def transport_description(self) -> str:
        if self.active_transport == "serial" and self.ser and self.ser.is_open:
            return f"USB {self.ser.port}"
        if self.active_transport == "ble" and self.ble_connected:
            return f"BLE {self.ble_device_name or self.ble_device_address}"
        return "未连接"

    def transport_mode_changed(self):
        if self.transport_connected() or self.transport_connecting:
            return
        kind = self.selected_transport()
        self.port_combo.clear()
        if kind == "ble":
            self.serial_label.setText("蓝牙")
            self.refresh_btn.setText("扫描蓝牙")
            self.connect_btn.setText("连接蓝牙")
            self.reference_combo.setEnabled(False)
            self.apply_reference_btn.setEnabled(False)
            self.reference_combo.setToolTip("V19 固定使用 SRB1。")
        else:
            self.serial_label.setText("串口")
            self.refresh_btn.setText("扫描串口")
            self.connect_btn.setText("打开串口")
            self.reference_combo.setEnabled(False)
            self.apply_reference_btn.setEnabled(False)
            self.reference_combo.setToolTip(
                "新版本固定使用 SRB1：每通道信号接 INxP，公共参考接 SRB1。"
            )
        self._apply_transport_timing(kind)
        self.refresh_ports()

    def _apply_transport_timing(self, kind: Optional[str] = None):
        """Apply the scheduler proven for each transport instead of one compromise."""
        selected = str(kind or self.active_transport or self.selected_transport() or "serial")
        poll_ms = BLE_POLL_INTERVAL_MS if selected == "ble" else SERIAL_POLL_INTERVAL_MS
        plot_ms = BLE_PLOT_INTERVAL_MS if selected == "ble" else SERIAL_PLOT_INTERVAL_MS
        if hasattr(self, "serial_timer"):
            self.serial_timer.setInterval(int(poll_ms))
        if hasattr(self, "plot_timer"):
            self.plot_timer.setInterval(int(plot_ms))

    def refresh_ports(self):
        if self.transport_connected() or self.transport_connecting:
            return
        if self.selected_transport() == "ble":
            self.port_combo.clear()
            self.port_combo.addItem("正在扫描 BLE…", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            if not BLE_AVAILABLE or self.ble_worker is None:
                self.set_status(
                    f"BLE 不可用：请运行 install_and_run.bat 安装 bleak。{BLE_IMPORT_ERROR}"
                )
                return
            self.ble_worker.scan(5.0)
            return

        current_device = self.port_combo.currentData()
        self.port_combo.clear()
        self.port_device_map = {}
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        if not ports:
            self.port_combo.addItem("未发现串口", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.set_status("未发现串口：请插入设备后点击“扫描串口”。")
        else:
            for info in ports:
                description = (info.description or info.manufacturer or "未知设备").strip()
                label = f"{info.device} — {description}"
                self.port_combo.addItem(label, userData=info.device)
                self.port_device_map[label] = info.device
            self.port_combo.setEnabled(True)
            self.connect_btn.setEnabled(True)
            if current_device:
                index = self.port_combo.findData(current_device)
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
            if len(ports) == 1:
                self.set_status(f"发现串口 {ports[0].device}：确认设备后点击“打开串口”。")
            else:
                self.set_status(f"发现 {len(ports)} 个串口：请选择正确设备后点击“打开串口”。")

    def on_ble_scan_started(self):
        self.set_status("正在扫描 BLE，约 5 秒…")

    def on_ble_scan_finished(self, rows):
        if self.selected_transport() != "ble" or self.transport_connected():
            return
        self.port_combo.clear()
        if not rows:
            self.port_combo.addItem("未发现 BLE 设备", userData=None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.set_status("未发现 BLE：确认固件已烧录、板子已上电且未被其他软件连接。")
            return
        preferred_index = -1
        for row in rows:
            label = f"{row['name']} — {row['address']}"
            self.port_combo.addItem(label, userData=row["key"])
            if row.get("preferred") and preferred_index < 0:
                preferred_index = self.port_combo.count() - 1
        if preferred_index >= 0:
            self.port_combo.setCurrentIndex(preferred_index)
            self.set_status("已发现兼容的 OmniBCI BLE 设备，点击“连接蓝牙”。")
        else:
            self.set_status(
                "未看到已知的 OmniBCI 设备名；仍可选择设备，连接后会校验 GATT 与固件协议。"
            )
        self.port_combo.setEnabled(True)
        self.connect_btn.setEnabled(True)

    def toggle_connection(self):
        if self.transport_connected() or self.transport_connecting:
            self.disconnect_transport()
            return
        if self.selected_transport() == "ble":
            key = self.port_combo.currentData()
            if not key:
                QtWidgets.QMessageBox.warning(
                    self,
                    "BLE",
                    "请先扫描蓝牙并选择 OmniBCI-C3-SRB1-V3、OmniBCI-C3-SRB2 或兼容设备。",
                )
                return
            if self.ble_worker is None:
                QtWidgets.QMessageBox.critical(
                    self, "BLE", "缺少 bleak，请运行 install_and_run.bat。"
                )
                return
            self.transport_connecting = True
            self.transport_combo.setEnabled(False)
            self.port_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
            self.connect_btn.setText("连接中…")
            self.connect_btn.setEnabled(False)
            self.ble_worker.connect_device(str(key))
            return

        port = self.port_combo.currentData()
        if not port:
            QtWidgets.QMessageBox.warning(self, "串口", "请先点击“扫描串口”，并选择一个设备。")
            return
        try:
            self.ser = serial.Serial(
                port,
                BAUD,
                timeout=SERIAL_READER_TIMEOUT_S,
                write_timeout=0.5,
            )
            self.serial_buffer_configured = False
            self.serial_buffer_error = ""
            try:
                # Best-effort Windows driver buffer.  Unlike V15, failure is
                # visible in diagnostics instead of silently ignored.
                self.ser.set_buffer_size(rx_size=SERIAL_RX_BUFFER_BYTES, tx_size=65536)
                self.serial_buffer_configured = True
            except Exception as buffer_exc:
                self.serial_buffer_error = str(buffer_exc)
            self.serial_worker = SerialTransportWorker(self.ser)
            self.serial_worker.buffer_configured = self.serial_buffer_configured
            self.serial_worker.buffer_error = self.serial_buffer_error
            self.serial_worker.start()
            self.active_transport = "serial"
            self._apply_transport_timing("serial")
            QtWidgets.QApplication.processEvents()
            time.sleep(0.7)
            self.transport_reset_input_buffer()
            self.transport_write(b"s")
            self.connect_btn.setText("关闭串口")
            self.transport_combo.setEnabled(False)
            self.port_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
            self.apply_reference_mode()
            self.set_status(
                f"已打开 {port}，并同步 {self.reference_short_name()} 参考与通道参数。"
                "现在可以点击“开始采集”。"
            )
        except Exception as exc:
            self.ser = None
            self.active_transport = None
            self.transport_combo.setEnabled(True)
            QtWidgets.QMessageBox.critical(self, "连接失败", str(exc))

    def on_ble_connecting(self, _key: str):
        self.set_status("正在连接并订阅 DATA/STATUS 特征…")

    @staticmethod
    def ble_reference_hint_from_name(name: str):
        normalized = str(name or "").strip().upper()
        if "SRB2" in normalized:
            return REFERENCE_SRB2
        if "SRB1" in normalized:
            return REFERENCE_SRB1
        return None

    def _ble_write_channel_config(self, ch: int, reference_mode: int):
        """V19 applies one atomic SRB1-only snapshot for every channel edit."""
        return self._ble_write_bulk_config(REFERENCE_SRB1)

    def _ble_write_bulk_config(self, reference_mode: int):
        """Configure and read back all ADS1299 registers in one V1 transaction."""
        if self.ble_worker is None:
            raise RuntimeError("BLE 后台线程未就绪")
        enabled_mask = sum((1 << ch) for ch in range(CHANNELS) if self.channel_enabled[ch]) & 0xFF
        bias_mask = (
            sum(
                (1 << ch)
                for ch in range(CHANNELS)
                if self.channel_enabled[ch] and self.channel_bias[ch]
            )
            & 0xFF
        )
        payload = encode_set_config(
            self.current_mode,
            enabled_mask,
            bias_mask,
            self.impedance_mask if self.impedance_active else 0,
            self.channel_gains,
        )
        snapshot = decode_config_snapshot(
            self.ble_worker.request_blocking(MSG_SET_CONFIG, payload, timeout=4.0)
        )
        if not snapshot.verified or snapshot.enabled_mask != enabled_mask:
            raise RuntimeError("ADS1299 配置读回不一致")
        self.ble_worker.config_snapshot = snapshot
        return {
            "verified": snapshot.verified,
            "enabled_mask": snapshot.enabled_mask,
            "bias_p": snapshot.bias_p,
            "bias_n": snapshot.bias_n,
            "reference": REFERENCE_SRB1,
            "generation": snapshot.generation,
            "channel_registers": snapshot.channel_registers,
        }

    def sync_ble_configuration(self, requested_reference=None, probe_capability: bool = True):
        """V19 is fixed SRB1; connection handshake already read device state."""
        self.set_reference_mode_local(REFERENCE_SRB1)
        return REFERENCE_SRB1, False

    def apply_ble_config_snapshot(self, snapshot):
        gain_by_code = {0: 1, 1: 2, 2: 4, 3: 6, 4: 8, 5: 12, 6: 24}
        for ch, register in enumerate(snapshot.channel_registers):
            self.channel_enabled[ch] = not bool(register & 0x80)
            self.channel_gains[ch] = gain_by_code.get((register >> 4) & 0x07, 24)
            self.channel_bias[ch] = bool(snapshot.bias_p & (1 << ch))
            self.channel_srb2[ch] = False
        self.current_mode = int(snapshot.mode)
        self.set_reference_mode_local(REFERENCE_SRB1)
        self.set_bias_checks(int(snapshot.bias_p))

    def on_ble_connected(self, name: str, address: str, mtu: int, reconnected: bool):
        self.transport_connecting = False
        self.active_transport = "ble"
        self._apply_transport_timing("ble")
        self.ble_connected = True
        self.ble_device_name = name
        self.ble_device_address = address
        self.ble_peer_mtu = int(mtu)
        self.log_event(
            "ble_connected",
            device_name=str(name),
            device_address=str(address),
            mtu=int(mtu),
            reconnected=bool(reconnected),
        )
        self.ble_low_mtu_warned = False
        self.ble_protocol_warned = False
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("断开蓝牙")
        self.transport_combo.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.reference_combo.setEnabled(False)
        self.apply_reference_btn.setEnabled(False)

        # During an active recording the ESP32 keeps sampling and retains BLE
        # blocks across a radio disconnect.  Do not send s/A5/b here: doing so
        # destroys the retained session and creates a real sequence gap exactly
        # when the automatic reconnect was supposed to recover it.
        if reconnected and self.streaming:
            if self.ble_worker is not None:
                self.ble_worker.set_streaming_hint(True)
            self.set_status(
                f"BLE 已自动重连并完成 V19/V1 握手：{name}，MTU={mtu}；"
                "正在继续原可靠会话并补传断线期间数据。"
            )
            return

        try:
            self.ble_supports_srb2 = False
            self.ble_reference_profile = "srb1_fixed"
            snapshot = self.ble_worker.config_snapshot if self.ble_worker is not None else None
            if snapshot is None:
                raise RuntimeError("未收到 ADS1299 寄存器快照")
            self.apply_ble_config_snapshot(snapshot)
            self.refresh_channel_parameter_labels()
            self.mode_before_internal_short = (
                self.current_mode if self.current_mode in (0, 1, 2) else 1
            )
            self.mode_combo.setCurrentIndex(self._mode_index_from_code(self.current_mode))
            self._sync_internal_short_button(self.current_mode == 3)
            action = "已自动重连" if reconnected else "已连接"
            info = self.ble_worker.device_info if self.ble_worker is not None else None
            firmware = info.get("firmware", (19, 0, 0)) if info else (19, 0, 0)
            protocol = info.get("protocol", 1) if info else 1
            self.set_status(
                f"BLE {action}并确认设备就绪：{name}，MTU={mtu}，"
                f"固件 V{firmware[0]}.{firmware[1]}.{firmware[2]}，协议 V{protocol}，固定 SRB1。"
                + ("采集已恢复。" if reconnected and self.streaming else "点击“开始采集”。")
            )
        except Exception as exc:
            self.set_status(f"BLE 握手后初始化界面失败：{exc}")

    def on_ble_disconnected(self, reason: str, will_reconnect: bool):
        self.log_event(
            "ble_disconnected",
            level="warning",
            reason=str(reason),
            will_reconnect=bool(will_reconnect),
        )
        self.ble_connected = False
        self.transport_connecting = bool(will_reconnect)
        if will_reconnect:
            self.connect_btn.setText("自动重连中…")
            self.connect_btn.setEnabled(True)
            self.set_status(f"{reason}；后台正在自动重连。")
            return
        self.active_transport = None
        self.transport_connecting = False
        self.streaming = False
        self.close_raw_file()
        self.ble_rx_buffer.clear()
        self.ble_batch_started_monotonic = None
        if self.ble_worker is not None:
            self.ble_worker.clear_data()
        self.connect_btn.setText("连接蓝牙")
        self.connect_btn.setEnabled(True)
        self.transport_combo.setEnabled(True)
        self.port_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.set_status(reason)

    def on_ble_data(self, payload):
        if self.active_transport != "ble":
            return
        data = bytes(payload)
        if data:
            self.ble_rx_buffer.extend(data)

    def on_ble_status(self, payload):
        data = bytes(payload)
        if len(data) < 32 or data[:2] != b"\xbc\x53":
            return
        if self.ble_worker is not None:
            self.ble_worker.set_peer_status_protocol(data[2])
        flags = data[5]
        status = {
            "status_protocol": data[2],
            "phase": data[3],
            "mode": data[4],
            "flags": flags,
            "mtu": int.from_bytes(data[6:8], "little"),
            "sequence": int.from_bytes(data[8:12], "little"),
            "queue_drop": int.from_bytes(data[12:16], "little"),
            "notify_error": int.from_bytes(data[16:20], "little"),
            "command_drop": int.from_bytes(data[20:24], "little"),
            "mtu_blocked": int.from_bytes(data[24:28], "little"),
            "blocks_sent": int.from_bytes(data[28:32], "little"),
        }
        if len(data) >= 72:
            status.update(
                {
                    "reliable_stored": int.from_bytes(data[32:34], "little"),
                    "reliable_outstanding": int.from_bytes(data[34:36], "little"),
                    "reliable_highest_acked": int.from_bytes(data[36:40], "little"),
                    "reliable_next_block": int.from_bytes(data[40:44], "little"),
                    "reliable_ack_count": int.from_bytes(data[44:48], "little"),
                    "reliable_nack_count": int.from_bytes(data[48:52], "little"),
                    "reliable_retransmit": int.from_bytes(data[52:56], "little"),
                    "reliable_recovered": int.from_bytes(data[56:60], "little"),
                    "reliable_overflow": int.from_bytes(data[60:64], "little"),
                    "reliable_unknown_nack": int.from_bytes(data[64:68], "little"),
                    "reliable_protocol_error": int.from_bytes(data[68:72], "little"),
                }
            )
        if len(data) >= 76:
            status["config_generation"] = int.from_bytes(data[72:76], "little")
        if len(data) >= 96:
            status.update(
                {
                    "missed_drdy": int.from_bytes(data[76:80], "little"),
                    "late_drdy": int.from_bytes(data[80:84], "little"),
                    "mutex_busy": int.from_bytes(data[84:88], "little"),
                    "bad_status": int.from_bytes(data[88:92], "little"),
                    "max_read_us": int.from_bytes(data[92:96], "little"),
                }
            )

        previous = dict(self.ble_status)
        counter_keys = (
            "queue_drop",
            "notify_error",
            "command_drop",
            "mtu_blocked",
            "blocks_sent",
            "reliable_ack_count",
            "reliable_nack_count",
            "reliable_retransmit",
            "reliable_recovered",
            "reliable_overflow",
            "reliable_unknown_nack",
            "reliable_protocol_error",
            "missed_drdy",
            "late_drdy",
            "mutex_busy",
            "bad_status",
        )
        delta = {}
        for key in counter_keys:
            if key not in status:
                continue
            current = int(status.get(key, 0))
            if key not in previous:
                delta[key] = 0
                continue
            old = int(previous.get(key, 0))
            # Counter reset/reboot is treated as a fresh baseline, not as a huge
            # unsigned wrap. Genuine uint32 wrap is irrelevant at EEG timescales.
            delta[key] = current - old if current >= old else current
        self.ble_status_delta = delta
        self.ble_status = status
        self.log_event("ble_status", status=dict(status), delta=dict(delta))
        self.ble_peer_mtu = self.ble_status["mtu"]
        self.current_mode = int(self.ble_status["mode"])
        self._sync_internal_short_button(self.current_mode == 3)
        if data[2] == 0x04 and not self.ble_protocol_warned:
            self.ble_protocol_warned = True
            self.set_status(
                "已识别 STATUS V4 固件：启用低延迟容错与 ACK 心跳；坏块显示真实缺口。"
            )
        elif (len(data) < 76 or data[2] not in (0x04, 0x05)) and not self.ble_protocol_warned:
            self.ble_protocol_warned = True
            self.set_status("BLE STATUS 格式不匹配：需要 SRB1-only STATUS V4/V5 固件 V19。")
        if self.ble_peer_mtu >= BLE_MIN_STREAM_MTU:
            self.ble_low_mtu_warned = False
        if self.ble_peer_mtu < BLE_MIN_STREAM_MTU and not self.ble_low_mtu_warned:
            self.ble_low_mtu_warned = True
            self.set_status(
                f"BLE 已连接但 MTU={self.ble_peer_mtu}<100，固件会阻止 EEG Notify。"
                "请关闭其他蓝牙软件、重新连接或更新电脑蓝牙驱动。"
            )

    def on_ble_info(self, text: str):
        if self.active_transport == "ble" or self.transport_connecting:
            self.set_status(text)

    def on_ble_error(self, text: str):
        self.log_event("ble_error", level="error", message=str(text)[:500])
        self.transport_connecting = False
        if self.active_transport != "ble":
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("连接蓝牙")
        self.set_status(text)
        QtWidgets.QMessageBox.warning(self, "BLE", text)

    def on_ble_performance_event(self, payload):
        details = dict(payload or {})
        event_name = str(details.pop("event", "ble_performance"))
        self.log_event(
            event_name,
            level="warning",
            display_delay_ms=round(float(self.display_delay_s) * 1000.0, 3),
            display_target_ms=round(float(self.display_target_delay_samples) * 1000.0 / FS, 3),
            raw_queue_bytes=int(self.raw_writer.queued_bytes),
            **self.action_correlation(),
            **details,
        )

    def disconnect_transport(self):
        if self.active_transport == "serial":
            self.stop_stream()
            if self.serial_worker is not None:
                try:
                    self.serial_worker.stop(timeout=2.0, close_port=False)
                except Exception:
                    pass
                self.serial_worker = None
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.active_transport = None
            self.connect_btn.setText("打开串口")
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.set_status("串口已关闭。")
            return
        if self.active_transport == "ble" or self.transport_connecting:
            self.stop_stream()
            self.transport_connecting = False
            if self.ble_worker is not None:
                try:
                    self.ble_worker.disconnect_blocking()
                except Exception as exc:
                    self.set_status(f"BLE 断开异常：{exc}")
            self.ble_connected = False
            self.active_transport = None
            self.connect_btn.setText("连接蓝牙")
            self.connect_btn.setEnabled(True)
            self.transport_combo.setEnabled(True)
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.set_status("BLE 已断开。")

    def disconnect_serial(self):
        self.disconnect_transport()

    def require_transport(self) -> bool:
        if not self.transport_connected():
            kind = "蓝牙" if self.selected_transport() == "ble" else "串口"
            QtWidgets.QMessageBox.warning(self, "设备未连接", f"请先扫描并连接{kind}设备。")
            return False
        return True

    def transport_write(self, data: bytes):
        payload = bytes(data)
        self.log_event(
            "transport_command",
            command_ascii=(
                payload.decode("ascii", errors="replace")
                if all(32 <= value < 127 for value in payload)
                else ""
            ),
            command_hex=payload.hex(),
            byte_count=len(payload),
        )
        if self.active_transport == "serial":
            if not self.ser or not self.ser.is_open:
                raise RuntimeError("串口未连接")
            return self.ser.write(payload)
        if self.active_transport == "ble":
            if not self.ble_connected or self.ble_worker is None:
                raise RuntimeError("BLE 未连接")
            self.ble_worker.write_blocking(payload)
            return len(payload)
        raise RuntimeError("设备未连接")

    def transport_reset_input_buffer(self, clear_status: bool = True, reset_reliable: bool = False):
        if self.active_transport == "serial":
            if self.serial_worker is not None:
                self.serial_worker.clear_data(clear_driver=True)
            elif self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
        elif self.active_transport == "ble":
            self.ble_rx_buffer.clear()
            self.ble_batch_started_monotonic = None
            if self.ble_worker is not None:
                self.ble_worker.clear_data(reset_reliable=reset_reliable)
            if clear_status and self.ble_worker is not None:
                while True:
                    try:
                        self.ble_worker.status_queue.get_nowait()
                    except queue.Empty:
                        break
        self.last_serial_waiting_bytes = 0

    def _run_api_on_gui(self, operation: str, payload: dict) -> dict:
        """Run a control operation on the Qt thread and return its result."""

        request = {
            "operation": operation,
            "payload": dict(payload),
            "done": threading.Event(),
            "result": None,
            "error": None,
        }
        self.api_gui_request.emit(request)
        if not request["done"].wait(timeout=120.0):
            raise RuntimeError("gui_control_timeout")
        if request["error"] is not None:
            raise request["error"]
        result = request["result"]
        if not isinstance(result, dict):
            raise RuntimeError("GUI control handler returned an invalid result")
        return result

    @QtCore.Slot(object)
    def _handle_api_gui_request(self, request: dict) -> None:
        try:
            operation = request["operation"]
            payload = request["payload"]
            if operation == "stop_measurement":
                self.stop_stream(offer_export=False)
                request["result"] = {
                    "recording_id": payload["recording_id"],
                    "stopped": True,
                }
            elif operation == "export_bdf":
                request["result"] = self.export_recording_bdf(
                    Path(payload["path"]),
                    payload["markers"],
                    recording_id=payload["recording_id"],
                    recording_started_at=payload["recording_started_at"],
                    first_sequence=payload["first_sequence"],
                    overwrite=payload["overwrite"],
                )
            else:
                raise RuntimeError("unsupported_gui_control")
        except BaseException as exc:
            request["error"] = exc
        finally:
            request["done"].set()

    def _api_stop_measurement(self) -> dict:
        if not self.streaming or not self.recording_session_id:
            raise RuntimeError("not_recording")
        recording_id = self.recording_session_id
        return self._run_api_on_gui("stop_measurement", {"recording_id": recording_id})

    def _api_export_bdf(self, request: dict, markers: tuple[MarkerEvent, ...]) -> dict:
        path = request.get("path")
        overwrite = request.get("overwrite", False)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a bool")
        if self.stream_server is None:
            raise RuntimeError("stream_api_unavailable")
        state = self.stream_server.recording_snapshot()
        recording_id = state.get("recording_id")
        if not isinstance(recording_id, str) or not recording_id:
            raise RuntimeError("no_recording")
        return self._run_api_on_gui(
            "export_bdf",
            {
                "path": path,
                "overwrite": overwrite,
                "markers": tuple(markers),
                "recording_id": recording_id,
                "recording_started_at": state["recording_started_at"],
                "first_sequence": state["first_sequence"],
            },
        )

    def _recording_folder(self) -> Path:
        folder = RECORDINGS_DIR / "bin"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _recording_configuration_snapshot(self) -> dict:
        reference = "SRB2" if self.reference_is_srb2() else "SRB1"
        transport = self.transport_description() if self.transport_connected() else "disconnected"
        return {
            "sample_rate_hz": FS,
            "frame_bytes": FRAME_BYTES,
            "transport": transport,
            "ble_device_name": self.ble_device_name or "",
            "ble_device_address": self.ble_device_address or "",
            "reference": reference,
            "reference_code": int(self.reference_mode),
            "mode_code": int(self.current_mode),
            "mode_name": MODE_NAMES.get(int(self.current_mode), "UNKNOWN"),
            "global_gain": int(self.gain),
            "channel_gains": [int(value) for value in self.channel_gains.tolist()],
            "channel_enabled": [bool(value) for value in self.channel_enabled.tolist()],
            "channel_bias": [bool(value) for value in self.channel_bias.tolist()],
            "channel_srb2": [bool(value) for value in self.channel_srb2.tolist()],
            "bias_register": self.bias_register_name(),
            "bias_mask": int(self.current_bias_mask()),
            "bias_mask_hex": f"0x{self.current_bias_mask():02X}",
        }

    def make_raw_path(self) -> str:
        """Preview the next continuous recording filename."""
        now = datetime.now()
        return str(self._recording_folder() / f"{now:%m%d_%H%M}_xxxxxx.bin")
