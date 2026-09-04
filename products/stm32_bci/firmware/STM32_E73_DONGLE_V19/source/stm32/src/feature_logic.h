#ifndef BCI_FEATURE_LOGIC_H
#define BCI_FEATURE_LOGIC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BCI_TRIGGER_DEBOUNCE_MS 5U
#define BCI_ACQUISITION_HEALTH_TIMEOUT_MS 100U
#define BCI_STREAM_FRAME_SIZE 48U
#define BCI_SD_BATCH_FRAMES 128U
#define BCI_SD_BATCH_BYTES (BCI_STREAM_FRAME_SIZE * BCI_SD_BATCH_FRAMES)
#define BCI_SD_FILENAME_BYTES 18U

enum bci_led_state {
	BCI_LED_STOPPED = 0,
	BCI_LED_BOOT,
	BCI_LED_ACQUIRING,
	BCI_LED_FATAL,
};

bool bci_trigger_accept(uint32_t now_ms, uint32_t *last_ms, bool *seen);
bool bci_led_output(enum bci_led_state state, uint32_t elapsed_ms);
bool bci_acquisition_healthy(uint32_t now_ms, uint32_t last_success_ms);
int bci_sd_filename(char *destination, size_t size, uint32_t index);
bool bci_sd_probe_allowed(bool gui, bool running, bool attempted, bool failed);
uint64_t bci_unwrap_sample_us(uint64_t now_us, uint32_t sample_us);

#endif
