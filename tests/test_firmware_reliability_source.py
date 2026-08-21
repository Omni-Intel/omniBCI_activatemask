import unittest
from pathlib import Path


FIRMWARE = Path(__file__).parents[1] / "firmware" / "ESP32C3_ADS1299_SRB1_BLE_V19" / "ESP32C3_ADS1299_SRB1_BLE_V19.ino"


class FirmwareReliabilitySourceTests(unittest.TestCase):
    def test_failed_new_block_send_does_not_advance_sequence(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertNotIn("const uint32_t seq = bleReliableNextNewTxSequence++;", source)
        self.assertIn("if (!sendReliableBlock(snapshot, false)) return;", source)
        self.assertIn("bleReliableNextNewTxSequence++;", source)
        self.assertIn("if (!copyReliableBlock(seq, snapshot)) return;", source)

    def test_ble_tx_sends_a_snapshot_not_a_shared_ring_slot(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("copyReliableBlock(requested, snapshot)", source)
        self.assertIn("portENTER_CRITICAL(&bleReliableMux);", source)
        self.assertIn("markReliableBlockSent(snapshot);", source)

    def test_failed_nack_retransmission_keeps_request_pending(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("if (!sendReliableBlock(snapshot, true)) return;", source)
        self.assertIn("if (!sendReliableGapMarker(requested)) return;", source)
        self.assertIn("bleReliableNackPending && bleReliableNackFirst == requested", source)

    def test_sampling_does_not_mask_interrupts_for_the_full_frame(self):
        source = FIRMWARE.read_text(encoding="utf-8")
        read_frame = source.split("bool readAdsFrame", 1)[1].split("void deselectAllSpi", 1)[0]

        self.assertNotIn("noInterrupts();", read_frame)
        self.assertNotIn("interrupts();", read_frame)

    def test_notify_acceptance_controls_tx_progress(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("bleDataLastNotifyResult = 0", source)
        self.assertIn("bleDataLastNotifyResult = 1", source)
        self.assertIn("bleDataLastNotifyResult = -1", source)
        self.assertIn("if (bleDataLastNotifyResult != 1) return false;", source)
        self.assertIn("bleNotifyBackoffUntilMs", source)

    def test_status_v5_exports_capture_diagnostics(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("constexpr size_t BLE_STATUS_BYTES = 96;", source)
        self.assertIn("destination[2] = 0x05", source)
        self.assertIn("writeU32LE(&destination[76], missedDrdyCount);", source)
        self.assertIn("writeU32LE(&destination[92], maxReadTimeUs);", source)

    def test_capture_queue_has_two_seconds_of_elasticity(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("constexpr uint16_t FRAME_QUEUE_LENGTH = 512;", source)


if __name__ == "__main__":
    unittest.main()
