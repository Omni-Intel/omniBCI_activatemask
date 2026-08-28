"""Main-window behavior grouped by responsibility."""

from __future__ import annotations

from .runtime import *  # noqa: F403 - shared Qt runtime namespace


class ChannelConfigMixin:
    def reference_is_srb2(self) -> bool:
        return False

    def impedance_series_default_kohm(self) -> float:
        return LEAD_OFF_SERIES_SRB1_KOHM

    def sync_impedance_series_compensation(self):
        if self.impedance_series_spin is None:
            return
        value = self.impedance_series_default_kohm()
        self.impedance_series_spin.setValue(value)
        self.impedance_series_spin.setToolTip(
            f"已按 {self.reference_short_name()} 参考自动设置为 {value:.2f} kΩ；"
            "也可按对应接口的外部短接实测值校准。"
        )

    def set_reference_mode_local(self, mode: int):
        self.reference_mode = REFERENCE_SRB1
        self.channel_srb2[:] = False
        if hasattr(self, "reference_combo"):
            index = self.reference_combo.findData(self.reference_mode)
            if index >= 0:
                self.reference_combo.blockSignals(True)
                self.reference_combo.setCurrentIndex(index)
                self.reference_combo.blockSignals(False)
        self.sync_impedance_series_compensation()
        self.refresh_channel_parameter_labels()

    def reference_short_name(self) -> str:
        return "SRB2" if self.reference_is_srb2() else "SRB1"

    def bias_register_name(self) -> str:
        if self.current_mode == 0:
            return "BIAS_SENSP+BIAS_SENSN"
        return "BIAS_SENSN" if self.reference_is_srb2() else "BIAS_SENSP"

    @staticmethod
    def _decode_config_ack_packet(
        packet: bytes,
        expected_command: int,
        expected_argument: Optional[int] = None,
    ):
        packet = bytes(packet)
        if len(packet) != 12 or packet[0] != 0xBC:
            return None
        if packet[1] != (int(expected_command) & 0xFF):
            return None
        if expected_argument is not None and packet[2] != (int(expected_argument) & 0xFF):
            return None
        checksum = 0
        for value in packet[:11]:
            checksum ^= value
        if checksum != packet[11]:
            return None
        return {
            "command": packet[1],
            "argument": packet[2],
            "channel_register": packet[3],
            "bias_p": packet[4],
            "bias_n": packet[5],
            "misc1": packet[6],
            "loff_p": packet[4],
            "loff_n": packet[5],
            "loff_config": packet[6],
            "reference": packet[7],
            "mode": packet[8],
            "verified": bool(packet[9] & 0x01),
            "enabled_mask": packet[10],
        }

    def read_config_ack(
        self,
        expected_command: int,
        timeout: float = 1.8,
        expected_argument: Optional[int] = None,
    ):
        """Read one matching ADS register ACK with GATT-read fallback.

        V16 serial ACKs are drained from the dedicated reader queue while the
        normal serial parser is temporarily paused.  This removes the race where
        a Qt timer consumed a 12-byte configuration ACK as if it were EEG.
        """
        if not self.transport_connected():
            return None
        deadline = time.perf_counter() + timeout
        buffer = bytearray()
        marker = bytes((0xBC, expected_command & 0xFF))
        wanted_argument = None if expected_argument is None else int(expected_argument) & 0xFF
        next_direct_read = time.perf_counter() + 0.22
        serial_mode = self.active_transport == "serial"
        if serial_mode:
            self.serial_control_read_active = True
        try:
            while time.perf_counter() < deadline:
                chunk = b""
                if self.active_transport == "serial":
                    if self.serial_worker is not None:
                        chunk = self.serial_worker.drain_data(4096)
                elif self.active_transport == "ble" and self.ble_worker is not None:
                    try:
                        chunk = self.ble_worker.status_queue.get(timeout=0.02)
                    except queue.Empty:
                        chunk = b""
                    now = time.perf_counter()
                    if not chunk and now >= next_direct_read:
                        next_direct_read = now + 0.30
                        try:
                            direct = self.ble_worker.read_status_blocking(timeout=0.65)
                        except Exception:
                            direct = b""
                        parsed = self._decode_config_ack_packet(
                            direct, expected_command, wanted_argument
                        )
                        if parsed is not None:
                            return parsed

                if chunk:
                    buffer.extend(chunk)

                while True:
                    start = buffer.find(marker)
                    if start < 0:
                        if len(buffer) > 1:
                            del buffer[:-1]
                        break
                    if len(buffer) < start + 12:
                        if start:
                            del buffer[:start]
                        break
                    packet = bytes(buffer[start : start + 12])
                    del buffer[: start + 12]
                    parsed = self._decode_config_ack_packet(
                        packet, expected_command, wanted_argument
                    )
                    if parsed is not None:
                        return parsed

                if len(buffer) > 256:
                    del buffer[:-32]
                QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents, 5)
                time.sleep(0.004)
            return None
        finally:
            if serial_mode:
                self.serial_control_read_active = False

    def _load_channel_names(self) -> List[str]:
        defaults = [f"CH{index}" for index in range(1, CHANNELS + 1)]
        saved = self.app_settings.value("channel_names", defaults)
        if isinstance(saved, str):
            saved = [part.strip() for part in saved.split(",")]
        try:
            return self.validate_channel_names(list(saved))
        except (TypeError, ValueError):
            return defaults

    def _save_channel_names(self) -> None:
        self.app_settings.setValue("channel_names", list(self.channel_names))
        self.app_settings.sync()

    @staticmethod
    def validate_channel_names(names) -> List[str]:
        names = [str(name).strip() for name in names]
        if len(names) != CHANNELS:
            raise ValueError(f"必须提供 {CHANNELS} 个通道名称。")
        for name in names:
            if not name:
                raise ValueError("通道名称不能为空。")
            if len(name) > 16:
                raise ValueError(f"通道名称“{name}”超过 16 个字符。")
            if any(ord(char) < 32 or ord(char) > 126 for char in name):
                raise ValueError(
                    f"通道名称“{name}”包含非 ASCII 字符；请使用 Fp1、Cz、ECG 等英文标签。"
                )
        folded = [name.casefold() for name in names]
        if len(set(folded)) != len(folded):
            raise ValueError("通道名称不能重复。")
        return names

    def open_channel_naming_dialog(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("通道命名")
        dialog.setModal(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        hint = QtWidgets.QLabel(
            "名称将用于波形、PSD、单通道视图和导出元数据，并自动保存。\n"
            "为兼容 BDF/FIF，建议使用 Fp1、Fp2、C3、C4、Cz、ECG 等短英文标签。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QtWidgets.QFormLayout()
        editors = []
        for ch, current_name in enumerate(self.channel_names):
            editor = QtWidgets.QLineEdit(current_name)
            editor.setMaxLength(16)
            editor.selectAll()
            form.addRow(f"CH{ch + 1} (INP{ch + 1})", editor)
            editors.append(editor)
        layout.addLayout(form)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(buttons)

        def accept_names():
            try:
                names = self.validate_channel_names([editor.text() for editor in editors])
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(dialog, "通道名称无效", str(exc))
                return
            self.channel_names = names
            self._save_channel_names()
            self.refresh_channel_parameter_labels()
            self.set_status("通道名称已保存，并同步到显示与后续导出文件。")
            dialog.accept()

        buttons.accepted.connect(accept_names)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()

    def refresh_channel_parameter_labels(self):
        """Keep the per-channel hardware state visible without opening a dialog."""
        if not hasattr(self, "channel_buttons"):
            return
        for ch, button in enumerate(self.channel_buttons):
            name = self.channel_names[ch]
            enabled = bool(self.channel_enabled[ch])
            bias = "BIAS✓" if self.channel_bias[ch] else "BIAS—"
            power = "ON" if enabled else "OFF"
            reference = "SRB1全局"
            button.setText(f"{name}  {power}  ×{int(self.channel_gains[ch])}\n{bias}  {reference}")
            icon = QtGui.QPixmap(11, 11)
            icon.fill(QtGui.QColor("#56bd31" if enabled else "#8b969e"))
            button.setIcon(QtGui.QIcon(icon))
            button.setToolTip(
                f"{name} (INP{ch + 1}): {'启用' if enabled else '禁用'}, "
                f"PGA ×{int(self.channel_gains[ch])}, "
                f"{'参与' if self.channel_bias[ch] else '不参与'} {self.bias_register_name()}；"
                + "EEG 模式使用全局 SRB1"
            )
            if hasattr(self, "channel_plots"):
                self.channel_plots[ch].setLabel("left", name, units="uV")
            if hasattr(self, "channel_combo"):
                self.channel_combo.setItemText(ch, name)
            if hasattr(self, "single_channel_combo"):
                self.single_channel_combo.setItemText(ch, name)
            if hasattr(self, "differential_b_combo"):
                self.differential_b_combo.setItemText(ch, name)
            if hasattr(self, "bias_checks") and ch < len(self.bias_checks):
                self.bias_checks[ch].setText(name)

    def validated_channel_name(self, ch, name):
        names = list(self.channel_names)
        names[ch] = name
        return self.validate_channel_names(names)[ch]

    def open_channel_settings(self, ch):
        self._select_channel(ch)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"CH{ch + 1} 通道设置")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
        channel_name = QtWidgets.QLineEdit(self.channel_names[ch])
        channel_name.setMaxLength(16)
        form.addRow("通道名称", channel_name)
        summary = QtWidgets.QLabel()
        summary.setStyleSheet(
            "background:#fff0e6;color:#b83c00;border:1px solid #ffb589;padding:8px;font-weight:bold;"
        )
        form.addRow(summary)
        enabled = QtWidgets.QCheckBox("启用该通道")
        enabled.setChecked(bool(self.channel_enabled[ch]))
        form.addRow("通道电源", enabled)
        gain = QtWidgets.QComboBox()
        gain.addItems([str(value) for value in VALID_GAINS])
        gain.setCurrentText(str(int(self.channel_gains[ch])))
        form.addRow("PGA 增益", gain)
        bias = QtWidgets.QCheckBox(f"加入 {self.bias_register_name()} 共模反馈计算")
        bias.setChecked(bool(self.channel_bias[ch]))
        form.addRow("BIAS", bias)
        note_text = (
            "V19 固定使用 SRB1：测量电极接 INxP，公共参考接 SRB1，"
            "MISC1.SRB1 在 EEG 模式中全局开启。"
        )
        note = QtWidgets.QLabel(note_text)
        note.setWordWrap(True)
        note.setStyleSheet("color:#5d6870;")
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        form.addRow(buttons)

        def update_summary(*_args):
            summary.setText(
                f"CH{ch + 1}  |  {'ON' if enabled.isChecked() else 'OFF'}  |  "
                f"PGA ×{gain.currentText()}  |  BIAS {'YES' if bias.isChecked() else 'NO'}  |  "
                "SRB1 GLOBAL"
            )

        enabled.toggled.connect(update_summary)
        gain.currentTextChanged.connect(update_summary)
        bias.toggled.connect(update_summary)
        update_summary()
        selected_name = self.channel_names[ch]

        def accept_settings():
            nonlocal selected_name
            try:
                selected_name = self.validated_channel_name(ch, channel_name.text())
            except ValueError as exc:
                QtWidgets.QMessageBox.warning(dialog, "通道名称无效", str(exc))
                return
            dialog.accept()

        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(accept_settings)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.apply_channel_settings(
            ch,
            enabled.isChecked(),
            int(gain.currentText()),
            bias.isChecked(),
            False,
            selected_name,
        )

    def apply_channel_settings(self, ch, enabled, gain, bias, srb2=None, channel_name=None):
        if channel_name is None:
            channel_name = self.channel_names[ch]
        channel_name = self.validated_channel_name(ch, channel_name)
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        effective_srb2 = False
        flags = (
            (0x01 if enabled else 0)
            | (0x02 if bias and enabled else 0)
            | (0x04 if effective_srb2 else 0)
        )
        was_streaming = bool(self.streaming)
        ack = None
        old_state = (
            bool(self.channel_enabled[ch]),
            int(self.channel_gains[ch]),
            bool(self.channel_bias[ch]),
            bool(self.channel_srb2[ch]),
        )
        try:
            if self.transport_connected() and self.offline_uv is None:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    if self.ble_worker is not None and self.active_transport == "ble":
                        self.ble_worker.set_streaming_hint(False)
                    time.sleep(0.12)
                # BLE configuration ACKs use STATUS, so never clear DATA here.
                # Clearing the reliable-delivery queue discarded valid tail EEG
                # and was the reason a channel toggle could create a fake host gap.
                if self.active_transport == "serial":
                    self.transport_reset_input_buffer()
                elif self.ble_worker is not None:
                    while True:
                        try:
                            self.ble_worker.status_queue.get_nowait()
                        except queue.Empty:
                            break
                if self.active_transport == "ble":
                    self.channel_enabled[ch] = bool(enabled)
                    self.channel_gains[ch] = int(gain)
                    self.channel_bias[ch] = bool(bias and enabled)
                    ack = self._ble_write_bulk_config(REFERENCE_SRB1)
                else:
                    self.transport_write(bytes([0xA7, ch & 0x07, gain & 0xFF, flags]))
                    ack = self.read_config_ack(0xA7, expected_argument=ch & 0x07)
                    if ack is None or ack["argument"] != (ch & 0x07) or not ack["verified"]:
                        raise RuntimeError(f"ADS1299 配置校验失败：CH{ch + 1}")
            self.channel_enabled[ch] = bool(enabled)
            self.channel_gains[ch] = int(gain)
            self.channel_bias[ch] = bool(bias and enabled)
            self.channel_srb2[ch] = False
            self.channel_names[ch] = channel_name
            self._save_channel_names()
            self.set_bias_checks(sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i]))
            self.refresh_channel_parameter_labels()
            # Start a fresh display/filter epoch for the new hardware channel
            # configuration, but do not discard already-received BLE DATA from
            # the transport queues. The raw BIN session remains continuous.
            self.ring.clear()
            self.reset_processing_state()
            self.last_seq = None
            self.first_seq = None
            self.first_clock = None
            readback = (
                f"；ADS读回 CHnSET=0x{(ack.get('channel_registers') or [ack.get('channel_register', 0xFF)] * 8)[ch]:02X}, "
                f"P=0x{ack['bias_p']:02X}, N=0x{ack['bias_n']:02X}"
                if ack is not None
                else "；仅更新离线显示参数"
            )
            self.set_status(
                f"已确认 CH{ch + 1}: {'ON' if enabled else 'OFF'}, PGA×{gain}, "
                f"{self.bias_register_name()}={'YES' if bias and enabled else 'NO'}, "
                + "SRB1=GLOBAL"
                + readback
            )
        except Exception as exc:
            (
                self.channel_enabled[ch],
                self.channel_gains[ch],
                self.channel_bias[ch],
                self.channel_srb2[ch],
            ) = old_state
            self.refresh_channel_parameter_labels()
            QtWidgets.QMessageBox.critical(self, "通道设置失败", str(exc))
        finally:
            if was_streaming and self.transport_connected():
                try:
                    # Do not discard retained BLE tail blocks before restart.
                    if self.active_transport == "serial":
                        self.transport_reset_input_buffer()
                    self.transport_write(b"b")
                    self.streaming = True
                    if self.ble_worker is not None and self.active_transport == "ble":
                        self.ble_worker.set_streaming_hint(True)
                    self.last_seq = None
                    self.first_seq = None
                    self.first_clock = None
                except Exception:
                    self.streaming = False

    def apply_reference_mode(self):
        if self.impedance_active:
            self.stop_impedance_detection(silent=True)
        was_streaming = bool(self.streaming)
        try:
            if (
                self.active_transport == "ble"
                and self.transport_connected()
                and self.offline_uv is None
            ):
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    time.sleep(0.08)
                self.sync_ble_configuration()
                self.ble_supports_srb2 = False
                self.ble_reference_profile = "srb1_fixed"
                self.set_reference_mode_local(REFERENCE_SRB1)
                self.set_bias_checks(sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i]))
                self.ring.clear()
                self.filtered_ring.clear()
                self.reset_processing_state()
                if was_streaming:
                    self.transport_write(b"b")
                    self.streaming = True
                self.set_status(
                    "BLE 已同步固定 SRB1：信号接 INxP，公共参考接 SRB1，BIAS 使用 SENSP。"
                )
                return
            if self.transport_connected() and self.offline_uv is None:
                if was_streaming:
                    self.transport_write(b"s")
                    self.streaming = False
                    time.sleep(0.08)
                self.transport_reset_input_buffer()
                self.transport_write(bytes([0xA8, REFERENCE_SRB1]))
                time.sleep(0.12)

                # Re-send all channels with the SRB2 flag permanently clear.
                payload = bytearray()
                for ch in range(CHANNELS):
                    enabled = bool(self.channel_enabled[ch])
                    flags = (0x01 if enabled else 0) | (
                        0x02 if self.channel_bias[ch] and enabled else 0
                    )
                    payload.extend((0xA7, ch, int(self.channel_gains[ch]), flags))
                self.transport_write(payload)
                time.sleep(0.25)
                # A7-capable firmware returns one readback ACK per channel.
                # This bulk synchronization does not need to expose all eight
                # replies, so discard them before normal polling/streaming.
                self.transport_reset_input_buffer()
                if was_streaming:
                    self.transport_write(b"b")
                    self.streaming = True

            self.set_reference_mode_local(REFERENCE_SRB1)
            self.set_bias_checks(sum((1 << i) for i in range(CHANNELS) if self.channel_bias[i]))
            self.ring.clear()
            self.filtered_ring.clear()
            self.reset_processing_state()
            self.set_status(
                "参考已固定为 SRB1：信号接 INxP，参考接 SRB1，"
                "BIAS 使用 BIAS_SENSP；原始极性为 INxP-SRB1。"
            )
        except Exception as exc:
            if was_streaming and self.transport_connected() and not self.streaming:
                try:
                    self.transport_reset_input_buffer()
                    self.transport_write(b"b")
                    self.streaming = True
                except Exception:
                    pass
            QtWidgets.QMessageBox.critical(self, "参考模式切换失败", str(exc))
