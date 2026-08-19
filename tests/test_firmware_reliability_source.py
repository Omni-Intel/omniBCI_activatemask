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


if __name__ == "__main__":
    unittest.main()
