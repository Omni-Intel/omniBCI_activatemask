import unittest
from pathlib import Path


FIRMWARE = Path(__file__).parents[2] / "firmware" / "ESP32C3_ADS1299_FULL_DIFF_BLE_V20" / "ESP32C3_ADS1299_FULL_DIFF_BLE_V20.ino"


class FirmwareReliabilitySourceTests(unittest.TestCase):
    def test_failed_new_block_send_does_not_advance_sequence(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertNotIn("const uint32_t seq = bleReliableNextNewTxSequence++;", source)
        self.assertTrue("if (sendReliableBlock(*block, false)) bleReliableNextNewTxSequence++;" in source)

    def test_v20_marks_sent_only_after_notify_acceptance(self):
        source = FIRMWARE.read_text(encoding="utf-8")
        body = source.split("bool sendReliableBlock(ReliableBleBlock &block, bool retransmission) {", 1)[1]
        self.assertLess(body.index("if (!sendBleBytes("), body.index("block.sent = true;"))
        # V20 is frozen; unlike the later EEG baseline it sends a shared ring slot.
        self.assertTrue("copyReliableBlock(requested, snapshot)" not in source)

    def test_failed_nack_retransmission_keeps_request_pending(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertTrue("transmitted = sendReliableBlock(*block, true);" in source)
        self.assertTrue("transmitted = sendReliableGapMarker(requested);" in source)
        self.assertTrue("if (retryRequired) return;" in source)

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

    def test_capture_queue_is_512_frames_not_a_rate_dependent_duration(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("constexpr uint16_t FRAME_QUEUE_LENGTH = 512;", source)


if __name__ == "__main__":
    unittest.main()
