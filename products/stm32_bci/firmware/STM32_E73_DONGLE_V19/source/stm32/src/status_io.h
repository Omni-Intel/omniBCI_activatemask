#ifndef BCI_STATUS_IO_H
#define BCI_STATUS_IO_H

#include <stdint.h>

#include "feature_logic.h"

int status_io_init(void);
void status_led_set_state(enum bci_led_state state);
void status_led_note_frame_success(void);
uint32_t status_trigger_count(void);
uint32_t status_trigger_drop_count(void);
struct status_record_event {
	uint32_t number;
	int64_t uptime_ms;
};
bool status_take_record_event(struct status_record_event *event);
void status_discard_record_events(void);

#endif
