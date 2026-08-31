#ifndef BCIBAND_MCUBOOT_STM32_COMPAT_H_
#define BCIBAND_MCUBOOT_STM32_COMPAT_H_

#if !defined(CONFIG_SOC_FAMILY_NORDIC_NRF)
#define nrf_crypto_keys_housekeeping() do { } while (0)
#endif

#endif
