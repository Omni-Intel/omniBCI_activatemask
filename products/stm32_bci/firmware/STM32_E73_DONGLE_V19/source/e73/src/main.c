/*
 * E73-2G4M08S1C -> original EWT nRF52840 USB dongle bridge.
 *
 * Normal build: STM32 SPI master -> E73 SPIS -> Nordic ESB PTX.
 * Self-test build: generate valid 48-byte ADS1299 records internally.
 *
 * The ESB radio tuple and 48-byte record layout intentionally match the
 * previously validated EWT dongle implementation byte-for-byte.
 */

#include <errno.h>
#include <string.h>

#include <esb.h>
#include <hal/nrf_gpio.h>
#include <nrfx_spis.h>
#include <zephyr/irq.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#include "control_tunnel.h"

LOG_MODULE_REGISTER(e73_dongle, LOG_LEVEL_INF);

#define FRAME_SIZE            48U
#define FRAME_QUEUE_DEPTH     64U

/* Current PCB: E73 pad 33/32/30/34. */
#define PIN_SCK               NRF_GPIO_PIN_MAP(0, 13)
#define PIN_MOSI              NRF_GPIO_PIN_MAP(0, 20)
#define PIN_MISO              NRF_GPIO_PIN_MAP(0, 17)
#define PIN_CSN               NRF_GPIO_PIN_MAP(0, 22)

#define ESB_RF_CHANNEL        80U
#define ESB_PIPE               0U

static const uint8_t esb_base_addr_0[4] = {0x45, 0x57, 0x54, 0x73};
static const uint8_t esb_base_addr_1[4] = {0x12, 0x99, 0x24, 0x08};
static const uint8_t esb_prefixes[8] = {
	0xA5, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8
};

static nrfx_spis_t spis = NRFX_SPIS_INSTANCE(NRF_SPIS2);
static uint8_t spis_rx[FRAME_SIZE];
static uint8_t spis_tx[FRAME_SIZE];

struct radio_packet {
	uint8_t length;
	uint8_t data[FRAME_SIZE];
};

K_MSGQ_DEFINE(frame_queue, sizeof(struct radio_packet), FRAME_QUEUE_DEPTH, 4);
K_MSGQ_DEFINE(command_queue, sizeof(struct radio_packet), 32, 4);
K_SEM_DEFINE(esb_tx_done, 0, 1);

static atomic_t spi_valid_count;
static atomic_t spi_bad_count;
static atomic_t spi_drop_count;
static atomic_t spi_last_rx_amount;
static atomic_t esb_ok_count;
static atomic_t esb_fail_count;
static atomic_t esb_tx_failed_latched;
static atomic_t command_rx_count;
static atomic_t command_spi_count;
static atomic_t reply_spi_count;

static uint16_t crc16_ccitt_false(const uint8_t *data, size_t len)
{
	uint16_t crc = 0xFFFF;

	for (size_t i = 0; i < len; ++i) {
		crc ^= (uint16_t)data[i] << 8;
		for (uint8_t bit = 0; bit < 8; ++bit) {
			crc = (crc & 0x8000U) ?
				(uint16_t)((crc << 1) ^ 0x1021U) :
				(uint16_t)(crc << 1);
		}
	}
	return crc;
}

static bool frame_is_valid(const uint8_t *frame, size_t len)
{
	if (len != FRAME_SIZE || frame[0] != 0xA5 || frame[1] != 0x5A ||
	    frame[2] != 1U || frame[3] != 1U) {
		return false;
	}

	uint16_t received_crc = (uint16_t)frame[46] |
				((uint16_t)frame[47] << 8);
	return received_crc == crc16_ccitt_false(frame, 46);
}

#ifdef EWT_SELF_TEST
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

static void put_s24_be(uint8_t *p, int32_t value)
{
	uint32_t raw = (uint32_t)value & 0xFFFFFFU;
	p[0] = (uint8_t)(raw >> 16);
	p[1] = (uint8_t)(raw >> 8);
	p[2] = (uint8_t)raw;
}

static int32_t simulated_channel(uint8_t channel, uint32_t sample)
{
	static const int32_t pattern[16] = {
		0, 42000, 78000, 98000, 78000, 42000, 0, -42000,
		-78000, -98000, -78000, -42000, 0, 42000, 78000, 98000
	};
	return (int32_t)(channel + 1U) * 1200L +
		pattern[(sample + channel * 3U) & 0x0FU];
}

static void make_test_frame(uint8_t *frame, uint32_t sequence)
{
	memset(frame, 0, FRAME_SIZE);
	frame[0] = 0xA5;
	frame[1] = 0x5A;
	frame[2] = 1;
	frame[3] = 1;
	put_u32_le(frame + 4, sequence);
	put_u32_le(frame + 8, k_uptime_get_32() * 1000U);
	frame[12] = 0xC0;
	for (uint8_t channel = 0; channel < 8; ++channel) {
		put_s24_be(frame + 16 + channel * 3,
			     simulated_channel(channel, sequence));
	}
	put_u16_le(frame + 40, 1000U);
	put_u16_le(frame + 46, crc16_ccitt_false(frame, 46));
}
#endif

static void prepare_spi_downlink(void)
{
	struct radio_packet command;

	memset(spis_tx, 0, sizeof(spis_tx));
	if (k_msgq_get(&command_queue, &command, K_NO_WAIT) == 0) {
		memcpy(spis_tx, command.data, command.length);
		atomic_inc(&command_spi_count);
		return;
	}
	spis_tx[0] = 0xE7;
	spis_tx[1] = 0x73;
	spis_tx[2] = 1;
	spis_tx[3] = FRAME_SIZE;
	spis_tx[4] = (uint8_t)atomic_get(&spi_valid_count);
	spis_tx[5] = (uint8_t)atomic_get(&spi_bad_count);
	spis_tx[6] = (uint8_t)atomic_get(&spi_drop_count);
	spis_tx[7] = (uint8_t)atomic_get(&esb_ok_count);
	spis_tx[8] = (uint8_t)atomic_get(&esb_fail_count);
	spis_tx[9] = (uint8_t)atomic_get(&spi_last_rx_amount);
}

static int arm_spis(void)
{
	prepare_spi_downlink();
	int err = nrfx_spis_buffers_set(&spis, spis_tx, sizeof(spis_tx),
					spis_rx, sizeof(spis_rx));
	if (err != 0) {
		LOG_ERR("SPIS buffers_set failed: %d", err);
	}
	return err;
}

static void spis_handler(const nrfx_spis_event_t *event, void *context)
{
	struct radio_packet packet;
	struct bci_tunnel_view tunnel;

	ARG_UNUSED(context);

	if (event->evt_type != NRFX_SPIS_XFER_DONE) {
		return;
	}
	atomic_set(&spi_last_rx_amount, event->rx_amount);

	if (frame_is_valid(event->p_rx_buf, event->rx_amount)) {
		packet.length = FRAME_SIZE;
		memcpy(packet.data, event->p_rx_buf, FRAME_SIZE);
		if (k_msgq_put(&frame_queue, &packet, K_NO_WAIT) == 0) {
			atomic_inc(&spi_valid_count);
		} else {
			atomic_inc(&spi_drop_count);
		}
	} else if (bci_tunnel_decode(event->p_rx_buf, event->rx_amount, &tunnel) &&
		   tunnel.type == BCI_TUNNEL_TYPE_REPLY) {
		packet.length = (uint8_t)tunnel.packet_length;
		memcpy(packet.data, event->p_rx_buf, packet.length);
		if (k_msgq_put(&frame_queue, &packet, K_NO_WAIT) == 0) {
			atomic_inc(&reply_spi_count);
		} else {
			atomic_inc(&spi_drop_count);
		}
	} else if (bci_tunnel_decode(event->p_rx_buf, event->rx_amount, &tunnel) &&
		   tunnel.type == BCI_TUNNEL_TYPE_POLL) {
		/* A stopped STM32 polls the full-duplex link without radio output. */
	} else {
		atomic_inc(&spi_bad_count);
	}

	if (arm_spis() != 0) {
		atomic_inc(&spi_drop_count);
	}
}

static void radio_event_handler(const struct esb_evt *event)
{
	struct esb_payload payload;
	struct bci_tunnel_view tunnel;
	struct radio_packet command;

	switch (event->evt_id) {
	case ESB_EVENT_TX_SUCCESS:
		atomic_inc(&esb_ok_count);
		k_sem_give(&esb_tx_done);
		break;
	case ESB_EVENT_TX_FAILED:
		atomic_inc(&esb_fail_count);
		atomic_set(&esb_tx_failed_latched, 1);
		k_sem_give(&esb_tx_done);
		break;
	case ESB_EVENT_RX_RECEIVED:
		while (esb_read_rx_payload(&payload) == 0) {
			if (!bci_tunnel_decode(payload.data, payload.length, &tunnel) ||
			    tunnel.type != BCI_TUNNEL_TYPE_COMMAND) {
				continue;
			}
			command.length = (uint8_t)tunnel.packet_length;
			memcpy(command.data, payload.data, command.length);
			if (k_msgq_put(&command_queue, &command, K_NO_WAIT) == 0) {
				atomic_inc(&command_rx_count);
			} else {
				atomic_inc(&spi_drop_count);
			}
		}
		break;
#if IS_ENABLED(CONFIG_ESB_MPSL_TIMESLOT)
	case ESB_EVENT_TIMESLOT_FAILED:
		atomic_inc(&esb_fail_count);
		atomic_set(&esb_tx_failed_latched, 1);
		k_sem_give(&esb_tx_done);
		break;
#endif
	}
}

static int esb_initialize(void)
{
	struct esb_config config = ESB_DEFAULT_CONFIG;
	int err;

	config.protocol = ESB_PROTOCOL_ESB_DPL;
	config.mode = ESB_MODE_PTX;
	config.bitrate = ESB_BITRATE_1MBPS;
	config.crc = ESB_CRC_16BIT;
	config.retransmit_count = 1;
	config.retransmit_delay = 500;
	config.payload_length = FRAME_SIZE;
	config.tx_output_power = ESB_TX_POWER_8DBM;
	config.use_fast_ramp_up = true;
	config.selective_auto_ack = false;
	config.event_handler = radio_event_handler;

	err = esb_init(&config);
	if (err) {
		return err;
	}
	err = esb_set_base_address_0(esb_base_addr_0);
	if (err) {
		return err;
	}
	err = esb_set_base_address_1(esb_base_addr_1);
	if (err) {
		return err;
	}
	err = esb_set_prefixes(esb_prefixes, ARRAY_SIZE(esb_prefixes));
	if (err) {
		return err;
	}
	return esb_set_rf_channel(ESB_RF_CHANNEL);
}

#ifndef EWT_SELF_TEST
static int spis_initialize(void)
{
	nrfx_spis_config_t config = NRFX_SPIS_DEFAULT_CONFIG(
		PIN_SCK, PIN_MOSI, PIN_MISO, PIN_CSN);

	config.mode = NRF_SPIS_MODE_0;
	config.bit_order = NRF_SPIS_BIT_ORDER_MSB_FIRST;
	config.csn_pullup = NRF_GPIO_PIN_PULLUP;
	config.miso_drive = NRF_GPIO_PIN_S0S1;

	IRQ_CONNECT(NRFX_IRQ_NUMBER_GET(NRF_SPIS2), 5,
		    nrfx_spis_irq_handler, &spis, 0);

	int err = nrfx_spis_init(&spis, &config, spis_handler, NULL);
	if (err) {
		LOG_ERR("nrfx_spis_init failed: %d", err);
		return err;
	}

	err = arm_spis();
	if (err == 0) {
		LOG_INF("SPIS2 ready: SCK=P0.13 MOSI=P0.20 MISO=P0.17 CS=P0.22");
	}
	return err;
}
#endif

int main(void)
{
	struct radio_packet packet;
	struct esb_payload payload = {0};
	uint32_t last_report_ms = 0;
	uint16_t poll_sequence = 0;
#ifdef EWT_SELF_TEST
	uint32_t self_test_sequence = 0;
	uint32_t next_test_ms = 0;
#endif
	int err = esb_initialize();

	LOG_INF("E73 dongle-compatible ESB: 1Mbps ch=%u pipe=%u frame=%u",
		ESB_RF_CHANNEL, ESB_PIPE, FRAME_SIZE);
	if (err) {
		LOG_ERR("ESB init failed: %d", err);
		return err;
	}

#ifndef EWT_SELF_TEST
	err = spis_initialize();
	if (err) {
		return err;
	}
#endif

	payload.length = FRAME_SIZE;
	payload.pipe = ESB_PIPE;
	payload.noack = false;

#ifdef EWT_SELF_TEST
	LOG_INF("SELF-TEST: valid 48-byte frames at 1000 Hz");
	next_test_ms = k_uptime_get_32();
#else
	LOG_INF("BRIDGE: waiting for valid 48-byte STM32 SPI frames");
#endif

	while (1) {
#ifdef EWT_SELF_TEST
		uint32_t now = k_uptime_get_32();
		if ((int32_t)(now - next_test_ms) < 0) {
			k_sleep(K_MSEC(next_test_ms - now));
		}
		make_test_frame(packet.data, self_test_sequence++);
		packet.length = FRAME_SIZE;
		next_test_ms += 1U;
		if ((int32_t)(k_uptime_get_32() - next_test_ms) > 40) {
			next_test_ms = k_uptime_get_32() + 1U;
		}
		bool have_frame = true;
#else
		bool have_frame =
			(k_msgq_get(&frame_queue, &packet, K_MSEC(10)) == 0);
#endif
		if (!have_frame) {
			packet.length = (uint8_t)bci_tunnel_encode(
				packet.data, BCI_TUNNEL_TYPE_POLL,
				poll_sequence++, NULL, 0U);
			have_frame = true;
		}
		if (have_frame) {
			memcpy(payload.data, packet.data, packet.length);
			payload.length = packet.length;
			atomic_set(&esb_tx_failed_latched, 0);
			err = esb_write_payload(&payload);
			if (err) {
				atomic_inc(&esb_fail_count);
			} else {
				k_sem_take(&esb_tx_done, K_FOREVER);
				if (atomic_cas(&esb_tx_failed_latched, 1, 0)) {
					err = esb_flush_tx();
					if (err) {
						atomic_inc(&esb_fail_count);
					}
				}
			}
		}

		uint32_t report_now = k_uptime_get_32();
		if ((report_now - last_report_ms) >= 5000U) {
			last_report_ms = report_now;
			LOG_INF("stats spi_ok=%ld bad=%ld drop=%ld radio_ok=%ld fail=%ld cmd_rx=%ld cmd_spi=%ld reply=%ld",
				(long)atomic_get(&spi_valid_count),
				(long)atomic_get(&spi_bad_count),
				(long)atomic_get(&spi_drop_count),
				(long)atomic_get(&esb_ok_count),
				(long)atomic_get(&esb_fail_count),
				(long)atomic_get(&command_rx_count),
				(long)atomic_get(&command_spi_count),
				(long)atomic_get(&reply_spi_count));
		}
	}
}
