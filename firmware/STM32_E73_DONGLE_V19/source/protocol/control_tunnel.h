#ifndef BCIBAND_CONTROL_TUNNEL_H_
#define BCIBAND_CONTROL_TUNNEL_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BCI_TUNNEL_MAGIC0       0xC3U
#define BCI_TUNNEL_MAGIC1       0x3CU
#define BCI_TUNNEL_VERSION      1U
#define BCI_TUNNEL_TYPE_COMMAND 1U
#define BCI_TUNNEL_TYPE_REPLY   2U
#define BCI_TUNNEL_TYPE_POLL    3U
#define BCI_TUNNEL_MAX_PAYLOAD  38U
#define BCI_TUNNEL_MAX_PACKET   47U

struct bci_tunnel_view {
	uint8_t type;
	uint16_t sequence;
	uint8_t length;
	const uint8_t *payload;
	size_t packet_length;
};

static inline uint16_t bci_crc16_ccitt_false(const uint8_t *data, size_t len)
{
	uint16_t crc = 0xFFFFU;

	for (size_t i = 0; i < len; ++i) {
		crc ^= (uint16_t)data[i] << 8;
		for (uint8_t bit = 0; bit < 8U; ++bit) {
			crc = (crc & 0x8000U) ?
				(uint16_t)((crc << 1) ^ 0x1021U) :
				(uint16_t)(crc << 1);
		}
	}
	return crc;
}

static inline size_t bci_tunnel_encode(uint8_t *destination, uint8_t type,
				       uint16_t sequence,
				       const uint8_t *payload, uint8_t length)
{
	if (destination == NULL || length > BCI_TUNNEL_MAX_PAYLOAD ||
	    (length != 0U && payload == NULL)) {
		return 0U;
	}
	destination[0] = BCI_TUNNEL_MAGIC0;
	destination[1] = BCI_TUNNEL_MAGIC1;
	destination[2] = BCI_TUNNEL_VERSION;
	destination[3] = type;
	destination[4] = (uint8_t)sequence;
	destination[5] = (uint8_t)(sequence >> 8);
	destination[6] = length;
	if (length != 0U) {
		memcpy(destination + 7, payload, length);
	}
	uint16_t crc = bci_crc16_ccitt_false(destination, 7U + length);
	destination[7U + length] = (uint8_t)crc;
	destination[8U + length] = (uint8_t)(crc >> 8);
	return 9U + length;
}

static inline bool bci_tunnel_decode(const uint8_t *packet, size_t available,
				     struct bci_tunnel_view *view)
{
	if (packet == NULL || view == NULL || available < 9U ||
	    packet[0] != BCI_TUNNEL_MAGIC0 || packet[1] != BCI_TUNNEL_MAGIC1 ||
	    packet[2] != BCI_TUNNEL_VERSION ||
	    packet[6] > BCI_TUNNEL_MAX_PAYLOAD) {
		return false;
	}
	size_t total = 9U + packet[6];
	if (available < total) {
		return false;
	}
	uint16_t received = (uint16_t)packet[7U + packet[6]] |
		((uint16_t)packet[8U + packet[6]] << 8);
	if (received != bci_crc16_ccitt_false(packet, 7U + packet[6])) {
		return false;
	}
	view->type = packet[3];
	view->sequence = (uint16_t)packet[4] | ((uint16_t)packet[5] << 8);
	view->length = packet[6];
	view->payload = packet + 7;
	view->packet_length = total;
	return true;
}

#endif
