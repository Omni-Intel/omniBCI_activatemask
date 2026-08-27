// Fixed SRB2 differential-input build for the myEMGpcb board.
// The shared implementation keeps packet layout, BLE UUIDs and GUI controls
// identical to the SRB1 V19 firmware; only ADS1299 reference-side routing and
// the default channel mask differ.
#define OMNIBCI_FIXED_REFERENCE_SRB2 1
#define OMNIBCI_ACTIVE_CH_MASK 0xFF
#include "../ESP32C3_ADS1299_SRB1_BLE_V19/ESP32C3_ADS1299_SRB1_BLE_V19.ino"
