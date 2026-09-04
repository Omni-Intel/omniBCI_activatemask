#include "feature_logic.h"

bool bci_sd_probe_allowed(bool gui, bool running, bool attempted, bool failed)
{
	return !failed && (!gui || !running || !attempted);
}

uint64_t bci_unwrap_sample_us(uint64_t now_us, uint32_t sample_us)
{
	/* Queue residence is bounded well below the 71-minute wire timestamp period. */
	return now_us - (uint32_t)(now_us - sample_us);
}

#include <errno.h>
#include <stdio.h>

bool bci_trigger_accept(uint32_t now_ms, uint32_t *last_ms, bool *seen)
{
	if (last_ms == NULL || seen == NULL) {
		return false;
	}
	if (*seen && (uint32_t)(now_ms - *last_ms) < BCI_TRIGGER_DEBOUNCE_MS) {
		return false;
	}
	*seen = true;
	*last_ms = now_ms;
	return true;
}

bool bci_led_output(enum bci_led_state state, uint32_t elapsed_ms)
{
	switch (state) {
	case BCI_LED_ACQUIRING:
		return true;
	case BCI_LED_BOOT:
		return (elapsed_ms % 1000U) < 500U;
	case BCI_LED_FATAL:
		return (elapsed_ms % 200U) < 100U;
	case BCI_LED_STOPPED:
	default:
		return false;
	}
}

bool bci_acquisition_healthy(uint32_t now_ms, uint32_t last_success_ms)
{
	return (uint32_t)(now_ms - last_success_ms) <
		BCI_ACQUISITION_HEALTH_TIMEOUT_MS;
}

int bci_sd_filename(char *destination, size_t size, uint32_t index)
{
	if (destination == NULL || size < BCI_SD_FILENAME_BYTES ||
	    index == 0U || index > 99999U) {
		return -EINVAL;
	}
	int written = snprintf(destination, size, "/SD:/BCI%05u.BIN", index);
	return written == (int)(BCI_SD_FILENAME_BYTES - 1U) ? 0 : -EINVAL;
}
