#ifndef BCIBAND_EVENT_PACKET_H_
#define BCIBAND_EVENT_PACKET_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BCI_PACKET_SIZE       48U
#define BCI_PACKET_SYNC0      0xA5U
#define BCI_PACKET_SYNC1      0x5AU
#define BCI_PACKET_VERSION    1U
#define BCI_PACKET_TYPE_EEG   1U
#define BCI_PACKET_TYPE_EVENT 2U

static inline uint16_t bci_event_crc16(const uint8_t *data, size_t length)
{
	uint16_t crc = 0xFFFFU;

	for (size_t i = 0; i < length; ++i) {
		crc ^= (uint16_t)data[i] << 8;
		for (uint8_t bit = 0; bit < 8U; ++bit) {
			crc = (crc & 0x8000U) ?
				(uint16_t)((crc << 1) ^ 0x1021U) :
				(uint16_t)(crc << 1);
		}
	}
	return crc;
}

static inline void bci_event_put_u32(uint8_t *dst, uint32_t value)
{
	dst[0] = (uint8_t)value;
	dst[1] = (uint8_t)(value >> 8);
	dst[2] = (uint8_t)(value >> 16);
	dst[3] = (uint8_t)(value >> 24);
}

static inline void bci_event_put_u64(uint8_t *dst, uint64_t value)
{
	bci_event_put_u32(dst, (uint32_t)value);
	bci_event_put_u32(dst + 4, (uint32_t)(value >> 32));
}

static inline bool bci_event_packet_is_valid(const uint8_t *packet, size_t length)
{
	if (packet == NULL || length != BCI_PACKET_SIZE ||
	    packet[0] != BCI_PACKET_SYNC0 || packet[1] != BCI_PACKET_SYNC1 ||
	    packet[2] != BCI_PACKET_VERSION ||
	    packet[3] != BCI_PACKET_TYPE_EVENT) {
		return false;
	}
	uint16_t received = (uint16_t)packet[46] |
		((uint16_t)packet[47] << 8);
	return received == bci_event_crc16(packet, 46U);
}

static inline void bci_event_packet_build(uint8_t packet[BCI_PACKET_SIZE],
					  uint32_t event_sequence,
					  uint64_t event_start_us,
					  uint8_t event_id,
					  uint32_t anchor_frame_sequence,
					  uint32_t anchor_frame_timestamp)
{
	memset(packet, 0, BCI_PACKET_SIZE);
	packet[0] = BCI_PACKET_SYNC0;
	packet[1] = BCI_PACKET_SYNC1;
	packet[2] = BCI_PACKET_VERSION;
	packet[3] = BCI_PACKET_TYPE_EVENT;
	bci_event_put_u32(packet + 4, event_sequence);
	bci_event_put_u64(packet + 8, event_start_us);
	packet[16] = event_id;
	packet[17] = 1U; /* event_valid: packet existence also implies an event */
	bci_event_put_u32(packet + 18, anchor_frame_sequence);
	bci_event_put_u32(packet + 22, anchor_frame_timestamp);
	uint16_t crc = bci_event_crc16(packet, 46U);
	packet[46] = (uint8_t)crc;
	packet[47] = (uint8_t)(crc >> 8);
}

#endif
