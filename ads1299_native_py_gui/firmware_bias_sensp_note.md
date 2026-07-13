# Firmware note for BIAS_SENSP GUI command

Python GUI sends:

```cpp
A6 0D mask
```

Firmware side should parse this 3-byte binary command and execute only:

```cpp
writeAdsRegister(0x0D, mask);   // ADS1299 BIAS_SENSP
```

Do **not** write register `0x0E` here if you want `BIAS_SENSN` unchanged.

Recommended behavior:

```cpp
static uint8_t currentBiasSensPMask = 0x1F;

void applyBiasSensP(uint8_t mask) {
  currentBiasSensPMask = mask;
  writeAdsRegister(0x0D, currentBiasSensPMask);
}
```

If your mode switching function rewrites ADS1299 registers, use `currentBiasSensPMask` instead of hard-coded `0x1F`; otherwise mode switching will overwrite the GUI selection.
