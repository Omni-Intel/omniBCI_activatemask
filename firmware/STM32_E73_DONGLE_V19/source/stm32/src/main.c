#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/usb/usb_device.h>

#include "control_tunnel.h"

LOG_MODULE_REGISTER(bciband_stm32, LOG_LEVEL_INF);

#define ADS_FRAME_SIZE       27U
#define STREAM_FRAME_SIZE    48U
#define ADS_WAKEUP           0x02U
#define ADS_RESET_CMD        0x06U
#define ADS_START_CMD        0x08U
#define ADS_STOP_CMD         0x0AU
#define ADS_RDATAC           0x10U
#define ADS_SDATAC           0x11U
#define ADS_RREG             0x20U
#define ADS_WREG             0x40U

#define ADS_REG_CONFIG1      0x01U
#define ADS_REG_CONFIG2      0x02U
#define ADS_REG_CONFIG3      0x03U
#define ADS_REG_LOFF         0x04U
#define ADS_REG_CH1SET       0x05U
#define ADS_REG_CH8SET       0x0CU
#define ADS_REG_BIAS_SENSP   0x0DU
#define ADS_REG_BIAS_SENSN   0x0EU
#define ADS_REG_LOFF_SENSP   0x0FU
#define ADS_REG_LOFF_SENSN   0x10U
#define ADS_REG_LOFF_FLIP    0x11U
#define ADS_REG_MISC1        0x15U

#define ADS_CONFIG1_250SPS   0x96U
#define ADS_CONFIG1_500SPS   0x95U
#define ADS_CONFIG1_1000SPS  0x94U
#define ADS_CONFIG2_NORMAL   0xC0U
#define ADS_CONFIG2_TEST     0xD1U
#define ADS_CONFIG3_INT_REF  0xECU
#define ADS_LOFF_6NA_31HZ    0x02U
#define ADS_MISC1_SRB1       0x20U
#define ADS_ACTIVE_CH_MASK   0xFFU

#define CH_POWER_DOWN        0x80U
#define CH_MUX_NORMAL        0x00U
#define CH_MUX_SHORTED       0x01U
#define CH_MUX_TEST          0x05U

#define NSC_PWM_PERIOD_NS    5000U
#define NSC_PWM_PULSE_NS     2500U
#define WORK_LED_TIMEOUT_MS  100U
#define CONTROL_REPLY_DEPTH  24U

#define GPIO_SPEC(controller, pin_number, flags_value) \
	{ .port = DEVICE_DT_GET(DT_NODELABEL(controller)), \
	  .pin = (pin_number), .dt_flags = (flags_value) }

enum frontend_mode {
	MODE_EEG_BIAS_PN = 0,
	MODE_EEG_BIAS_P_ONLY = 1,
	MODE_EEG_BIAS_OFF = 2,
	MODE_INPUT_SHORTED = 3,
	MODE_INTERNAL_TEST = 4,
};

struct control_reply {
	uint8_t length;
	uint8_t data[BCI_TUNNEL_MAX_PAYLOAD];
};

static const struct gpio_dt_spec ads_drdy =
	GPIO_SPEC(gpioa, 4U, GPIO_ACTIVE_LOW | GPIO_PULL_UP);
static const struct gpio_dt_spec ads_cs = GPIO_SPEC(gpiob, 2U, GPIO_ACTIVE_HIGH);
static const struct gpio_dt_spec ads_start = GPIO_SPEC(gpioe, 8U, GPIO_ACTIVE_HIGH);
static const struct gpio_dt_spec ads_reset = GPIO_SPEC(gpioe, 10U, GPIO_ACTIVE_HIGH);
static const struct gpio_dt_spec rf_reset = GPIO_SPEC(gpiod, 5U, GPIO_ACTIVE_HIGH);
static const struct gpio_dt_spec rf_irq =
	GPIO_SPEC(gpiod, 6U, GPIO_ACTIVE_HIGH | GPIO_PULL_DOWN);
static const struct gpio_dt_spec rf_csn = GPIO_SPEC(gpiob, 9U, GPIO_ACTIVE_HIGH);
/* Board wiring: PA1 -> resistor/LED -> GND (active high). */
static const struct gpio_dt_spec work_led = GPIO_SPEC(gpioa, 1U, GPIO_ACTIVE_HIGH);

static const struct pwm_dt_spec nsc_pwm =
	PWM_DT_SPEC_GET(DT_NODELABEL(nsc_pwm_output));
static const struct device *const ads_spi = DEVICE_DT_GET(DT_NODELABEL(spi1));
static const struct device *const rf_spi = DEVICE_DT_GET(DT_NODELABEL(spi3));
static const struct device *const usb_cdc =
	DEVICE_DT_GET(DT_NODELABEL(cdc_acm_uart0));

static const struct spi_config rf_spi_config = {
	.frequency = 2000000U,
	.operation = SPI_OP_MODE_MASTER | SPI_TRANSFER_MSB | SPI_WORD_SET(8),
	.slave = 0U,
};

static const struct spi_config ads_spi_config = {
	.frequency = 1000000U,
	/* ADS1299 shifts DOUT on SCLK rising edges and latches DIN on falling edges. */
	.operation = SPI_OP_MODE_MASTER | SPI_MODE_CPHA |
		SPI_TRANSFER_MSB | SPI_WORD_SET(8),
	.slave = 0U,
};

K_SEM_DEFINE(ads_drdy_sem, 0, 1);
K_MSGQ_DEFINE(control_reply_queue, sizeof(struct control_reply),
	      CONTROL_REPLY_DEPTH, 4);
static struct gpio_callback ads_drdy_callback;

static uint32_t sample_sequence;
static uint32_t ads_frames;
static uint32_t rf_frames;
static uint32_t rf_errors;
static uint32_t usb_frames;
static uint32_t control_commands;
static uint32_t control_replies;
static uint32_t last_successful_frame_ms;
static uint16_t reply_sequence;
static uint16_t current_sample_rate_hz = 250U;
static uint8_t current_config1 = ADS_CONFIG1_250SPS;
static uint8_t current_sample_rate_code;

static bool streaming_enabled;
static bool configuration_verified;
static enum frontend_mode current_mode = MODE_EEG_BIAS_P_ONLY;
static uint8_t current_enabled_mask = ADS_ACTIVE_CH_MASK;
static uint8_t current_bias_mask = ADS_ACTIVE_CH_MASK;
static uint8_t current_lead_off_mask;
static uint8_t channel_gain[8] = {24, 24, 24, 24, 24, 24, 24, 24};
static uint8_t channel_gain_code[8] = {6, 6, 6, 6, 6, 6, 6, 6};

static uint8_t binary_state;
static uint8_t binary_register;
static uint8_t binary_channel;
static uint8_t binary_gain;
static uint8_t bulk_config[12];
static uint8_t bulk_index;
static char numeric_command[4];
static uint8_t numeric_length;
static uint32_t numeric_last_ms;

static void process_control_bytes(const uint8_t *data, size_t length);

static void work_led_set(bool on)
{
	(void)gpio_pin_set_dt(&work_led, on ? 1 : 0);
}

static void work_led_update(uint32_t now)
{
	if (!streaming_enabled || last_successful_frame_ms == 0U ||
	    (uint32_t)(now - last_successful_frame_ms) >= WORK_LED_TIMEOUT_MS) {
		work_led_set(false);
	}
}

static void put_u16_le(uint8_t *p, uint16_t value)
{
	p[0] = (uint8_t)value;
	p[1] = (uint8_t)(value >> 8);
}

static void put_u32_le(uint8_t *p, uint32_t value)
{
	p[0] = (uint8_t)value;
	p[1] = (uint8_t)(value >> 8);
	p[2] = (uint8_t)(value >> 16);
	p[3] = (uint8_t)(value >> 24);
}

static uint8_t ads_spi_transfer(uint8_t tx)
{
	uint8_t rx = 0U;
	const struct spi_buf tx_buf = {.buf = &tx, .len = 1U};
	struct spi_buf rx_buf = {.buf = &rx, .len = 1U};
	const struct spi_buf_set tx_set = {.buffers = &tx_buf, .count = 1U};
	const struct spi_buf_set rx_set = {.buffers = &rx_buf, .count = 1U};

	if (spi_transceive(ads_spi, &ads_spi_config, &tx_set, &rx_set) != 0) {
		return 0U;
	}
	return rx;
}

static void ads_select(void)
{
	gpio_pin_set_raw(ads_cs.port, ads_cs.pin, 0);
	k_busy_wait(4U);
}

static void ads_deselect(void)
{
	k_busy_wait(4U);
	gpio_pin_set_raw(ads_cs.port, ads_cs.pin, 1);
}

static void ads_command(uint8_t command)
{
	ads_select();
	(void)ads_spi_transfer(command);
	ads_deselect();
	k_msleep(2U);
}

static void ads_write_register(uint8_t address, uint8_t value)
{
	ads_select();
	(void)ads_spi_transfer((uint8_t)(ADS_WREG | (address & 0x1FU)));
	k_busy_wait(2U);
	(void)ads_spi_transfer(0U);
	(void)ads_spi_transfer(value);
	ads_deselect();
	k_msleep(2U);
}

static uint8_t ads_read_register(uint8_t address)
{
	uint8_t value;

	ads_select();
	(void)ads_spi_transfer((uint8_t)(ADS_RREG | (address & 0x1FU)));
	k_busy_wait(2U);
	(void)ads_spi_transfer(0U);
	value = ads_spi_transfer(0U);
	ads_deselect();
	k_msleep(2U);
	return value;
}

static bool ads_read_frame(uint8_t frame[ADS_FRAME_SIZE])
{
	uint8_t zeros[ADS_FRAME_SIZE] = {0};
	const struct spi_buf tx_buf = {.buf = zeros, .len = sizeof(zeros)};
	struct spi_buf rx_buf = {.buf = frame, .len = ADS_FRAME_SIZE};
	const struct spi_buf_set tx = {.buffers = &tx_buf, .count = 1U};
	const struct spi_buf_set rx = {.buffers = &rx_buf, .count = 1U};
	int err;

	if (gpio_pin_get_raw(ads_drdy.port, ads_drdy.pin) != 0) {
		return false;
	}
	ads_select();
	err = spi_transceive(ads_spi, &ads_spi_config, &tx, &rx);
	ads_deselect();
	return err == 0;
}

static bool gain_to_code(uint8_t gain, uint8_t *code)
{
	switch (gain) {
	case 1: *code = 0U; return true;
	case 2: *code = 1U; return true;
	case 4: *code = 2U; return true;
	case 6: *code = 3U; return true;
	case 8: *code = 4U; return true;
	case 12: *code = 5U; return true;
	case 24: *code = 6U; return true;
	default: return false;
	}
}

static uint8_t channel_setting(uint8_t gain_code, uint8_t mux)
{
	return (uint8_t)(((gain_code & 7U) << 4) | (mux & 7U));
}

static void ads_stop_streaming(void)
{
	work_led_set(false);
	if (!streaming_enabled) {
		return;
	}
	gpio_pin_set_raw(ads_start.port, ads_start.pin, 0);
	ads_command(ADS_STOP_CMD);
	ads_command(ADS_SDATAC);
	streaming_enabled = false;
	k_sem_reset(&ads_drdy_sem);
}

static void ads_start_streaming(void)
{
	if (streaming_enabled || !configuration_verified) {
		return;
	}
	k_sem_reset(&ads_drdy_sem);
	gpio_pin_set_raw(ads_start.port, ads_start.pin, 1);
	k_msleep(2U);
	ads_command(ADS_RDATAC);
	ads_command(ADS_START_CMD);
	streaming_enabled = true;
}

static bool verify_frontend(enum frontend_mode mode)
{
	uint8_t expected_mux = CH_MUX_NORMAL;
	uint8_t expected_bias_p = current_bias_mask & current_enabled_mask;
	uint8_t expected_bias_n = 0U;
	bool srb1 = mode <= MODE_EEG_BIAS_OFF;
	uint8_t loff = srb1 ? (current_lead_off_mask & current_enabled_mask) : 0U;

	if (mode == MODE_INPUT_SHORTED) expected_mux = CH_MUX_SHORTED;
	if (mode == MODE_INTERNAL_TEST) expected_mux = CH_MUX_TEST;
	if (mode == MODE_EEG_BIAS_PN) expected_bias_n = expected_bias_p;
	if (mode >= MODE_EEG_BIAS_OFF) {
		expected_bias_p = 0U;
		expected_bias_n = 0U;
	}

	bool ok = ads_read_register(ADS_REG_CONFIG1) == current_config1;
	ok &= ads_read_register(ADS_REG_CONFIG2) ==
		(mode == MODE_INTERNAL_TEST ? ADS_CONFIG2_TEST : ADS_CONFIG2_NORMAL);
	ok &= ads_read_register(ADS_REG_CONFIG3) == ADS_CONFIG3_INT_REF;
	for (uint8_t ch = 0U; ch < 8U; ++ch) {
		uint8_t expected = channel_setting(channel_gain_code[ch], expected_mux);
		if ((current_enabled_mask & BIT(ch)) == 0U) {
			expected = CH_POWER_DOWN |
				channel_setting(channel_gain_code[ch], CH_MUX_SHORTED);
		}
		ok &= ads_read_register(ADS_REG_CH1SET + ch) == expected;
	}
	ok &= ads_read_register(ADS_REG_BIAS_SENSP) == expected_bias_p;
	ok &= ads_read_register(ADS_REG_BIAS_SENSN) == expected_bias_n;
	ok &= ads_read_register(ADS_REG_LOFF) == (loff ? ADS_LOFF_6NA_31HZ : 0U);
	ok &= ads_read_register(ADS_REG_LOFF_SENSP) == loff;
	ok &= ads_read_register(ADS_REG_LOFF_SENSN) == 0U;
	ok &= ads_read_register(ADS_REG_MISC1) == (srb1 ? ADS_MISC1_SRB1 : 0U);
	return ok;
}

static bool configure_frontend(enum frontend_mode mode)
{
	if (streaming_enabled) {
		return false;
	}
	uint8_t mux = CH_MUX_NORMAL;
	uint8_t bias_p = current_bias_mask & current_enabled_mask;
	uint8_t bias_n = 0U;
	bool srb1 = mode <= MODE_EEG_BIAS_OFF;
	uint8_t loff = srb1 ? (current_lead_off_mask & current_enabled_mask) : 0U;

	if (mode == MODE_INPUT_SHORTED) mux = CH_MUX_SHORTED;
	if (mode == MODE_INTERNAL_TEST) mux = CH_MUX_TEST;
	if (mode == MODE_EEG_BIAS_PN) bias_n = bias_p;
	if (mode >= MODE_EEG_BIAS_OFF) {
		bias_p = 0U;
		bias_n = 0U;
	}

	configuration_verified = false;
	ads_write_register(ADS_REG_CONFIG1, current_config1);
	ads_write_register(ADS_REG_CONFIG2,
		mode == MODE_INTERNAL_TEST ? ADS_CONFIG2_TEST : ADS_CONFIG2_NORMAL);
	ads_write_register(ADS_REG_CONFIG3, ADS_CONFIG3_INT_REF);
	for (uint8_t ch = 0U; ch < 8U; ++ch) {
		uint8_t value = channel_setting(channel_gain_code[ch], mux);
		if ((current_enabled_mask & BIT(ch)) == 0U) {
			value = CH_POWER_DOWN |
				channel_setting(channel_gain_code[ch], CH_MUX_SHORTED);
		}
		ads_write_register(ADS_REG_CH1SET + ch, value);
	}
	ads_write_register(ADS_REG_BIAS_SENSP, bias_p);
	ads_write_register(ADS_REG_BIAS_SENSN, bias_n);
	ads_write_register(ADS_REG_LOFF, loff ? ADS_LOFF_6NA_31HZ : 0U);
	ads_write_register(ADS_REG_LOFF_SENSP, loff);
	ads_write_register(ADS_REG_LOFF_SENSN, 0U);
	ads_write_register(ADS_REG_LOFF_FLIP, 0U);
	ads_write_register(ADS_REG_MISC1, srb1 ? ADS_MISC1_SRB1 : 0U);
	current_mode = mode;
	configuration_verified = verify_frontend(mode);
	return configuration_verified;
}

static bool ads_initialize(void)
{
	gpio_pin_set_raw(ads_start.port, ads_start.pin, 0);
	gpio_pin_set_raw(ads_reset.port, ads_reset.pin, 0);
	k_msleep(100U);
	gpio_pin_set_raw(ads_reset.port, ads_reset.pin, 1);
	k_msleep(500U);
	ads_command(ADS_WAKEUP);
	ads_command(ADS_SDATAC);
	ads_command(ADS_STOP_CMD);
	streaming_enabled = false;
	return configure_frontend(MODE_EEG_BIAS_P_ONLY);
}

static void queue_control_reply(const uint8_t *data, uint8_t length)
{
	struct control_reply reply = {.length = length};

	if (length == 0U || length > sizeof(reply.data)) {
		return;
	}
	memcpy(reply.data, data, length);
	if (k_msgq_put(&control_reply_queue, &reply, K_NO_WAIT) == 0) {
		control_replies++;
	}
}

static void send_config_ack(uint8_t command, uint8_t argument)
{
	uint8_t reply[12] = {
		0xBC, command, argument, 0xFF, 0xFF, 0xFF,
		0xFF, 0x00, (uint8_t)current_mode, 0x00,
		current_enabled_mask, 0x00
	};

	if (!streaming_enabled) {
		if (command == 0xAAU) {
			reply[3] = ads_read_register(ADS_REG_CONFIG1);
			reply[4] = (uint8_t)current_sample_rate_hz;
			reply[5] = (uint8_t)(current_sample_rate_hz >> 8);
			reply[6] = current_sample_rate_code;
		} else if (command == 0xA9U) {
			reply[3] = current_lead_off_mask;
			reply[4] = ads_read_register(ADS_REG_LOFF_SENSP);
			reply[5] = ads_read_register(ADS_REG_LOFF_SENSN);
			reply[6] = ads_read_register(ADS_REG_LOFF);
		} else {
			reply[3] = command == 0xA7U ?
				ads_read_register(ADS_REG_CH1SET + (argument & 7U)) :
				current_bias_mask;
			reply[4] = ads_read_register(ADS_REG_BIAS_SENSP);
			reply[5] = ads_read_register(ADS_REG_BIAS_SENSN);
			reply[6] = ads_read_register(ADS_REG_MISC1);
		}
		reply[9] = configuration_verified ? 1U : 0U;
	}
	for (uint8_t i = 0U; i < 11U; ++i) {
		reply[11] ^= reply[i];
	}
	queue_control_reply(reply, sizeof(reply));
}

static void set_all_gain(uint8_t gain)
{
	uint8_t code;
	if (streaming_enabled || !gain_to_code(gain, &code)) return;
	for (uint8_t ch = 0U; ch < 8U; ++ch) {
		channel_gain[ch] = gain;
		channel_gain_code[ch] = code;
	}
	(void)configure_frontend(current_mode);
}

static void flush_numeric_command(void)
{
	if (numeric_length == 0U) return;
	numeric_command[numeric_length] = '\0';
	int value = atoi(numeric_command);
	numeric_length = 0U;
	if (value > 0 && value <= 24) {
		set_all_gain((uint8_t)value);
	}
}

static void set_channel_config(uint8_t channel, uint8_t gain, uint8_t flags)
{
	uint8_t code;
	if (streaming_enabled || channel >= 8U || !gain_to_code(gain, &code)) return;
	uint8_t bit = BIT(channel);
	channel_gain[channel] = gain;
	channel_gain_code[channel] = code;
	if (flags & 1U) current_enabled_mask |= bit;
	else current_enabled_mask &= (uint8_t)~bit;
	if ((flags & 3U) == 3U) current_bias_mask |= bit;
	else current_bias_mask &= (uint8_t)~bit;
	(void)configure_frontend(current_mode);
}

static void set_bulk_config(const uint8_t *payload)
{
	uint8_t codes[8];
	if (streaming_enabled) return;
	for (uint8_t ch = 0U; ch < 8U; ++ch) {
		if (!gain_to_code(payload[4U + ch], &codes[ch])) return;
	}
	current_enabled_mask = payload[1];
	current_bias_mask = payload[2] & current_enabled_mask;
	for (uint8_t ch = 0U; ch < 8U; ++ch) {
		channel_gain[ch] = payload[4U + ch];
		channel_gain_code[ch] = codes[ch];
	}
	(void)configure_frontend(current_mode);
}

static bool set_sample_rate(uint8_t rate_code)
{
	uint16_t rate_hz;
	uint8_t config1;

	switch (rate_code) {
	case 0U: rate_hz = 250U; config1 = ADS_CONFIG1_250SPS; break;
	case 1U: rate_hz = 500U; config1 = ADS_CONFIG1_500SPS; break;
	case 2U: rate_hz = 1000U; config1 = ADS_CONFIG1_1000SPS; break;
	default: return false;
	}

	/* A rate transition is an acquisition boundary.  Leave streaming stopped
	 * so the host can consume the register ACK before explicitly sending 'b'.
	 */
	ads_stop_streaming();
	current_sample_rate_hz = rate_hz;
	current_config1 = config1;
	current_sample_rate_code = rate_code;
	return configure_frontend(current_mode);
}

static void handle_ascii_command(uint8_t command)
{
	switch (command) {
	case 'b': case 'B': ads_start_streaming(); break;
	case 's': case 'S': ads_stop_streaming(); break;
	case 'e': case 'E': case 'p': case 'P': case 'm': case 'M': case '*':
		(void)configure_frontend(MODE_EEG_BIAS_P_ONLY); break;
	case 'n': case 'N': (void)configure_frontend(MODE_EEG_BIAS_PN); break;
	case 'o': case 'O': (void)configure_frontend(MODE_EEG_BIAS_OFF); break;
	case 'q': case 'Q': (void)configure_frontend(MODE_INPUT_SHORTED); break;
	case 't': case 'T': (void)configure_frontend(MODE_INTERNAL_TEST); break;
	case 'r': case 'R':
		ads_frames = rf_frames = rf_errors = usb_frames = 0U; break;
	default: break;
	}
}

static void process_control_byte(uint8_t value)
{
	if (binary_state == 40U) {
		bulk_config[bulk_index++] = value;
		if (bulk_index == sizeof(bulk_config)) {
			set_bulk_config(bulk_config);
			send_config_ack(0xA5U, bulk_config[1]);
			bulk_index = 0U;
			binary_state = 0U;
		}
		return;
	}
	if (binary_state == 1U) {
		binary_register = value;
		binary_state = 2U;
		return;
	}
	if (binary_state == 2U) {
		if (binary_register == ADS_REG_BIAS_SENSP && !streaming_enabled) {
			current_bias_mask = value & current_enabled_mask;
			(void)configure_frontend(current_mode);
			send_config_ack(0xA6U, value);
		}
		binary_state = 0U;
		return;
	}
	if (binary_state == 10U) {
		binary_channel = value;
		binary_state = 11U;
		return;
	}
	if (binary_state == 11U) {
		binary_gain = value;
		binary_state = 12U;
		return;
	}
	if (binary_state == 12U) {
		set_channel_config(binary_channel, binary_gain, value);
		send_config_ack(0xA7U, binary_channel);
		binary_state = 0U;
		return;
	}
	if (binary_state == 20U) {
		/* V19 SRB1-only: consume A8 reference byte and keep SRB1 fixed. */
		binary_state = 0U;
		return;
	}
	if (binary_state == 30U) {
		if (!streaming_enabled) {
			current_lead_off_mask = value & current_enabled_mask;
			(void)configure_frontend(current_mode);
		}
		send_config_ack(0xA9U, value);
		binary_state = 0U;
		return;
	}
	if (binary_state == 50U) {
		(void)set_sample_rate(value);
		send_config_ack(0xAAU, value);
		binary_state = 0U;
		return;
	}

	if (value == 0xA5U) { flush_numeric_command(); bulk_index = 0U; binary_state = 40U; return; }
	if (value == 0xA6U) { flush_numeric_command(); binary_state = 1U; return; }
	if (value == 0xA7U) { flush_numeric_command(); binary_state = 10U; return; }
	if (value == 0xA8U) { flush_numeric_command(); binary_state = 20U; return; }
	if (value == 0xA9U) { flush_numeric_command(); binary_state = 30U; return; }
	if (value == 0xAAU) { flush_numeric_command(); binary_state = 50U; return; }
	if (value >= '0' && value <= '9') {
		if (numeric_length < sizeof(numeric_command) - 1U) {
			numeric_command[numeric_length++] = (char)value;
			numeric_last_ms = k_uptime_get_32();
		} else {
			numeric_length = 0U;
		}
		return;
	}
	if (value == '\r' || value == '\n' || value == ' ' || value == '\t' ||
	    value == ',' || value == ';') {
		flush_numeric_command();
		return;
	}
	flush_numeric_command();
	handle_ascii_command(value);
}

static void process_control_bytes(const uint8_t *data, size_t length)
{
	for (size_t i = 0U; i < length; ++i) {
		control_commands++;
		process_control_byte(data[i]);
	}
}

static void build_stream_frame(uint8_t destination[STREAM_FRAME_SIZE],
			       const uint8_t ads[ADS_FRAME_SIZE],
			       bool drdy_was_low, uint16_t read_time_us)
{
	uint8_t flags = 0U;
	memset(destination, 0, STREAM_FRAME_SIZE);
	destination[0] = 0xA5U;
	destination[1] = 0x5AU;
	destination[2] = 1U;
	destination[3] = 1U;
	put_u32_le(destination + 4, sample_sequence++);
	put_u32_le(destination + 8, k_uptime_get_32() * 1000U);
	memcpy(destination + 12, ads, 3U);
	if ((ads[0] & 0xF0U) == 0xC0U) flags |= BIT(0);
	if (drdy_was_low) flags |= BIT(1);
	if (current_mode == MODE_INTERNAL_TEST) flags |= BIT(3);
	if (current_mode == MODE_INPUT_SHORTED) flags |= BIT(4);
	if (current_mode == MODE_EEG_BIAS_PN || current_mode == MODE_EEG_BIAS_P_ONLY) flags |= BIT(5);
	if (current_mode == MODE_EEG_BIAS_PN) flags |= BIT(6);
	if (current_mode <= MODE_EEG_BIAS_OFF) flags |= BIT(7);
	destination[15] = flags;
	memcpy(destination + 16, ads + 3, 24U);
	put_u16_le(destination + 40, read_time_us);
	destination[42] = 1U;
	destination[43] = (uint8_t)current_mode;
	destination[44] = 0U;
	destination[45] = 0U;
	put_u16_le(destination + 46,
		   bci_crc16_ccitt_false(destination, STREAM_FRAME_SIZE - 2U));
}

static int rf_exchange(const uint8_t tx_data[STREAM_FRAME_SIZE])
{
	uint8_t rx_data[STREAM_FRAME_SIZE] = {0};
	const struct spi_buf tx_buf = {.buf = (void *)tx_data, .len = STREAM_FRAME_SIZE};
	struct spi_buf rx_buf = {.buf = rx_data, .len = STREAM_FRAME_SIZE};
	const struct spi_buf_set tx = {.buffers = &tx_buf, .count = 1U};
	const struct spi_buf_set rx = {.buffers = &rx_buf, .count = 1U};

	gpio_pin_set_raw(rf_csn.port, rf_csn.pin, 0);
	int err = spi_transceive(rf_spi, &rf_spi_config, &tx, &rx);
	gpio_pin_set_raw(rf_csn.port, rf_csn.pin, 1);
	if (err == 0) {
		struct bci_tunnel_view tunnel;
		if (bci_tunnel_decode(rx_data, sizeof(rx_data), &tunnel) &&
		    tunnel.type == BCI_TUNNEL_TYPE_COMMAND) {
			process_control_bytes(tunnel.payload, tunnel.length);
		}
	}
	return err;
}

static void service_control_link(void)
{
	struct control_reply reply;
	uint8_t tx_data[STREAM_FRAME_SIZE] = {0};
	if (k_msgq_get(&control_reply_queue, &reply, K_NO_WAIT) == 0) {
		(void)bci_tunnel_encode(tx_data, BCI_TUNNEL_TYPE_REPLY,
					reply_sequence++, reply.data, reply.length);
	} else {
		(void)bci_tunnel_encode(tx_data, BCI_TUNNEL_TYPE_POLL,
					reply_sequence++, NULL, 0U);
	}
	if (rf_exchange(tx_data) != 0) rf_errors++;
}

static bool usb_host_ready(void)
{
	uint32_t dtr = 0U;
	return device_is_ready(usb_cdc) &&
		uart_line_ctrl_get(usb_cdc, UART_LINE_CTRL_DTR, &dtr) == 0 && dtr != 0U;
}

static void usb_send_frame(const uint8_t frame[STREAM_FRAME_SIZE])
{
	for (size_t i = 0; i < STREAM_FRAME_SIZE; ++i) uart_poll_out(usb_cdc, frame[i]);
}

static void ads_drdy_handler(const struct device *port,
			     struct gpio_callback *callback,
			     gpio_port_pins_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(callback);
	ARG_UNUSED(pins);
	k_sem_give(&ads_drdy_sem);
}

static int configure_gpio(void)
{
	const struct gpio_dt_spec *all[] = {
		&ads_drdy, &ads_cs,
		&ads_start, &ads_reset, &rf_reset, &rf_irq, &rf_csn, &work_led,
	};
	for (size_t i = 0; i < ARRAY_SIZE(all); ++i) {
		if (!gpio_is_ready_dt(all[i])) return -ENODEV;
	}
	int err = gpio_pin_configure_dt(&ads_drdy, GPIO_INPUT);
	err |= gpio_pin_configure_dt(&ads_cs, GPIO_OUTPUT_ACTIVE);
	err |= gpio_pin_configure_dt(&ads_start, GPIO_OUTPUT_INACTIVE);
	err |= gpio_pin_configure_dt(&ads_reset, GPIO_OUTPUT_ACTIVE);
	err |= gpio_pin_configure_dt(&rf_reset, GPIO_OUTPUT_ACTIVE);
	err |= gpio_pin_configure_dt(&rf_irq, GPIO_INPUT | GPIO_PULL_DOWN);
	err |= gpio_pin_configure_dt(&rf_csn, GPIO_OUTPUT_ACTIVE);
	err |= gpio_pin_configure_dt(&work_led, GPIO_OUTPUT_INACTIVE);
	if (err != 0) return -EIO;
	gpio_init_callback(&ads_drdy_callback, ads_drdy_handler, BIT(ads_drdy.pin));
	err = gpio_add_callback(ads_drdy.port, &ads_drdy_callback);
	if (err != 0) return err;
	return gpio_pin_interrupt_configure_dt(&ads_drdy, GPIO_INT_EDGE_TO_ACTIVE);
}

int main(void)
{
	uint8_t ads_frame[ADS_FRAME_SIZE];
	uint8_t stream_frame[STREAM_FRAME_SIZE];
	uint32_t next_report_ms;

	if (!device_is_ready(nsc_pwm.dev) || !device_is_ready(ads_spi) ||
	    !device_is_ready(rf_spi) ||
	    configure_gpio() != 0) {
		LOG_ERR("PWM, SPI1, SPI3, or GPIO initialization failed");
		return -ENODEV;
	}
	int err = pwm_set_dt(&nsc_pwm, NSC_PWM_PERIOD_NS, NSC_PWM_PULSE_NS);
	if (err != 0) return err;
	LOG_INF("NSC1002 PE5 PWM: 200 kHz, 50%%");
	k_msleep(1500U);
	gpio_pin_set_raw(rf_reset.port, rf_reset.pin, 0);
	k_msleep(10U);
	gpio_pin_set_raw(rf_reset.port, rf_reset.pin, 1);
	k_msleep(100U);

	if (device_is_ready(usb_cdc)) {
		err = usb_enable(NULL);
		if (err != 0) LOG_ERR("USB CDC enable failed: %d", err);
	}
	while (!ads_initialize()) {
		LOG_ERR("ADS1299 register verification failed; retrying");
		k_sleep(K_SECONDS(2));
	}
	if (!boot_is_img_confirmed()) {
		err = boot_write_img_confirmed();
		if (err != 0) {
			LOG_ERR("MCUboot image confirmation failed: %d", err);
		} else {
			LOG_INF("MCUboot image confirmed after ADS1299 self-check");
		}
	}
	ads_start_streaming();
	LOG_INF("ADS1299 V19 control ready: 250/500/1000 SPS, SRB1, runtime channel/gain/BIAS/LOFF/modes");
	next_report_ms = k_uptime_get_32() + 5000U;

	while (true) {
		if (numeric_length != 0U &&
		    (k_uptime_get_32() - numeric_last_ms) >= 30U) {
			flush_numeric_command();
		}

		if (!streaming_enabled) {
			service_control_link();
			k_msleep(3U);
		} else if (k_sem_take(&ads_drdy_sem, K_MSEC(20)) == 0) {
			bool drdy_was_low = gpio_pin_get_raw(ads_drdy.port, ads_drdy.pin) == 0;
			uint32_t start_cycles = k_cycle_get_32();
			if (ads_read_frame(ads_frame)) {
				uint32_t read_us = k_cyc_to_us_floor32(k_cycle_get_32() - start_cycles);
				if (read_us > UINT16_MAX) read_us = UINT16_MAX;
				ads_frames++;
				build_stream_frame(stream_frame, ads_frame, drdy_was_low,
						   (uint16_t)read_us);
				err = rf_exchange(stream_frame);
				if (err == 0) {
					rf_frames++;
					last_successful_frame_ms = k_uptime_get_32();
					work_led_set(true);
				} else {
					rf_errors++;
				}
				if (usb_host_ready()) {
					usb_send_frame(stream_frame);
					usb_frames++;
				}
			}
		} else {
			service_control_link();
		}

		for (uint8_t i = 0U; i < 4U &&
		     k_msgq_num_used_get(&control_reply_queue) != 0U; ++i) {
			service_control_link();
		}

		uint32_t now = k_uptime_get_32();
		work_led_update(now);
		if ((int32_t)(now - next_report_ms) >= 0) {
			LOG_INF("frames=%u rf=%u err=%u usb=%u ctrl=%u reply=%u run=%d rate=%u mode=%u mask=%02x bias=%02x",
				ads_frames, rf_frames, rf_errors, usb_frames,
				control_commands, control_replies, streaming_enabled,
				current_sample_rate_hz, current_mode,
				current_enabled_mask, current_bias_mask);
			next_report_ms += 5000U;
		}
	}
}
