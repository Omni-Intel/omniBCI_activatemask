#include "status_io.h"

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

LOG_MODULE_REGISTER(bci_status_io, LOG_LEVEL_INF);

#define STATUS_THREAD_STACK_SIZE 1024U
#define STATUS_THREAD_PRIORITY 8
#define STATUS_UPDATE_MS 50U
#define TRIGGER_EVENT_DEPTH 32U
#define EVENT_UART_FRAME_US 1000U

struct trigger_event {
	uint32_t number;
	uint8_t code;
	uint64_t start_us;
};

static const struct gpio_dt_spec work_led =
	GPIO_DT_SPEC_GET(DT_NODELABEL(bci_control), work_led_gpios);
static const struct device *const event_uart =
	DEVICE_DT_GET(DT_NODELABEL(usart1));

K_MSGQ_DEFINE(trigger_events, sizeof(struct trigger_event),
	      TRIGGER_EVENT_DEPTH, 4);
K_MSGQ_DEFINE(record_events, sizeof(struct status_record_event), TRIGGER_EVENT_DEPTH, 4);
K_THREAD_STACK_DEFINE(status_stack, STATUS_THREAD_STACK_SIZE);
static struct k_thread status_thread_data;
static atomic_t led_state = ATOMIC_INIT(BCI_LED_BOOT);
static atomic_t led_state_started_ms;
static atomic_t frame_seen;
static atomic_t last_success_ms;
static atomic_t trigger_count;
static atomic_t trigger_drops;
static uint64_t event_start_time_us(uint64_t rx_time_us, uint8_t index,
					uint8_t count)
{
	/* UART RX interrupt is observed at the end of the byte. */
	uint32_t correction = ((uint32_t)(count - index)) * EVENT_UART_FRAME_US;
	return (uint64_t)rx_time_us - correction;
}

static void enqueue_event(uint8_t code, uint64_t start_us)
{
	struct trigger_event event = {
		.number = (uint32_t)atomic_inc(&trigger_count) + 1U,
		.code = code,
		.start_us = start_us,
	};
	if (k_msgq_put(&trigger_events, &event, K_NO_WAIT) != 0) {
		atomic_inc(&trigger_drops);
	}
	if (atomic_get(&led_state) == BCI_LED_ACQUIRING) {
		struct status_record_event record = {
			.number = event.number,
			.code = event.code,
			.start_us = event.start_us,
			.uptime_ms = (int64_t)(event.start_us / 1000U),
		};
		if (k_msgq_put(&record_events, &record, K_NO_WAIT)) atomic_inc(&trigger_drops);
	}
}

static void event_uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	uint8_t data[16];

	while (uart_irq_update(dev) && uart_irq_rx_ready(dev)) {
		int count = uart_fifo_read(dev, data, sizeof(data));
		if (count <= 0) {
			break;
		}
		uint64_t rx_time_us = k_cyc_to_us_floor64(k_cycle_get_64());
		for (int i = 0; i < count; ++i) {
			enqueue_event(data[i], event_start_time_us(rx_time_us,
				(uint8_t)i, (uint8_t)count));
		}
	}
}

bool status_take_record_event(struct status_record_event *event)
{
	return k_msgq_get(&record_events, event, K_NO_WAIT) == 0;
}

void status_discard_record_events(void)
{
	k_msgq_purge(&record_events);
}

static void status_thread(void *unused1, void *unused2, void *unused3)
{
	ARG_UNUSED(unused1);
	ARG_UNUSED(unused2);
	ARG_UNUSED(unused3);

	while (true) {
		struct trigger_event event;
		while (k_msgq_get(&trigger_events, &event, K_NO_WAIT) == 0) {
			LOG_INF("EXT_TRIG event=%u code=%u start_us=%llu",
				event.number, event.code,
				(unsigned long long)event.start_us);
		}

		uint32_t now = k_uptime_get_32();
		enum bci_led_state state =
			(enum bci_led_state)atomic_get(&led_state);
		bool on;
		if (state == BCI_LED_ACQUIRING) {
			on = atomic_get(&frame_seen) != 0 &&
				bci_acquisition_healthy(
					now, (uint32_t)atomic_get(&last_success_ms));
		} else {
			on = bci_led_output(
				state,
				now - (uint32_t)atomic_get(&led_state_started_ms));
		}
		(void)gpio_pin_set_dt(&work_led, on ? 1 : 0);
		k_msleep(STATUS_UPDATE_MS);
	}
}

int status_io_init(void)
{
	if (!gpio_is_ready_dt(&work_led) || !device_is_ready(event_uart)) {
		return -ENODEV;
	}
	int err = gpio_pin_configure_dt(&work_led, GPIO_OUTPUT_INACTIVE);
	if (err != 0) {
		return err;
	}
	atomic_set(&led_state_started_ms, (atomic_val_t)k_uptime_get_32());
	k_thread_create(&status_thread_data, status_stack,
			K_THREAD_STACK_SIZEOF(status_stack), status_thread,
			NULL, NULL, NULL, STATUS_THREAD_PRIORITY, 0, K_NO_WAIT);
	k_thread_name_set(&status_thread_data, "status_io");

	err = uart_irq_callback_user_data_set(event_uart, event_uart_isr, NULL);
	if (err != 0) {
		return err;
	}
	uart_irq_rx_enable(event_uart);
	return 0;
}

void status_led_set_state(enum bci_led_state state)
{
	atomic_set(&led_state, (atomic_val_t)state);
	atomic_set(&led_state_started_ms, (atomic_val_t)k_uptime_get_32());
	if (state != BCI_LED_ACQUIRING) {
		atomic_clear(&frame_seen);
	}
}

void status_led_note_frame_success(void)
{
	atomic_set(&last_success_ms, (atomic_val_t)k_uptime_get_32());
	atomic_set(&frame_seen, 1);
}

uint32_t status_trigger_count(void)
{
	return (uint32_t)atomic_get(&trigger_count);
}

uint32_t status_trigger_drop_count(void)
{
	return (uint32_t)atomic_get(&trigger_drops);
}
