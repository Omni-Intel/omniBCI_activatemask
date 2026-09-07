#ifndef BCI_SD_RECORDER_H
#define BCI_SD_RECORDER_H

#include <stdbool.h>
#include <stdint.h>

#include "feature_logic.h"

struct sd_recorder_stats {
	bool stopped;
	int32_t close_error;
	bool mounted;
	bool recording;
	uint64_t bytes_written;
	uint32_t frames_dropped;
	int32_t last_error;
};

struct sd_record_config {
	uint16_t sample_rate;
	uint8_t gains[8];
	uint8_t enabled_mask;
	uint8_t bias_mask;
	uint8_t mode;
};

int sd_recorder_init(void);
int sd_recorder_start(const struct sd_record_config *config);
void sd_recorder_take_control(void);
void sd_recorder_event(uint32_t number, uint8_t code, uint32_t next_sequence,
			       uint64_t start_us, int64_t uptime_ms);
void sd_recorder_submit(const uint8_t frame[BCI_STREAM_FRAME_SIZE]);
int sd_recorder_stop(void);
void sd_recorder_get_stats(struct sd_recorder_stats *stats);

#endif
