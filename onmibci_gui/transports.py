"""Runtime component extracted from the legacy GUI."""

from __future__ import annotations

from .common import *  # noqa: F403
from .frames import *  # noqa: F403


def expand_compact_ble_payload(payload: bytes, frame_count: int) -> bytes:
    """Expand BLE V2 compact records back to the standard 48-byte BIN format."""
    if len(payload) != int(frame_count) * BLE_COMPACT_FRAME_BYTES:
        raise ValueError("BLE compact payload length mismatch")
    out = bytearray()
    for index in range(int(frame_count)):
        record = payload[index * BLE_COMPACT_FRAME_BYTES : (index + 1) * BLE_COMPACT_FRAME_BYTES]
        sequence = record[0:4]
        timestamp = record[4:8]
        ads_raw = record[8:35]
        flags = record[35]
        frame = bytearray(FRAME_BYTES)
        frame[0:4] = bytes((SYNC1, SYNC2, 1, 1))
        frame[4:8] = sequence
        frame[8:12] = timestamp
        frame[12:15] = ads_raw[0:3]
        frame[15] = flags
        frame[16:40] = ads_raw[3:27]
        frame[40:42] = b"\x00\x00"
        frame[42] = 2 if (flags & 0x04) else 0
        if flags & 0x08:
            mode = 4
        elif flags & 0x10:
            mode = 3
        elif flags & 0x40:
            mode = 0
        elif flags & 0x20:
            mode = 1
        else:
            mode = 2
        frame[43] = mode
        frame[44] = 0
        frame[45] = 0
        struct.pack_into("<H", frame, 46, crc16_ccitt(frame[:46]))
        out.extend(frame)
    return bytes(out)


class SerialTransportWorker:
    """Continuously drain pyserial outside the Qt event loop.

    The worker owns *reads* and an in-memory byte deque.  GUI/command writes may
    still use the same full-duplex serial handle.  reset_input_buffer() is
    serialized with reads so configuration ACKs and EEG bytes cannot race an OS
    buffer reset.  No Qt signal is emitted per read.
    """

    def __init__(self, ser_handle):
        self.ser = ser_handle
        self._chunks = deque()
        self._data_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="OmniBCI-SerialReader", daemon=True)
        self._queued_bytes = 0
        self._peak_queued_bytes = 0
        self._read_calls = 0
        self._read_errors = 0
        self._overflow_events = 0
        self._last_gap_s = 0.0
        self._max_gap_s = 0.0
        self._last_rx_monotonic = None
        self.buffer_configured = False
        self.buffer_error = ""

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.ser is None or not self.ser.is_open:
                    break
                with self._read_lock:
                    waiting = int(self.ser.in_waiting)
                    want = min(
                        SERIAL_READER_MAX_READ_BYTES,
                        max(1, waiting),
                    )
                    payload = self.ser.read(want)
                if not payload:
                    continue
                now = time.monotonic()
                if self._last_rx_monotonic is not None:
                    gap = max(0.0, now - self._last_rx_monotonic)
                    self._last_gap_s = gap
                    self._max_gap_s = max(self._max_gap_s, gap)
                self._last_rx_monotonic = now
                with self._data_lock:
                    self._chunks.append(bytes(payload))
                    self._queued_bytes += len(payload)
                    self._peak_queued_bytes = max(self._peak_queued_bytes, self._queued_bytes)
                    if self._queued_bytes > SERIAL_HOST_MAX_QUEUE_BYTES:
                        # Do not silently discard EEG.  Count pressure and keep
                        # the queue lossless; diagnostics will expose the host
                        # processing stall while RAM acts as the absorber.
                        self._overflow_events += 1
                self._read_calls += 1
            except Exception:
                self._read_errors += 1
                if self._stop.wait(0.02):
                    break

    def queued_data_bytes(self) -> int:
        with self._data_lock:
            return int(self._queued_bytes)

    def drain_data(self, max_bytes: int = SERIAL_MAX_PROCESS_BYTES) -> bytes:
        max_bytes = max(1, int(max_bytes))
        parts = []
        total = 0
        with self._data_lock:
            while self._chunks and total < max_bytes:
                chunk = self._chunks[0]
                remaining = max_bytes - total
                if len(chunk) <= remaining:
                    parts.append(self._chunks.popleft())
                    total += len(chunk)
                else:
                    parts.append(chunk[:remaining])
                    self._chunks[0] = chunk[remaining:]
                    total += remaining
                    break
            self._queued_bytes = max(0, self._queued_bytes - total)
        return b"".join(parts)

    def clear_data(self, clear_driver: bool = True):
        with self._data_lock:
            self._chunks.clear()
            self._queued_bytes = 0
        if clear_driver and self.ser is not None and self.ser.is_open:
            try:
                with self._read_lock:
                    self.ser.reset_input_buffer()
            except Exception:
                pass

    def metrics(self) -> dict:
        with self._data_lock:
            queued = int(self._queued_bytes)
            peak = int(self._peak_queued_bytes)
        return {
            "queued_bytes": queued,
            "peak_queued_bytes": peak,
            "read_calls": int(self._read_calls),
            "read_errors": int(self._read_errors),
            "overflow_events": int(self._overflow_events),
            "last_gap_s": float(self._last_gap_s),
            "max_gap_s": float(self._max_gap_s),
            "buffer_configured": bool(self.buffer_configured),
            "buffer_error": str(self.buffer_error),
        }

    def stop(self, timeout: float = 2.0, close_port: bool = False):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.1, float(timeout)))
        if close_port and self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass


class BleTransportWorker(QtCore.QThread):
    """Own a Bleak asyncio loop outside the Qt GUI thread.

    DATA notifications are forwarded as opaque byte chunks. STATUS ACK packets
    are also placed into a thread-safe queue so synchronous configuration
    dialogs can wait for hardware readback without blocking the BLE event loop.
    """

    scan_started = QtCore.Signal()
    scan_finished = QtCore.Signal(object)
    connecting = QtCore.Signal(str)
    connected = QtCore.Signal(str, str, int, bool)
    disconnected = QtCore.Signal(str, bool)
    data_received = QtCore.Signal(object)
    status_received = QtCore.Signal(object)
    info = QtCore.Signal(str)
    error = QtCore.Signal(str)
    performance_event = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=64)
        self._response_waiters = {}
        self._next_request_id = 1
        self.device_info = None
        self.config_snapshot = None
        # DATA notifications stay in the BLE worker thread.  A lock-protected
        # deque is drained by the GUI timer, avoiding one queued Qt event per
        # notification (a common cause of visible Windows GUI stalls).
        self._data_chunks = deque()
        self._data_lock = threading.Lock()
        self._queued_data_bytes = 0
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._devices = {}
        self._desired_key: Optional[str] = None
        self._manual_disconnect = False
        self._reconnect_task = None
        self._status_poll_task = None
        self._reliable_watchdog_task = None
        self._connect_lock = None
        self._closing = False
        self._streaming_hint = False
        self._streaming_hint_started_monotonic = 0.0
        self._timing_lock = threading.Lock()
        self._last_notify_monotonic: Optional[float] = None
        self._notify_gap_last_s = 0.0
        self._notify_gap_max_s = 0.0
        self._notify_burst_max_bytes = 0
        self._notify_gap_over_100ms = 0
        self._notify_gap_events = deque(maxlen=32)
        self._notify_gap_samples = deque(maxlen=BLE_ADAPTIVE_GAP_SAMPLES)
        self._notify_gap_ewma_s = 0.0
        self._adaptive_profile = "learning"

        # Bleak's DATA notification callback must do almost no Python work.
        # It only timestamps + copies bytes into this lossless host queue, then
        # returns to the Windows BLE stack immediately. Reliable decoding, CRC,
        # compact-frame expansion and ACK/NACK decisions run in a separate
        # decoder thread, so a busy Qt paint/PSD turn cannot block the notify
        # callback long enough to fill the MCU retention ring.
        self._notify_decode_queue = queue.Queue()
        self._notify_decode_stop = threading.Event()
        self._notify_decode_sentinel = object()
        self._notify_decode_lock = threading.Lock()
        self._notify_decode_queued_bytes = 0
        self._notify_decode_peak_bytes = 0
        self._notify_decode_errors = 0
        self._notify_decoder_thread: Optional[threading.Thread] = None

        # Reliable BLE block reassembly/ACK state. DATA is decoded by the
        # dedicated decoder thread; the GUI only ever receives ordered standard
        # 48-byte ADS frames.
        self._reliable_lock = threading.Lock()
        self._reliable_rx_buf = bytearray()
        self._reliable_session_id = None
        self._reliable_accept_any_session = True
        self._reliable_expected_block = 0
        self._reliable_pending = {}
        self._reliable_last_ack_sent = 0xFFFFFFFF
        self._reliable_last_ack_time = 0.0
        self._reliable_last_nack = None
        self._reliable_blocks_received = 0
        self._reliable_blocks_delivered = 0
        self._reliable_block_crc_bad = 0
        self._reliable_sync_drop = 0
        self._reliable_duplicates = 0
        self._reliable_out_of_order = 0
        self._reliable_retransmitted_received = 0
        self._reliable_gap_markers = 0
        self._reliable_ack_sent = 0
        self._reliable_nack_sent = 0
        self._reliable_control_errors = 0
        # V18 hotfix: ACK/NACK writes are generated off the decoder thread and
        # can become obsolete before Windows actually transmits them. Suppress
        # those stale controls instead of letting an already-repaired NACK hit
        # the MCU after the corresponding cumulative ACK has released the block.
        self._reliable_stale_nack_suppressed = 0
        self._reliable_stale_ack_suppressed = 0
        self._reliable_last_ack_wire = 0xFFFFFFFF
        self._reliable_max_pending = 0
        self._reliable_gap_sequence = None
        self._reliable_gap_first_seen = 0.0
        self._reliable_watchdog_nacks = 0
        self._reliable_forced_skips = 0
        self._reliable_last_delivery_monotonic = 0.0
        self._watchdog_reconnects = 0
        self._watchdog_last_reconnect_monotonic = 0.0
        self._peer_status_protocol = 0
        self._legacy_v4_ack_retries = 0
        self._legacy_v4_ack_probes = 0
        self._legacy_v4_reliable_resets = 0
        self._legacy_v4_fast_forward_events = 0
        self._legacy_v4_fast_forward_blocks = 0
        self._legacy_v4_last_retry_monotonic = 0.0
        self._legacy_v4_last_probe_monotonic = 0.0
        self._legacy_v4_last_reset_monotonic = 0.0
        self._gatt_write_lock = None
        self._control_pending_lock = threading.Lock()
        self._pending_ack_packet: Optional[bytes] = None
        self._pending_nack_packet: Optional[bytes] = None
        self._control_drain_scheduled = False

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._connect_lock = asyncio.Lock()
        self._gatt_write_lock = asyncio.Lock()
        self._notify_decode_stop.clear()
        self._notify_decoder_thread = threading.Thread(
            target=self._notify_decoder_loop,
            name="OmniBCI-BLEDecoder",
            daemon=True,
        )
        self._notify_decoder_thread.start()
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._notify_decode_stop.set()
            try:
                self._notify_decode_queue.put_nowait(self._notify_decode_sentinel)
            except Exception:
                pass
            decoder = self._notify_decoder_thread
            if decoder is not None and decoder.is_alive():
                decoder.join(timeout=2.0)
            pending = list(asyncio.all_tasks(self._loop))
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def _submit(self, coroutine):
        if not BLE_AVAILABLE:
            raise RuntimeError(f"未安装 Bleak：{BLE_IMPORT_ERROR}")
        if not self._ready.wait(3.0) or self._loop is None:
            raise RuntimeError("BLE 后台线程未就绪")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def scan(self, timeout: float = 5.0):
        try:
            self._submit(self._scan(float(timeout)))
        except Exception as exc:
            self.error.emit(str(exc))

    async def _scan(self, timeout: float):
        self.scan_started.emit()
        try:
            devices = await BleakScanner.discover(timeout=max(1.0, timeout))
            rows = []
            self._devices = {}
            for device in devices:
                address = str(getattr(device, "address", "") or "")
                if not address:
                    continue
                name = str(getattr(device, "name", "") or "").strip() or "未命名 BLE 设备"
                self._devices[address] = device
                rows.append(
                    {
                        "key": address,
                        "name": name,
                        "address": address,
                        "preferred": name in BLE_DEVICE_NAMES,
                    }
                )
            rows.sort(
                key=lambda item: (not item["preferred"], item["name"].lower(), item["address"])
            )
            self.scan_finished.emit(rows)
        except Exception as exc:
            self.error.emit(f"BLE 扫描失败：{exc}")
            self.scan_finished.emit([])

    def connect_device(self, key: str):
        self._desired_key = str(key)
        self._manual_disconnect = False
        try:
            future = self._submit(self._connect_to_device(str(key), reconnected=False))
            future.add_done_callback(self._initial_connect_done)
        except Exception as exc:
            self.error.emit(str(exc))

    def _initial_connect_done(self, future):
        try:
            future.result()
        except Exception as exc:
            self.error.emit(f"BLE 连接失败：{exc}")

    async def _resolve_device(self, key: str):
        device = self._devices.get(key)
        if device is not None:
            return device
        finder = getattr(BleakScanner, "find_device_by_address", None)
        if finder is None:
            return None
        return await finder(key, timeout=10.0)

    async def _connect_to_device(self, key: str, reconnected: bool):
        if self._closing:
            return
        async with self._connect_lock:
            if self._desired_key != key:
                return
            if self._client is not None and bool(getattr(self._client, "is_connected", False)):
                return
            if not reconnected:
                self.connecting.emit(key)
            device = await self._resolve_device(key)
            if self._desired_key != key:
                return
            if device is None:
                raise RuntimeError("找不到所选 BLE 设备，请重新扫描。")

            client = BleakClient(
                device,
                disconnected_callback=self._on_disconnected,
                timeout=15.0,
            )
            try:
                await client.connect()
                if self._desired_key != key:
                    await client.disconnect()
                    return
                services = client.services
                missing = [
                    uuid
                    for uuid in (
                        BLE_DATA_UUID,
                        BLE_CONTROL_UUID,
                        BLE_STATUS_UUID,
                        BLE_RESPONSE_UUID,
                    )
                    if services.get_characteristic(uuid) is None
                ]
                if missing:
                    raise RuntimeError("设备缺少 OmniBCI BLE 特征，可能选错设备或固件版本不匹配。")
                self._client = client
                if not reconnected:
                    self.reset_reliable_state(reset_metrics=True)
                else:
                    with self._reliable_lock:
                        self._reliable_accept_any_session = True
                await client.start_notify(BLE_DATA_UUID, self._on_data)
                await client.start_notify(BLE_STATUS_UUID, self._on_status)
                await client.start_notify(BLE_RESPONSE_UUID, self._on_response)
                # Give the resubscribed link a fresh stall deadline. Otherwise
                # the watchdog can immediately disconnect again based on the
                # timestamp from before the radio interruption.
                self._last_notify_monotonic = time.monotonic()
                hello = await self._request(MSG_HELLO, timeout=2.5)
                if len(hello) < 11 or hello[0] != 0:
                    raise RuntimeError("固件握手响应无效")
                firmware_version = hello[1]
                protocol_version = hello[4]
                if firmware_version != BLE_FIRMWARE_VERSION:
                    raise RuntimeError(
                        f"固件版本不兼容：需要 V{BLE_FIRMWARE_VERSION}，设备为 V{firmware_version}"
                    )
                if protocol_version != BLE_DEVICE_PROTOCOL_VERSION:
                    raise RuntimeError(
                        f"通信协议不兼容：需要 V{BLE_DEVICE_PROTOCOL_VERSION}，设备为 V{protocol_version}"
                    )
                self.device_info = {
                    "firmware": (hello[1], hello[2], hello[3]),
                    "protocol": protocol_version,
                    "capabilities": int.from_bytes(hello[5:7], "little"),
                    "boot_id": int.from_bytes(hello[7:11], "little"),
                }
                if not (reconnected and self._streaming_hint):
                    self.config_snapshot = decode_config_snapshot(
                        await self._request(MSG_GET_CONFIG, timeout=3.0)
                    )
                status = bytes(await client.read_gatt_char(BLE_STATUS_UUID))
                self._publish_status(status)
                mtu = int(getattr(client, "mtu_size", 23) or 23)
                name = str(getattr(device, "name", "") or BLE_DEVICE_NAME)
                address = str(getattr(device, "address", key) or key)
                self._manual_disconnect = False
                self.connected.emit(name, address, mtu, bool(reconnected))
                if self._status_poll_task is not None:
                    self._status_poll_task.cancel()
                self._status_poll_task = asyncio.create_task(self._status_poll_loop(client))
                if self._reliable_watchdog_task is not None:
                    self._reliable_watchdog_task.cancel()
                self._reliable_watchdog_task = asyncio.create_task(
                    self._reliable_watchdog_loop(client)
                )
            except Exception:
                if self._client is client:
                    self._client = None
                try:
                    if bool(getattr(client, "is_connected", False)):
                        await client.disconnect()
                except Exception:
                    pass
                raise

    @staticmethod
    def _make_reliable_control_packet(
        command_type: int, session_id: int, seq_a: int, seq_b: int = 0
    ) -> bytes:
        body = (
            BLE_CTRL_MAGIC
            + bytes((BLE_CTRL_VERSION, int(command_type) & 0xFF))
            + struct.pack(
                "<III",
                int(session_id) & 0xFFFFFFFF,
                int(seq_a) & 0xFFFFFFFF,
                int(seq_b) & 0xFFFFFFFF,
            )
        )
        return body + struct.pack("<H", crc16_ccitt(body))

    async def _send_reliable_control(self, packet: bytes, kind: str):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            return
        write_started = time.monotonic()
        try:
            lock = self._gatt_write_lock
            if lock is None:
                return
            async with lock:
                # Revalidate immediately before the GATT write. A missing block
                # can be repaired while an older NACK is waiting behind another
                # Windows GATT operation. Sending that obsolete NACK after the
                # cumulative ACK makes the firmware report an "unknown NACK"
                # even though no data was actually lost.
                packet_to_send = bytes(packet)
                is_ack = kind in ("ack", "ack_retry")
                is_nack = kind == "nack"
                if (
                    (is_ack or is_nack)
                    and len(packet_to_send) == BLE_CTRL_PACKET_BYTES
                    and packet_to_send[:2] == BLE_CTRL_MAGIC
                    and packet_to_send[2] == BLE_CTRL_VERSION
                ):
                    session_id, seq_a, seq_b = struct.unpack_from("<III", packet_to_send, 4)
                    with self._reliable_lock:
                        current_session = self._reliable_session_id
                        if current_session is None or int(session_id) != int(current_session):
                            if is_nack:
                                self._reliable_stale_nack_suppressed += 1
                            else:
                                self._reliable_stale_ack_suppressed += 1
                            return

                        if is_nack:
                            expected = int(self._reliable_expected_block)
                            pending_keys = sorted(self._reliable_pending)
                            has_hole = bool(pending_keys and pending_keys[0] > expected)
                            # If the decoder has already advanced past this range
                            # (or the pending hole disappeared), the repair request
                            # is stale and must not reach the C3.
                            if not has_hole or expected > int(seq_b):
                                self._reliable_stale_nack_suppressed += 1
                                return
                            if expected != int(seq_a):
                                seq_a = expected
                                seq_b = max(expected, int(seq_b))
                                packet_to_send = self._make_reliable_control_packet(
                                    BLE_CTRL_NACK_RANGE, current_session, seq_a, seq_b
                                )
                        else:
                            ack_seq = int(seq_a)
                            last_wire = int(self._reliable_last_ack_wire)
                            if kind == "ack" and last_wire != 0xFFFFFFFF and ack_seq <= last_wire:
                                self._reliable_stale_ack_suppressed += 1
                                return
                            # A V4 retry deliberately repeats the latest
                            # cumulative ACK. Never let an older queued retry
                            # move the firmware acknowledgement backwards.
                            if kind != "ack" and last_wire != 0xFFFFFFFF and ack_seq < last_wire:
                                ack_seq = last_wire
                                packet_to_send = self._make_reliable_control_packet(
                                    BLE_CTRL_ACK, current_session, ack_seq
                                )

                # ACK/NACK are CRC-protected, idempotent controls and stay out of
                # the Windows response queue. The legacy V4 stack can also hold
                # RESET write-with-response for seconds when resources are
                # exhausted, so only that compatibility profile sends RESET
                # without a response. V5 keeps the stronger acknowledged RESET.
                response_required = kind == "reset" and self._peer_status_protocol != 0x04
                await client.write_gatt_char(
                    BLE_CONTROL_UUID,
                    packet_to_send,
                    response=response_required,
                )
            with self._reliable_lock:
                if kind in ("ack", "ack_retry"):
                    self._reliable_ack_sent += 1
                    if kind == "ack_retry":
                        self._legacy_v4_ack_retries += 1
                    if len(packet_to_send) >= 12:
                        self._reliable_last_ack_wire = struct.unpack_from("<I", packet_to_send, 8)[
                            0
                        ]
                elif kind == "nack":
                    self._reliable_nack_sent += 1
            write_ms = (time.monotonic() - write_started) * 1000.0
            if write_ms >= 100.0:
                self.performance_event.emit(
                    {
                        "event": "gatt_control_slow",
                        "kind": str(kind),
                        "write_ms": round(write_ms, 3),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._reliable_lock:
                self._reliable_control_errors += 1
            self.performance_event.emit(
                {
                    "event": "gatt_control_error",
                    "kind": str(kind),
                    "message": str(exc)[:300],
                }
            )

    def _schedule_reliable_control(self, packet: bytes, kind: str):
        """Coalesce ACK/NACK writes into one bounded async drain task.

        Creating one asyncio task per decoded block allowed hundreds of stale
        controls to queue behind a slow Windows GATT write. The resulting NACK
        storm delayed cumulative ACKs until the firmware retention ring filled.
        """
        loop = self._loop
        if loop is None or self._closing:
            return
        payload = bytes(packet)
        control_kind = str(kind)
        spawn = False
        with self._control_pending_lock:
            if control_kind == "ack":
                current = self._pending_ack_packet
                if current is None:
                    self._pending_ack_packet = payload
                elif len(current) >= 12 and len(payload) >= 12:
                    current_session = struct.unpack_from("<I", current, 4)[0]
                    new_session = struct.unpack_from("<I", payload, 4)[0]
                    current_seq = struct.unpack_from("<I", current, 8)[0]
                    new_seq = struct.unpack_from("<I", payload, 8)[0]
                    if new_session != current_session or new_seq >= current_seq:
                        self._pending_ack_packet = payload
                else:
                    self._pending_ack_packet = payload
            elif control_kind == "nack":
                self._pending_nack_packet = payload
            else:
                # Reliable decoder currently generates only ACK/NACK here.
                self._pending_nack_packet = payload
            if not self._control_drain_scheduled:
                self._control_drain_scheduled = True
                spawn = True

        if not spawn:
            return

        def _spawn():
            if self._closing:
                with self._control_pending_lock:
                    self._control_drain_scheduled = False
                return
            try:
                asyncio.create_task(self._drain_reliable_controls())
            except RuntimeError:
                with self._control_pending_lock:
                    self._control_drain_scheduled = False

        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            with self._control_pending_lock:
                self._control_drain_scheduled = False

    async def _drain_reliable_controls(self):
        while not self._closing:
            packet = None
            kind = ""
            with self._control_pending_lock:
                # Release acknowledged firmware storage first. If a real hole
                # remains, the following NACK is revalidated against current
                # decoder state immediately before it reaches the wire.
                if self._pending_ack_packet is not None:
                    packet = self._pending_ack_packet
                    self._pending_ack_packet = None
                    kind = "ack"
                elif self._pending_nack_packet is not None:
                    packet = self._pending_nack_packet
                    self._pending_nack_packet = None
                    kind = "nack"
                else:
                    self._control_drain_scheduled = False
                    return
            await self._send_reliable_control(packet, kind)

        with self._control_pending_lock:
            self._control_drain_scheduled = False

    def _enqueue_notify_for_decode(self, payload: bytes):
        payload = bytes(payload)
        if not payload:
            return
        with self._notify_decode_lock:
            self._notify_decode_queued_bytes += len(payload)
            self._notify_decode_peak_bytes = max(
                self._notify_decode_peak_bytes, self._notify_decode_queued_bytes
            )
        self._notify_decode_queue.put(payload)

    def _clear_notify_decode_queue(self):
        removed = 0
        while True:
            try:
                item = self._notify_decode_queue.get_nowait()
            except queue.Empty:
                break
            if item is self._notify_decode_sentinel:
                continue
            try:
                removed += len(item)
            except Exception:
                pass
        with self._notify_decode_lock:
            self._notify_decode_queued_bytes = max(0, self._notify_decode_queued_bytes - removed)

    def _notify_decoder_loop(self):
        while not self._notify_decode_stop.is_set():
            try:
                item = self._notify_decode_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is self._notify_decode_sentinel:
                return
            payload = bytes(item)
            with self._notify_decode_lock:
                self._notify_decode_queued_bytes = max(
                    0, self._notify_decode_queued_bytes - len(payload)
                )
            try:
                with self._reliable_lock:
                    ordered_payloads, control_packets = self._decode_reliable_bytes_locked(payload)
                if ordered_payloads:
                    joined = b"".join(ordered_payloads)
                    with self._data_lock:
                        self._data_chunks.append(joined)
                        self._queued_data_bytes += len(joined)
                for packet, kind in control_packets:
                    self._schedule_reliable_control(packet, kind)
            except Exception:
                with self._notify_decode_lock:
                    self._notify_decode_errors += 1

    def reset_reliable_state(self, reset_metrics: bool = True):
        with self._reliable_lock:
            self._reliable_rx_buf.clear()
            self._reliable_session_id = None
            self._reliable_accept_any_session = True
            self._reliable_expected_block = 0
            self._reliable_pending.clear()
            self._reliable_last_ack_sent = 0xFFFFFFFF
            self._reliable_last_ack_time = 0.0
            self._reliable_last_nack = None
            self._reliable_last_ack_wire = 0xFFFFFFFF
            self._reliable_gap_sequence = None
            self._reliable_gap_first_seen = 0.0
            self._reliable_last_delivery_monotonic = time.monotonic()
            if reset_metrics:
                self._reliable_blocks_received = 0
                self._reliable_blocks_delivered = 0
                self._reliable_block_crc_bad = 0
                self._reliable_sync_drop = 0
                self._reliable_duplicates = 0
                self._reliable_out_of_order = 0
                self._reliable_retransmitted_received = 0
                self._reliable_gap_markers = 0
                self._reliable_ack_sent = 0
                self._reliable_nack_sent = 0
                self._reliable_control_errors = 0
                self._reliable_stale_nack_suppressed = 0
                self._reliable_stale_ack_suppressed = 0
                self._reliable_max_pending = 0
                self._reliable_watchdog_nacks = 0
                self._reliable_forced_skips = 0
                self._watchdog_reconnects = 0
                self._legacy_v4_ack_retries = 0
                self._legacy_v4_ack_probes = 0
                self._legacy_v4_reliable_resets = 0
                self._legacy_v4_fast_forward_events = 0
                self._legacy_v4_fast_forward_blocks = 0
            self._legacy_v4_last_retry_monotonic = 0.0
            self._legacy_v4_last_probe_monotonic = 0.0
            self._legacy_v4_last_reset_monotonic = 0.0
        with self._control_pending_lock:
            self._pending_ack_packet = None
            self._pending_nack_packet = None

    def reliable_metrics(self):
        with self._reliable_lock:
            return {
                "blocks_received": int(self._reliable_blocks_received),
                "blocks_delivered": int(self._reliable_blocks_delivered),
                "block_crc_bad": int(self._reliable_block_crc_bad),
                "sync_drop": int(self._reliable_sync_drop),
                "duplicates": int(self._reliable_duplicates),
                "out_of_order": int(self._reliable_out_of_order),
                "retransmitted_received": int(self._reliable_retransmitted_received),
                "gap_markers": int(self._reliable_gap_markers),
                "ack_sent": int(self._reliable_ack_sent),
                "nack_sent": int(self._reliable_nack_sent),
                "control_errors": int(self._reliable_control_errors),
                "stale_nack_suppressed": int(self._reliable_stale_nack_suppressed),
                "stale_ack_suppressed": int(self._reliable_stale_ack_suppressed),
                "pending_blocks": int(len(self._reliable_pending)),
                "max_pending": int(self._reliable_max_pending),
                "expected_block": int(self._reliable_expected_block),
                "watchdog_nacks": int(self._reliable_watchdog_nacks),
                "forced_skips": int(self._reliable_forced_skips),
                "watchdog_reconnects": int(self._watchdog_reconnects),
                "status_protocol": int(self._peer_status_protocol),
                "legacy_v4_ack_retries": int(self._legacy_v4_ack_retries),
                "legacy_v4_ack_probes": int(self._legacy_v4_ack_probes),
                "legacy_v4_reliable_resets": int(self._legacy_v4_reliable_resets),
                "legacy_v4_fast_forward_events": int(self._legacy_v4_fast_forward_events),
                "legacy_v4_fast_forward_blocks": int(self._legacy_v4_fast_forward_blocks),
                "session_id": None
                if self._reliable_session_id is None
                else int(self._reliable_session_id),
                "decode_queued_bytes": int(self._notify_decode_queued_bytes),
                "decode_peak_bytes": int(self._notify_decode_peak_bytes),
                "decode_errors": int(self._notify_decode_errors),
            }

    @staticmethod
    def _percentile(values, q: float) -> float:
        values = sorted(float(v) for v in values if np.isfinite(v) and v >= 0.0)
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        pos = (len(values) - 1) * float(np.clip(q, 0.0, 1.0))
        lo = int(pos)
        hi = min(len(values) - 1, lo + 1)
        frac = pos - lo
        return values[lo] * (1.0 - frac) + values[hi] * frac

    def adaptive_timing(self) -> dict:
        now = time.monotonic()
        with self._timing_lock:
            gaps = list(self._notify_gap_samples)
            ewma = float(self._notify_gap_ewma_s)
            # Long Windows delivery pauses are deliberately excluded from the
            # ACK/NACK learner, but the display still needs to remember them so
            # the next pause can be absorbed by a larger playback reserve.
            recent_peak = max(
                (
                    float(gap)
                    for event_time, gap in self._notify_gap_events
                    if now - float(event_time) <= 90.0
                ),
                default=0.0,
            )
        p95 = self._percentile(gaps, 0.95)
        p99 = self._percentile(gaps, 0.99)
        # Before enough samples are learned, retain V15-like behavior but avoid
        # a 100 ms NACK loop that can overwhelm a slow Windows adapter.
        learned = len(gaps) >= 12
        base = max(0.024, p95, ewma * 1.20)
        # ACK is a tiny write-without-response and must stay prompt even when
        # Windows batches DATA notifications. Stretching ACK to 350 ms made the
        # ESP32 retain too many otherwise healthy blocks during long recordings.
        legacy_v4 = int(self._peer_status_protocol) == 0x04
        ack_interval = (
            BLE_V4_ACK_MAX_INTERVAL_S if legacy_v4 else BLE_RELIABLE_ACK_MAX_INTERVAL_S
        )
        nack_repeat = min(BLE_ADAPTIVE_NACK_MAX_S, max(BLE_RELIABLE_NACK_REPEAT_S, base * 1.6))
        hole_reconnect = min(
            BLE_ADAPTIVE_HOLE_RECONNECT_MAX_S,
            max(3.0, 1.0 + base * 10.0),
        )
        stall_reconnect = min(
            BLE_ADAPTIVE_STALL_RECONNECT_MAX_S,
            max(4.0, 1.5 + base * 14.0),
        )
        hole_timeout = min(
            BLE_ADAPTIVE_HOLE_TIMEOUT_MAX_S,
            max(12.0, hole_reconnect * 2.5),
        )
        reconnect_cooldown = min(15.0, max(6.0, hole_reconnect * 1.5))
        if not learned:
            profile = "learning"
        elif p95 < 0.08:
            profile = "fast"
        elif p95 < 0.18:
            profile = "normal"
        else:
            profile = "batched"
        self._adaptive_profile = profile
        ack_every = BLE_V4_ACK_EVERY_BLOCKS if legacy_v4 else BLE_RELIABLE_ACK_EVERY_BLOCKS
        return {
            "profile": "legacy-v4-rescue" if legacy_v4 else profile,
            "samples": len(gaps),
            "p95_s": float(p95),
            "p99_s": float(p99),
            "recent_peak_s": float(recent_peak),
            "ewma_s": float(ewma),
            "ack_interval_s": float(ack_interval),
            "ack_every_blocks": int(ack_every),
            "nack_repeat_s": float(nack_repeat),
            "hole_reconnect_s": float(hole_reconnect),
            "stall_reconnect_s": float(stall_reconnect),
            "hole_timeout_s": float(hole_timeout),
            "reconnect_cooldown_s": float(reconnect_cooldown),
        }

    def _decode_reliable_bytes_locked(self, incoming: bytes):
        """Return ordered original ADS payloads plus ACK/NACK control packets."""
        if incoming:
            self._reliable_rx_buf.extend(incoming)
        ordered_payloads = []
        control_packets = []
        magic = BLE_BLOCK_MAGIC

        while len(self._reliable_rx_buf) >= 2:
            idx = self._reliable_rx_buf.find(magic)
            if idx < 0:
                keep = 1 if self._reliable_rx_buf[-1:] == magic[:1] else 0
                drop = len(self._reliable_rx_buf) - keep
                if drop > 0:
                    self._reliable_sync_drop += drop
                    del self._reliable_rx_buf[:drop]
                break
            if idx > 0:
                self._reliable_sync_drop += idx
                del self._reliable_rx_buf[:idx]
            if len(self._reliable_rx_buf) < BLE_BLOCK_HEADER_BYTES:
                break

            version = self._reliable_rx_buf[2]
            flags = self._reliable_rx_buf[3]
            frame_count = self._reliable_rx_buf[16]
            payload_len = struct.unpack_from("<H", self._reliable_rx_buf, 18)[0]
            gap_marker = bool(flags & 0x04)
            normal_v1 = (
                version == BLE_BLOCK_VERSION_V1
                and 1 <= frame_count <= BLE_V1_FRAMES_PER_BLOCK
                and payload_len == frame_count * FRAME_BYTES
            )
            compact_v2 = (
                version == BLE_BLOCK_VERSION_V2
                and 1 <= frame_count <= BLE_V2_FRAMES_PER_BLOCK
                and payload_len == frame_count * BLE_COMPACT_FRAME_BYTES
            )
            valid_shape = payload_len <= BLE_BLOCK_MAX_PAYLOAD_BYTES and (
                (gap_marker and frame_count == 0 and payload_len == 0) or normal_v1 or compact_v2
            )
            if not valid_shape:
                self._reliable_sync_drop += 1
                del self._reliable_rx_buf[0]
                continue

            total = BLE_BLOCK_HEADER_BYTES + payload_len + BLE_BLOCK_CRC_BYTES
            if len(self._reliable_rx_buf) < total:
                break
            packet = bytes(self._reliable_rx_buf[:total])
            rx_crc = struct.unpack_from("<H", packet, total - 2)[0]
            calc_crc = crc16_ccitt(packet[:-2])
            if rx_crc != calc_crc:
                self._reliable_block_crc_bad += 1
                del self._reliable_rx_buf[0]
                continue
            del self._reliable_rx_buf[:total]

            session_id = struct.unpack_from("<I", packet, 4)[0]
            block_seq = struct.unpack_from("<I", packet, 8)[0]
            payload = packet[BLE_BLOCK_HEADER_BYTES : BLE_BLOCK_HEADER_BYTES + payload_len]
            if not gap_marker and version == BLE_BLOCK_VERSION_V2:
                try:
                    payload = expand_compact_ble_payload(payload, frame_count)
                except Exception:
                    self._reliable_sync_drop += payload_len
                    continue

            if self._reliable_session_id is None:
                self._reliable_session_id = session_id
                self._reliable_accept_any_session = False
            elif session_id != self._reliable_session_id:
                # A reconnect may follow either a short radio interruption or
                # a complete C3 reboot. Accept the first session seen after a
                # reconnect; during a stable connection, only a newer session
                # may replace the current recording.
                if self._reliable_accept_any_session or session_id > self._reliable_session_id:
                    self._reliable_session_id = session_id
                    self._reliable_expected_block = 0
                    self._reliable_pending.clear()
                    self._reliable_last_ack_sent = 0xFFFFFFFF
                    self._reliable_last_ack_time = 0.0
                    self._reliable_last_nack = None
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                    self._reliable_accept_any_session = False
                else:
                    self._reliable_duplicates += 1
                    continue
            else:
                self._reliable_accept_any_session = False
            self._reliable_blocks_received += 1
            if flags & 0x01:
                self._reliable_retransmitted_received += 1

            expected = self._reliable_expected_block
            legacy_v4 = int(self._peer_status_protocol) == 0x04
            if legacy_v4 and block_seq > expected:
                # V4 marks a congested notify as sent even when Bluedroid
                # rejected it. Waiting for or NACKing that phantom block creates
                # a retransmission storm and freezes the visible stream. Treat
                # DATA V4 as a loss-tolerant live feed: advance immediately and
                # let the ADS sample sequence expose the exact missing interval.
                skipped = int(block_seq - expected)
                self._reliable_forced_skips += skipped
                self._reliable_gap_markers += skipped
                self._legacy_v4_fast_forward_events += 1
                self._legacy_v4_fast_forward_blocks += skipped
                self._reliable_expected_block = int(block_seq)
                self._reliable_pending.clear()
                self._reliable_gap_sequence = None
                self._reliable_gap_first_seen = 0.0
                self._reliable_last_nack = None
                expected = int(block_seq)
            if block_seq < expected:
                self._reliable_duplicates += 1
                now = time.monotonic()
                if (
                    expected > 0
                    and (now - self._reliable_last_ack_time)
                    >= self.adaptive_timing()["ack_interval_s"]
                ):
                    ack_seq = expected - 1
                    control_packets.append(
                        (
                            self._make_reliable_control_packet(
                                BLE_CTRL_ACK, self._reliable_session_id or 0, ack_seq
                            ),
                            "ack",
                        )
                    )
                    self._reliable_last_ack_sent = ack_seq
                    self._reliable_last_ack_time = now
                continue

            if block_seq not in self._reliable_pending:
                if block_seq > expected:
                    self._reliable_out_of_order += 1
                self._reliable_pending[block_seq] = (payload, flags)
                self._reliable_max_pending = max(
                    self._reliable_max_pending, len(self._reliable_pending)
                )

            if block_seq > expected:
                if self._reliable_gap_sequence != expected:
                    self._reliable_gap_sequence = expected
                    self._reliable_gap_first_seen = time.monotonic()
                first_missing = expected
                last_missing = min(block_seq - 1, expected + 255)
                now = time.monotonic()
                last_nack = self._reliable_last_nack
                should_send_nack = (
                    last_nack is None
                    or last_nack[0] != first_missing
                    or (now - last_nack[2]) >= self.adaptive_timing()["nack_repeat_s"]
                )
                if should_send_nack:
                    # The first out-of-order block already defines the complete
                    # missing prefix. Later blocks behind the same hole must not
                    # generate one extra GATT write each.
                    if last_nack is not None and last_nack[0] == first_missing:
                        last_missing = last_nack[1]
                    control_packets.append(
                        (
                            self._make_reliable_control_packet(
                                BLE_CTRL_NACK_RANGE,
                                self._reliable_session_id or 0,
                                first_missing,
                                last_missing,
                            ),
                            "nack",
                        )
                    )
                    self._reliable_last_nack = (first_missing, last_missing, now)

            delivered_now = 0
            while self._reliable_expected_block in self._reliable_pending:
                seq = self._reliable_expected_block
                block_payload, block_flags = self._reliable_pending.pop(seq)
                if block_flags & 0x04:
                    self._reliable_gap_markers += 1
                elif block_payload:
                    ordered_payloads.append(block_payload)
                    self._reliable_blocks_delivered += 1
                self._reliable_expected_block += 1
                delivered_now += 1

            if delivered_now:
                self._reliable_last_delivery_monotonic = time.monotonic()
                if self._reliable_expected_block in self._reliable_pending:
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                elif self._reliable_pending:
                    next_pending = min(self._reliable_pending)
                    if next_pending > self._reliable_expected_block:
                        if self._reliable_gap_sequence != self._reliable_expected_block:
                            self._reliable_gap_sequence = self._reliable_expected_block
                            self._reliable_gap_first_seen = time.monotonic()
                    else:
                        self._reliable_gap_sequence = None
                        self._reliable_gap_first_seen = 0.0
                else:
                    self._reliable_gap_sequence = None
                    self._reliable_gap_first_seen = 0.0
                highest = self._reliable_expected_block - 1
                now = time.monotonic()
                ack_distance = (
                    highest + 1
                    if self._reliable_last_ack_sent == 0xFFFFFFFF
                    else highest - self._reliable_last_ack_sent
                )
                if (
                    ack_distance >= self.adaptive_timing()["ack_every_blocks"]
                    or (now - self._reliable_last_ack_time)
                    >= self.adaptive_timing()["ack_interval_s"]
                    or gap_marker
                ):
                    control_packets.append(
                        (
                            self._make_reliable_control_packet(
                                BLE_CTRL_ACK, self._reliable_session_id or 0, highest
                            ),
                            "ack",
                        )
                    )
                    self._reliable_last_ack_sent = highest
                    self._reliable_last_ack_time = now
                    self._reliable_last_nack = None

        return ordered_payloads, control_packets

    def _on_data(self, _characteristic, data):
        payload = bytes(data)
        if not payload:
            return
        now = time.monotonic()
        with self._timing_lock:
            if self._last_notify_monotonic is not None:
                gap = max(0.0, now - self._last_notify_monotonic)
                self._notify_gap_last_s = gap
                self._notify_gap_max_s = max(self._notify_gap_max_s, gap)
                if 0.0 < gap <= BLE_ADAPTIVE_LEARN_MAX_GAP_S:
                    self._notify_gap_samples.append(gap)
                    if self._notify_gap_ewma_s <= 0.0:
                        self._notify_gap_ewma_s = gap
                    else:
                        self._notify_gap_ewma_s = 0.90 * self._notify_gap_ewma_s + 0.10 * gap
                if gap >= DISPLAY_JITTER_LONG_GAP_S:
                    self._notify_gap_over_100ms += 1
                    self._notify_gap_events.append((now, gap))
                    self.performance_event.emit(
                        {
                            "event": "ble_notify_gap",
                            "gap_ms": round(gap * 1000.0, 3),
                            "payload_bytes": len(payload),
                            "decode_queued_bytes": int(self._notify_decode_queued_bytes),
                        }
                    )
            self._last_notify_monotonic = now
            self._notify_burst_max_bytes = max(self._notify_burst_max_bytes, len(payload))

        # Return to Bleak/Windows immediately. The decoder thread performs all
        # CRC/reassembly/compact expansion and generates ACK/NACK independently
        # of Qt painting, PSD, channel dialogs and disk activity.
        self._enqueue_notify_for_decode(payload)

    async def _reliable_watchdog_loop(self, client):
        """Repair holes without intentionally restarting a healthy BLE session."""
        try:
            while (
                not self._closing
                and client is self._client
                and bool(getattr(client, "is_connected", False))
            ):
                await asyncio.sleep(BLE_RELIABLE_WATCHDOG_INTERVAL_S)
                now = time.monotonic()
                payloads = []
                controls = []
                with self._reliable_lock:
                    expected = int(self._reliable_expected_block)
                    pending_keys = sorted(self._reliable_pending)
                    has_hole = bool(pending_keys and pending_keys[0] > expected)
                    legacy_v4 = int(self._peer_status_protocol) == 0x04
                    if has_hole:
                        if self._reliable_gap_sequence != expected:
                            self._reliable_gap_sequence = expected
                            self._reliable_gap_first_seen = now
                        age = max(0.0, now - self._reliable_gap_first_seen)
                        first_available = int(pending_keys[0])
                        last_pending = min(pending_keys[-1] - 1, expected + 63)
                        last_nack = self._reliable_last_nack
                        if (
                            last_nack is None
                            or last_nack[0] != expected
                            or (now - last_nack[2]) >= self.adaptive_timing()["nack_repeat_s"]
                        ):
                            controls.append(
                                (
                                    self._make_reliable_control_packet(
                                        BLE_CTRL_NACK_RANGE,
                                        self._reliable_session_id or 0,
                                        expected,
                                        max(expected, last_pending),
                                    ),
                                    "nack",
                                )
                            )
                            self._reliable_last_nack = (expected, max(expected, last_pending), now)
                            self._reliable_watchdog_nacks += 1

                        # Long-run policy: never let one unrecoverable block hold
                        # every newer block until the ESP32 ring overflows. After
                        # a bounded repair window (or high pending pressure), jump
                        # directly to the first block we really have. ADS sequence
                        # numbers preserve the exact missing samples as a timeline
                        # gap; acquisition itself keeps running.
                        if (
                            age >= BLE_RELIABLE_HOLE_FAILOPEN_S
                            or len(self._reliable_pending) >= BLE_RELIABLE_FORCE_SKIP_PENDING
                        ):
                            skipped = max(1, first_available - expected)
                            self._reliable_expected_block = first_available
                            self._reliable_forced_skips += skipped
                            self._reliable_gap_markers += skipped
                            self.performance_event.emit(
                                {
                                    "event": "reliable_forced_skip",
                                    "skipped_blocks": int(skipped),
                                    "hole_age_ms": round(age * 1000.0, 3),
                                    "pending_blocks": int(len(self._reliable_pending)),
                                    "expected_block": int(expected),
                                    "first_available_block": int(first_available),
                                }
                            )
                            self._reliable_gap_sequence = None
                            self._reliable_gap_first_seen = 0.0
                            while self._reliable_expected_block in self._reliable_pending:
                                seq = self._reliable_expected_block
                                block_payload, block_flags = self._reliable_pending.pop(seq)
                                if block_flags & 0x04:
                                    self._reliable_gap_markers += 1
                                elif block_payload:
                                    payloads.append(block_payload)
                                    self._reliable_blocks_delivered += 1
                                self._reliable_expected_block += 1
                            highest = self._reliable_expected_block - 1
                            controls.append(
                                (
                                    self._make_reliable_control_packet(
                                        BLE_CTRL_ACK,
                                        self._reliable_session_id or 0,
                                        highest,
                                    ),
                                    "ack",
                                )
                            )
                            self._reliable_last_ack_sent = highest
                            self._reliable_last_ack_time = now
                            self._reliable_last_nack = None
                            self._reliable_last_delivery_monotonic = now

                    # STATUS V4 firmware may fill its 16-block window and then
                    # wait forever when the last write-without-response ACK is
                    # lost. No new DATA means the normal decoder cannot create
                    # another ACK, so the watchdog must repeat it proactively.
                    stream_age_anchor = max(
                        float(self._reliable_last_delivery_monotonic),
                        float(self._streaming_hint_started_monotonic),
                    )
                    quiet_age = max(0.0, now - stream_age_anchor)
                    can_ack = (
                        legacy_v4
                        and self._streaming_hint
                        and self._reliable_session_id is not None
                        and expected > 0
                        and not has_hole
                    )
                    if (
                        can_ack
                        and quiet_age >= BLE_V4_ACK_RETRY_IDLE_S
                        and (
                            self._legacy_v4_last_retry_monotonic <= 0.0
                            or now - self._legacy_v4_last_retry_monotonic
                            >= BLE_V4_ACK_RETRY_INTERVAL_S
                        )
                    ):
                        controls.append(
                            (
                                self._make_reliable_control_packet(
                                    BLE_CTRL_ACK,
                                    self._reliable_session_id or 0,
                                    expected - 1,
                                ),
                                "ack_retry",
                            )
                        )
                        self._legacy_v4_last_retry_monotonic = now

                    # V18 deliberately does not call client.disconnect() merely
                    # because DATA is temporarily quiet. Real disconnections still
                    # enter _on_disconnected() and use the normal reconnect loop.
                    # This prevents long sleep captures from looking like the app
                    # restarted itself after a transient Windows scheduling stall.

                if payloads:
                    joined = b"".join(payloads)
                    with self._data_lock:
                        self._data_chunks.append(joined)
                        self._queued_data_bytes += len(joined)
                for packet, kind in controls:
                    await self._send_reliable_control(packet, kind)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.info.emit(f"BLE 看门狗异常：{exc}")

    def set_streaming_hint(self, active: bool):
        self._streaming_hint = bool(active)
        self._streaming_hint_started_monotonic = time.monotonic() if self._streaming_hint else 0.0

    def set_peer_status_protocol(self, protocol: int):
        """Select the transport profile advertised by STATUS."""
        self._peer_status_protocol = int(protocol) & 0xFF

    def timing_metrics(self) -> Tuple[float, float, int, int]:
        with self._timing_lock:
            return (
                float(self._notify_gap_last_s),
                float(self._notify_gap_max_s),
                int(self._notify_burst_max_bytes),
                int(self._notify_gap_over_100ms),
            )

    def recent_gap_events(self):
        with self._timing_lock:
            return list(self._notify_gap_events)

    def reset_timing_metrics(self):
        with self._timing_lock:
            self._last_notify_monotonic = None
            self._notify_gap_last_s = 0.0
            self._notify_gap_max_s = 0.0
            self._notify_burst_max_bytes = 0
            self._notify_gap_over_100ms = 0
            self._notify_gap_events.clear()
            self._notify_gap_samples.clear()
            self._notify_gap_ewma_s = 0.0
            self._adaptive_profile = "learning"

    def queued_data_bytes(self) -> int:
        with self._data_lock:
            return int(self._queued_data_bytes)

    def drain_data(self, max_bytes: int = 131072) -> bytes:
        """Return up to max_bytes without posting per-notify Qt events."""
        limit = max(1, int(max_bytes))
        parts = []
        taken = 0
        with self._data_lock:
            while self._data_chunks and taken < limit:
                chunk = self._data_chunks.popleft()
                room = limit - taken
                if len(chunk) <= room:
                    parts.append(chunk)
                    taken += len(chunk)
                else:
                    parts.append(chunk[:room])
                    self._data_chunks.appendleft(chunk[room:])
                    taken += room
                    break
            self._queued_data_bytes = max(0, self._queued_data_bytes - taken)
        return b"".join(parts)

    def clear_data(self, reset_reliable: bool = False):
        """Clear GUI delivery bytes without silently discarding BLE protocol state.

        Reliable block state is preserved by default.  Resetting it during a
        mode change or automatic reconnect can turn already-retained blocks into
        apparent frame loss.  A fresh recording explicitly requests a reset.
        """
        with self._data_lock:
            self._data_chunks.clear()
            self._queued_data_bytes = 0
        if reset_reliable:
            self._clear_notify_decode_queue()
            self.reset_reliable_state(reset_metrics=True)

    def _publish_status(self, payload: bytes):
        payload = bytes(payload)
        if len(payload) == 12 and payload[:1] == b"\xbc":
            try:
                self.status_queue.put_nowait(payload)
            except queue.Full:
                try:
                    self.status_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.status_queue.put_nowait(payload)
                except queue.Full:
                    pass
        self.status_received.emit(payload)

    def _on_status(self, _characteristic, data):
        self._publish_status(bytes(data))

    def _on_response(self, _characteristic, data):
        try:
            packet = decode_packet(bytes(data))
        except ProtocolError as exc:
            self.info.emit(f"BLE RESPONSE 无效：{exc}")
            return
        waiter = self._response_waiters.pop(packet.request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(packet)

    async def _request(self, message_type: int, payload: bytes = b"", timeout: float = 3.0):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        request_id = self._next_request_id
        self._next_request_id = 1 if request_id >= 0xFFFF else request_id + 1
        waiter = self._loop.create_future()
        self._response_waiters[request_id] = waiter
        try:
            await client.write_gatt_char(
                BLE_CONTROL_UUID,
                encode_packet(message_type, request_id, payload),
                response=True,
            )
            packet = await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            self._response_waiters.pop(request_id, None)
        if packet.message_type != (MSG_RESPONSE | message_type):
            raise RuntimeError(f"BLE 响应类型不匹配：0x{packet.message_type:02X}")
        return packet.payload

    def request_blocking(self, message_type: int, payload: bytes = b"", timeout: float = 3.0):
        future = self._submit(self._request(message_type, bytes(payload), timeout))
        return bytes(future.result(timeout=max(0.5, timeout + 0.5)))

    async def _status_poll_loop(self, client):
        try:
            while (
                not self._closing
                and client is self._client
                and bool(getattr(client, "is_connected", False))
            ):
                # STATUS reads share the GATT transaction path with DATA
                # notifications on Windows.  Poll slowly; the 48-byte EEG frame
                # already carries the real-time sequence/queue diagnostics.
                await asyncio.sleep(BLE_STATUS_POLL_INTERVAL_S)
                if self._streaming_hint:
                    # The STATUS characteristic remains subscribed, so ACKs and
                    # firmware-pushed status still arrive. Skipping active reads
                    # removes a periodic source of Windows GATT head-of-line blocking.
                    continue
                try:
                    payload = bytes(await client.read_gatt_char(BLE_STATUS_UUID))
                    self._publish_status(payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.info.emit(f"BLE 状态轮询失败：{exc}")
        except asyncio.CancelledError:
            pass

    def _on_disconnected(self, client):
        if client is not self._client:
            return
        self._client = None
        if self._status_poll_task is not None:
            self._status_poll_task.cancel()
            self._status_poll_task = None
        if self._reliable_watchdog_task is not None:
            self._reliable_watchdog_task.cancel()
            self._reliable_watchdog_task = None
        should_reconnect = (
            not self._closing and not self._manual_disconnect and self._desired_key is not None
        )
        self.disconnected.emit("BLE 链路意外断开", should_reconnect)
        if should_reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(self._desired_key))

    async def _reconnect_loop(self, key: str):
        delay = 1.0
        while not self._closing and self._desired_key == key and not self._manual_disconnect:
            self.info.emit(f"BLE 将在 {delay:.0f} 秒后自动重连…")
            await asyncio.sleep(delay)
            try:
                await self._connect_to_device(key, reconnected=True)
                if self._client is not None and bool(getattr(self._client, "is_connected", False)):
                    return
            except Exception as exc:
                self.info.emit(f"BLE 重连失败：{exc}")
            delay = min(5.0, delay * 2.0)

    def write_blocking(self, data: bytes, timeout: float = 3.0):
        future = self._submit(self._write(bytes(data)))
        return future.result(timeout=max(0.5, float(timeout)))

    def read_status_blocking(self, timeout: float = 1.0) -> bytes:
        """Read STATUS directly when a notification was lost during setup."""
        future = self._submit(self._read_status())
        return bytes(future.result(timeout=max(0.5, float(timeout))))

    async def _read_status(self) -> bytes:
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        lock = self._gatt_write_lock
        if lock is None:
            raise RuntimeError("BLE GATT 锁未就绪")
        async with lock:
            return bytes(await client.read_gatt_char(BLE_STATUS_UUID))

    async def _write(self, data: bytes):
        client = self._client
        if client is None or not bool(getattr(client, "is_connected", False)):
            raise RuntimeError("BLE 尚未连接")
        if not data:
            return
        lock = self._gatt_write_lock
        if lock is None:
            raise RuntimeError("BLE 写入锁未就绪")
        async with lock:
            await client.write_gatt_char(BLE_CONTROL_UUID, data, response=True)

    def disconnect_blocking(self, timeout: float = 4.0):
        self._desired_key = None
        self._manual_disconnect = True
        future = self._submit(self._disconnect_current())
        return future.result(timeout=max(1.0, float(timeout)))

    async def _disconnect_current(self):
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._status_poll_task is not None:
            self._status_poll_task.cancel()
            self._status_poll_task = None
        if self._reliable_watchdog_task is not None:
            self._reliable_watchdog_task.cancel()
            self._reliable_watchdog_task = None
        client = self._client
        self._client = None
        if client is not None:
            try:
                if bool(getattr(client, "is_connected", False)):
                    try:
                        await client.stop_notify(BLE_DATA_UUID)
                    except Exception:
                        pass
                    try:
                        await client.stop_notify(BLE_STATUS_UUID)
                        await client.stop_notify(BLE_RESPONSE_UUID)
                    except Exception:
                        pass
                    await client.disconnect()
            finally:
                self.disconnected.emit("BLE 已断开", False)

    def shutdown(self):
        if not self.isRunning():
            return
        self._closing = True
        try:
            future = self._submit(self._disconnect_current())
            future.result(timeout=3.0)
        except Exception:
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.wait(3000)
