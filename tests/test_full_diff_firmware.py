from pathlib import Path
import re
import unittest


FIRMWARE = (
    Path(__file__).resolve().parents[1]
    / "firmware"
    / "ESP32C3_ADS1299_FULL_DIFF_BLE_V20"
    / "ESP32C3_ADS1299_FULL_DIFF_BLE_V20.ino"
)


class FullDifferentialFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = FIRMWARE.read_text(encoding="utf-8")

    def test_reference_selection_macro_and_on_constant_are_absent(self):
        self.assertNotIn("OMNIBCI_FIXED_REFERENCE_SRB2", self.source)
        self.assertNotIn("MISC1_SRB1_ON", self.source)
        self.assertIn("#define MISC1_SRB1_OFF       0x00", self.source)

    def test_channel_builder_cannot_set_srb2(self):
        match = re.search(
            r"uint8_t makeChannelSetting\([^;]+?\) \{.*?^\}",
            self.source,
            flags=re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match)
        body = match.group(0)
        self.assertNotIn("0x08", body)
        self.assertNotIn("srb2", body.lower())
        self.assertIn("(mux & 0x07u)", body)

    def test_register_programming_and_readback_enforce_full_differential(self):
        self.assertEqual(
            self.source.count("writeAdsRegister(0x15, MISC1_SRB1_OFF);"),
            1,
        )
        self.assertIn("writeAdsRegister(0x0D, biasP);", self.source)
        self.assertIn("writeAdsRegister(0x0E, biasN);", self.source)
        self.assertIn("writeAdsRegister(0x0F, leadOffMask);", self.source)
        self.assertIn("writeAdsRegister(0x10, leadOffMask);", self.source)
        self.assertIn("(observed & 0x08u) == 0x00u", self.source)
        self.assertIn("readAdsRegister(0x15) == MISC1_SRB1_OFF", self.source)

    def test_v20_identity_is_exposed(self):
        self.assertIn("FIRMWARE_VERSION_MAJOR = 20", self.source)
        self.assertIn('BLE_DEVICE_NAME[] = "OmniBCI-C3-FULLDIFF-V20"', self.source)
        self.assertIn("sendConfigAck(0xAB, FIRMWARE_PROFILE_FULL_DIFF)", self.source)


if __name__ == "__main__":
    unittest.main()
