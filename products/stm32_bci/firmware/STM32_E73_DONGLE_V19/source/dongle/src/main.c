#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <esb.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

#include "control_tunnel.h"
#include "event_packet.h"

LOG_MODULE_REGISTER(bciband_dongle, LOG_LEVEL_INF);

#define FRAME_SIZE       48U
#define USB_QUEUE_DEPTH  96U
#define CMD_QUEUE_DEPTH  32U
#define ESB_RF_CHANNEL   80U
#define ESB_PIPE         0U

static const uint8_t esb_base_addr_0[4] = {0x45, 0x57, 0x54, 0x73};
static const uint8_t esb_base_addr_1[4] = {0x12, 0x99, 0x24, 0x08};
static const uint8_t esb_prefixes[8] = {
	0xA5, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8
};

struct usb_packet {
	uint8_t length;
	uint8_t data[FRAME_SIZE];
};

struct command_chunk {
	uint8_t length;
	uint8_t data[BCI_TUNNEL_MAX_PAYLOAD];
};

K_MSGQ_DEFINE(usb_queue, sizeof(struct usb_packet), USB_QUEUE_DEPTH, 4);
K_MSGQ_DEFINE(command_queue, sizeof(struct command_chunk), CMD_QUEUE_DEPTH, 4);

static const struct device *const cdc_dev =
	DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
static atomic_t radio_count;
static atomic_t valid_count;
static atomic_t invalid_count;
static atomic_t queue_drop_count;
static atomic_t usb_count;
static atomic_t usb_command_count;
static atomic_t radio_command_count;
static atomic_t command_drop_count;
static uint16_t command_sequence;

static bool frame_valid(const uint8_t *frame)
{
	uint16_t expected;

	if (frame[0] != 0xA5U || frame[1] != 0x5AU ||
	    frame[2] != 0x01U || frame[3] != 0x01U) {
		return false;
	}
	expected = (uint16_t)frame[46] | ((uint16_t)frame[47] << 8);
	return bci_crc16_ccitt_false(frame, 46U) == expected;
}

static void queue_usb_bytes(const uint8_t *data, uint8_t length)
{
	struct usb_packet packet = {.length = length};

	if (length == 0U || length > sizeof(packet.data)) {
		return;
	}
	memcpy(packet.data, data, length);
	if (k_msgq_put(&usb_queue, &packet, K_NO_WAIT) != 0) {
		atomic_inc(&queue_drop_count);
	}
}

static void radio_event_handler(const struct esb_evt *event)
{
	struct esb_payload payload;
	struct bci_tunnel_view tunnel;

	if (event->evt_id != ESB_EVENT_RX_RECEIVED) {
		return;
	}

	while (esb_read_rx_payload(&payload) == 0) {
		atomic_inc(&radio_count);
		if (payload.length == FRAME_SIZE &&
		    (frame_valid(payload.data) ||
		     bci_event_packet_is_valid(payload.data, payload.length))) {
			atomic_inc(&valid_count);
			queue_usb_bytes(payload.data, FRAME_SIZE);
		} else if (bci_tunnel_decode(payload.data, payload.length, &tunnel) &&
			   tunnel.type == BCI_TUNNEL_TYPE_REPLY) {
			atomic_inc(&radio_command_count);
			queue_usb_bytes(tunnel.payload, tunnel.length);
		} else if (!(bci_tunnel_decode(payload.data, payload.length, &tunnel) &&
			     tunnel.type == BCI_TUNNEL_TYPE_POLL)) {
			atomic_inc(&invalid_count);
		}
	}
}

static int esb_initialize(void)
{
	struct esb_config config = ESB_DEFAULT_CONFIG;
	int err;

	config.protocol = ESB_PROTOCOL_ESB_DPL;
	config.mode = ESB_MODE_PRX;
	config.bitrate = ESB_BITRATE_1MBPS;
	config.crc = ESB_CRC_16BIT;
	config.payload_length = FRAME_SIZE;
	config.selective_auto_ack = false;
	config.event_handler = radio_event_handler;

	err = esb_init(&config);
	if (err) return err;
	err = esb_set_base_address_0(esb_base_addr_0);
	if (err) return err;
	err = esb_set_base_address_1(esb_base_addr_1);
	if (err) return err;
	err = esb_set_prefixes(esb_prefixes, ARRAY_SIZE(esb_prefixes));
	if (err) return err;
	err = esb_set_rf_channel(ESB_RF_CHANNEL);
	if (err) return err;
	return esb_start_rx();
}

static bool usb_host_ready(void)
{
	uint32_t dtr = 0U;

	return device_is_ready(cdc_dev) &&
		uart_line_ctrl_get(cdc_dev, UART_LINE_CTRL_DTR, &dtr) == 0 && dtr != 0U;
}

static void cdc_irq_handler(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	struct command_chunk chunk;

	while (uart_irq_update(dev) && uart_irq_rx_ready(dev)) {
		int received = uart_fifo_read(dev, chunk.data, sizeof(chunk.data));
		if (received <= 0) {
			break;
		}
		chunk.length = (uint8_t)received;
		atomic_add(&usb_command_count, received);
		if (k_msgq_put(&command_queue, &chunk, K_NO_WAIT) != 0) {
			atomic_inc(&command_drop_count);
		}
	}
}

static void service_command_ack_payload(void)
{
	static bool have_pending;
	static struct command_chunk pending;
	uint8_t encoded[BCI_TUNNEL_MAX_PACKET];
	struct esb_payload payload = {0};

	if (!have_pending &&
	    k_msgq_get(&command_queue, &pending, K_NO_WAIT) == 0) {
		have_pending = true;
	}
	if (!have_pending) {
		return;
	}

	size_t length = bci_tunnel_encode(encoded, BCI_TUNNEL_TYPE_COMMAND,
					  command_sequence, pending.data,
					  pending.length);
	payload.length = (uint8_t)length;
	payload.pipe = ESB_PIPE;
	payload.noack = false;
	memcpy(payload.data, encoded, length);
	int err = esb_write_payload(&payload);
	if (err == 0) {
		command_sequence++;
		have_pending = false;
	} else if (err != -ENOMEM) {
		atomic_inc(&command_drop_count);
		have_pending = false;
	}
}

int main(void)
{
	struct usb_packet packet;
	uint32_t last_report_ms = 0U;

	if (!device_is_ready(cdc_dev) || esb_initialize() != 0) {
		LOG_ERR("USB CDC or ESB initialization failed");
		return 0;
	}
	if (uart_irq_callback_user_data_set(cdc_dev, cdc_irq_handler, NULL) != 0) {
		LOG_ERR("USB CDC RX callback setup failed");
		return 0;
	}
	uart_irq_rx_enable(cdc_dev);
	LOG_INF("BCI-Band dongle: bidirectional CDC <-> ESB, 1Mbps ch80");

	while (true) {
		service_command_ack_payload();
		if (k_msgq_get(&usb_queue, &packet, K_MSEC(2)) == 0 &&
		    usb_host_ready()) {
			for (uint8_t i = 0; i < packet.length; ++i) {
				uart_poll_out(cdc_dev, packet.data[i]);
			}
			atomic_inc(&usb_count);
		}

		uint32_t now = k_uptime_get_32();
		if ((now - last_report_ms) >= 5000U) {
			last_report_ms = now;
			LOG_INF("radio=%ld data=%ld bad=%ld usb=%ld cmd_usb=%ld cmd_rf=%ld drop=%ld",
				(long)atomic_get(&radio_count),
				(long)atomic_get(&valid_count),
				(long)atomic_get(&invalid_count),
				(long)atomic_get(&usb_count),
				(long)atomic_get(&usb_command_count),
				(long)atomic_get(&radio_command_count),
				(long)atomic_get(&command_drop_count));
		}
	}
}
