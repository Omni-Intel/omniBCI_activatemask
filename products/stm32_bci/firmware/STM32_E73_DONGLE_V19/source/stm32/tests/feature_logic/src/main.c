#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#include <zephyr/ztest.h>

#include "feature_logic.h"

ZTEST(feature_logic, test_sample_time_wrap)
{
	zassert_equal(bci_unwrap_sample_us(9000, 4000), 4000);
	zassert_equal(bci_unwrap_sample_us(0x100000010ULL, 0xfffffff0U), 0xfffffff0ULL);
	zassert_equal(bci_unwrap_sample_us(0x200001000ULL, 0x800U), 0x200000800ULL);
}

ZTEST(feature_logic, test_sd_late_insert_policy)
{
	zassert_true(bci_sd_probe_allowed(false, true, true, false));
	zassert_true(bci_sd_probe_allowed(true, false, true, false));
	zassert_true(bci_sd_probe_allowed(true, true, false, false));
	zassert_false(bci_sd_probe_allowed(true, true, true, false));
	zassert_false(bci_sd_probe_allowed(false, true, false, true));
	zassert_false(bci_sd_probe_allowed(true, false, false, true));
}

ZTEST(feature_logic, test_trigger_debounce_and_wrap)
{
	uint32_t last = 0U;
	bool seen = false;

	zassert_true(bci_trigger_accept(100U, &last, &seen));
	zassert_false(bci_trigger_accept(104U, &last, &seen));
	zassert_true(bci_trigger_accept(105U, &last, &seen));

	last = UINT32_MAX - 2U;
	seen = true;
	zassert_true(bci_trigger_accept(3U, &last, &seen));
}

ZTEST(feature_logic, test_led_waveforms)
{
	zassert_false(bci_led_output(BCI_LED_STOPPED, 0U));
	zassert_true(bci_led_output(BCI_LED_ACQUIRING, 999U));
	zassert_true(bci_led_output(BCI_LED_BOOT, 0U));
	zassert_false(bci_led_output(BCI_LED_BOOT, 500U));
	zassert_true(bci_led_output(BCI_LED_FATAL, 0U));
	zassert_false(bci_led_output(BCI_LED_FATAL, 100U));
}

ZTEST(feature_logic, test_acquisition_health_timeout_and_wrap)
{
	zassert_true(bci_acquisition_healthy(1099U, 1000U));
	zassert_false(bci_acquisition_healthy(1100U, 1000U));
	zassert_true(bci_acquisition_healthy(50U, UINT32_MAX - 20U));
}

ZTEST(feature_logic, test_sd_filename_bounds)
{
	char name[18];

	zassert_equal(bci_sd_filename(name, sizeof(name), 1U), 0);
	zassert_mem_equal(name, "/SD:/BCI00001.BIN", sizeof(name));
	zassert_equal(bci_sd_filename(name, sizeof(name), 99999U), 0);
	zassert_mem_equal(name, "/SD:/BCI99999.BIN", sizeof(name));
	zassert_equal(bci_sd_filename(name, sizeof(name), 0U), -EINVAL);
	zassert_equal(bci_sd_filename(name, sizeof(name), 100000U), -EINVAL);
	zassert_equal(bci_sd_filename(name, sizeof(name) - 1U, 1U), -EINVAL);
	zassert_equal(bci_sd_filename(NULL, sizeof(name), 1U), -EINVAL);
}

ZTEST(feature_logic, test_sd_batch_geometry)
{
	zassert_equal(BCI_STREAM_FRAME_SIZE, 48U);
	zassert_equal(BCI_SD_BATCH_FRAMES, 128U);
	zassert_equal(BCI_SD_BATCH_BYTES, 6144U);
	zassert_equal(BCI_SD_BATCH_BYTES % BCI_STREAM_FRAME_SIZE, 0U);
	zassert_equal(BCI_SD_BATCH_BYTES % 512U, 0U);
}

ZTEST_SUITE(feature_logic, NULL, NULL, NULL, NULL, NULL);
