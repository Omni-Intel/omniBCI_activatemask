#include "sd_recorder.h"
#include <errno.h>
#include <ff.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/fs/fs.h>
#include <zephyr/kernel.h>
#include <zephyr/storage/disk_access.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(bci_sd_recorder, LOG_LEVEL_INF);

static FATFS fat_fs;
static struct fs_mount_t mount_point = {
	.type = FS_FATFS, .flags = FS_MOUNT_FLAG_NO_FORMAT,
	.fs_data = &fat_fs, .storage_dev = "SD", .mnt_point = "/SD:",
};
struct queued_frame {
	uint32_t generation;
	uint8_t data[BCI_STREAM_FRAME_SIZE];
};
struct queued_event {
	uint32_t generation, number, sequence;
	uint8_t code;
	uint64_t start_us;
	int64_t uptime_ms;
};
struct desired_state {
	bool running, gui;
	uint32_t generation;
	struct sd_record_config config;
};
/* 1260 * 52 = 65520 bytes; epoch tags prevent cross-file contamination. */
K_MSGQ_DEFINE(frames, sizeof(struct queued_frame), 1260, 4);
K_MSGQ_DEFINE(events, sizeof(struct queued_event), 32, 4);
K_THREAD_STACK_DEFINE(sd_stack, 4096);
static struct k_thread sd_thread_data;
static struct k_spinlock lock;
static struct desired_state desired;
static struct sd_recorder_stats stats;
static bool initialized;
static struct fs_file_t bin_file, meta_file;
static uint8_t batch[BCI_SD_BATCH_BYTES];
static size_t batch_count;
static uint64_t file_bytes;
static uint32_t first_seq, last_seq;
static uint64_t first_sample_us, last_sample_us;
static bool have_sample;
static uint32_t event_drops;
static uint32_t file_event_drops;
static uint32_t file_start_drops;

static struct desired_state snapshot(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	struct desired_state state = desired;
	k_spin_unlock(&lock, key);
	return state;
}

static void publish(bool mounted, bool recording, bool stopped, int err)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	stats.mounted = mounted;
	stats.recording = recording;
	stats.stopped = stopped && !desired.running;
	stats.last_error = err;
	k_spin_unlock(&lock, key);
}

static void drop(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	stats.frames_dropped++;
	k_spin_unlock(&lock, key);
}

static int meta_line(const char *fmt, ...)
{
	char line[640];
	va_list args;
	va_start(args, fmt);
	int length = vsnprintf(line, sizeof(line), fmt, args);
	va_end(args);
	if (length < 0 || length >= sizeof(line)) return -ENOSPC;
	ssize_t n = fs_write(&meta_file, line, length);
	return n == length ? 0 : (n < 0 ? (int)n : -EIO);
}

static int open_files(const struct desired_state *state)
{
	struct fs_dirent entry;
	char path[BCI_SD_FILENAME_BYTES], meta[BCI_SD_FILENAME_BYTES];
	for (uint32_t i = 1; i <= 99999; i++) {
		int err = bci_sd_filename(path, sizeof(path), i);
		if (err) return err;
		memcpy(meta, path, sizeof(meta));
		memcpy(meta + strlen(meta) - 3, "MET", 3);
		err = fs_stat(path, &entry);
		if (!err) continue;
		if (err != -ENOENT) return err;
		err = fs_stat(meta, &entry);
		if (!err) continue;
		if (err != -ENOENT) return err;
		fs_file_t_init(&bin_file);
		fs_file_t_init(&meta_file);
		err = fs_open(&bin_file, path, FS_O_CREATE | FS_O_WRITE);
		if (err) return err;
		err = fs_open(&meta_file, meta, FS_O_CREATE | FS_O_WRITE);
		if (err) { fs_close(&bin_file); return err; }
		file_bytes = 0;
		first_seq = last_seq = 0;
		first_sample_us = last_sample_us = 0;
		have_sample = false;
		batch_count = 0;
		k_spinlock_key_t key = k_spin_lock(&lock);
		file_start_drops = stats.frames_dropped;
		stats.close_error = 0;
		file_event_drops = event_drops;
		k_spin_unlock(&lock, key);
		const struct sd_record_config *c = &state->config;
		err = meta_line("{\"type\":\"start\",\"schema\":1,\"bin\":\"%s\",\"firmware\":\"%s\",\"uptime_ms\":%lld,\"utc\":null,\"time_source\":\"unsynchronized\",\"sample_rate\":%u,\"gains\":[%u,%u,%u,%u,%u,%u,%u,%u],\"enabled_mask\":%u,\"bias_mask\":%u,\"reference\":\"SRB1\",\"mode\":%u,\"generation\":%u}\n",
			path + 5, CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION, (long long)k_uptime_get(), c->sample_rate,
			c->gains[0], c->gains[1], c->gains[2], c->gains[3],
			c->gains[4], c->gains[5], c->gains[6], c->gains[7],
			c->enabled_mask, c->bias_mask, c->mode, state->generation);
		if (!err) err = fs_sync(&meta_file);
		if (err) { fs_close(&meta_file); fs_close(&bin_file); return err; }
		LOG_INF("SD recording file %s", path);
		return 0;
	}
	return -ENOSPC;
}

static int flush_batch(void)
{
	if (!batch_count) return 0;
	size_t bytes = batch_count * BCI_STREAM_FRAME_SIZE;
	ssize_t n = fs_write(&bin_file, batch, bytes);
	if (n != bytes) return n < 0 ? (int)n : -EIO;
	if (!have_sample) {
		first_seq = sys_get_le32(batch + 4);
		uint64_t now_us = (uint64_t)k_uptime_get() * 1000U;
		first_sample_us = bci_unwrap_sample_us(now_us, sys_get_le32(batch + 8));
	}
	last_seq = sys_get_le32(batch + (batch_count - 1) * BCI_STREAM_FRAME_SIZE + 4);
	uint64_t now_us = (uint64_t)k_uptime_get() * 1000U;
	last_sample_us = bci_unwrap_sample_us(now_us,
		sys_get_le32(batch + (batch_count - 1) * BCI_STREAM_FRAME_SIZE + 8));
	have_sample = true;
	file_bytes += bytes;
	k_spinlock_key_t key = k_spin_lock(&lock);
	stats.bytes_written += bytes;
	k_spin_unlock(&lock, key);
	batch_count = 0;
	return 0;
}

static int checkpoint(const char *type)
{
	int err = fs_sync(&bin_file);
	if (err) return err;
	k_spinlock_key_t key = k_spin_lock(&lock);
	uint32_t dropped = stats.frames_dropped - file_start_drops;
	uint32_t lost_events = event_drops - file_event_drops;
	k_spin_unlock(&lock, key);
	err = meta_line("{\"type\":\"%s\",\"uptime_ms\":%lld,\"synced_bytes\":%llu,\"has_samples\":%s,\"first_seq\":%u,\"last_seq\":%u,\"first_sample_uptime_us\":%llu,\"last_sample_uptime_us\":%llu,\"sd_drops\":%u,\"event_drops\":%u}\n",
		type, (long long)k_uptime_get(), (unsigned long long)file_bytes,
		have_sample ? "true" : "false", first_seq, last_seq,
		(unsigned long long)first_sample_us, (unsigned long long)last_sample_us,
		dropped, lost_events);
	return err ? err : fs_sync(&meta_file);
}

static int drain_events(uint32_t generation)
{
	struct queued_event e;
	while (k_msgq_peek(&events, &e) == 0) {
		if (e.generation != generation) {
			if ((int32_t)(e.generation - generation) > 0) break;
			(void)k_msgq_get(&events, &e, K_NO_WAIT);
			continue;
		}
		(void)k_msgq_get(&events, &e, K_NO_WAIT);
		int err = meta_line("{\"type\":\"trigger\",\"event\":%u,\"code\":%u,\"sequence\":%u,\"start_us\":%llu,\"uptime_ms\":%lld,\"association\":\"first_software_frame_after_event\"}\n",
			e.number, e.code, e.sequence,
			(unsigned long long)e.start_us, (long long)e.uptime_ms);
		if (err) return err;
	}
	return 0;
}

static int drain_frames(uint32_t generation)
{
	struct queued_frame f;
	while (k_msgq_peek(&frames, &f) == 0) {
		if (f.generation != generation) break;
		(void)k_msgq_get(&frames, &f, K_NO_WAIT);
		memcpy(batch + batch_count++ * BCI_STREAM_FRAME_SIZE, f.data, BCI_STREAM_FRAME_SIZE);
		if (batch_count == BCI_SD_BATCH_FRAMES) {
			int err = flush_batch();
			if (err) return err;
		}
	}
	return flush_batch();
}

static int close_files(uint32_t generation, bool clean)
{
	int err = clean ? drain_frames(generation) : -EIO;
	if (!err) err = drain_events(generation);
	if (!err) err = checkpoint("end");
	int e = fs_close(&bin_file);
	if (!err) err = e;
	e = fs_close(&meta_file);
	if (!err) err = e;
	batch_count = 0;
	k_spinlock_key_t key = k_spin_lock(&lock);
	stats.close_error = err;
	k_spin_unlock(&lock, key);
	return err;
}

static void sd_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
	bool mounted = false, recording = false, attempted = false, failed = false;
	bool previous_gui = false;
	uint32_t generation = UINT32_MAX;
	int last_error = 0;
	int64_t next_probe = 0, next_sync = 0;
	while (true) {
		struct desired_state state = snapshot();
		if (state.generation != generation) {
			if (recording) {
				int err = close_files(generation, true);
				if (err) { failed = true; last_error = err; }
				recording = false;
			}
			generation = state.generation;
			attempted = false;
			next_probe = 0;
			/* Explicit stop opens a new insertion window, never a radio timeout. */
			if (!state.running) failed = false;
		}
		/* GUI takeover while already running freezes the current mount state. */
		if (state.gui && !previous_gui && state.running) attempted = true;
		previous_gui = state.gui;
		int64_t now = k_uptime_get();
		if (mounted && !state.running && now >= next_probe) {
			int err = fs_unmount(&mount_point);
			if (!err) mounted = false;
			else { last_error = err; next_probe = now + 1000; }
		}
		if (!mounted && now >= next_probe &&
		    bci_sd_probe_allowed(state.gui, state.running, attempted, failed)) {
			attempted = true;
			next_probe = now + 1000;
			int err = fs_mount(&mount_point);
			if (!err) { mounted = true; last_error = 0; }
			else {
				last_error = err;
				/* Failed HAL init retains ErrorCode; no mounted files own this disk. */
				bool force = true;
				(void)disk_access_ioctl("SD", DISK_IOCTL_CTRL_DEINIT, &force);
			}
		}
		if (mounted && state.running && !recording && !failed) {
			int err = open_files(&state);
			if (!err) { recording = true; next_sync = now + 1000; }
			else { failed = true; last_error = err; }
		}
		publish(mounted, recording, !state.running && !recording, last_error);
		struct queued_frame f;
		if (k_msgq_get(&frames, &f, K_MSEC(20)) == 0) {
			if (recording && f.generation == generation) {
				memcpy(batch + batch_count++ * BCI_STREAM_FRAME_SIZE, f.data, BCI_STREAM_FRAME_SIZE);
			} else drop();
		}
		if (recording) {
			int err = drain_events(generation);
			if (!err && (batch_count == BCI_SD_BATCH_FRAMES || now >= next_sync)) err = flush_batch();
			if (!err && now >= next_sync) {
				err = checkpoint("progress");
				next_sync = now + 1000;
			}
			if (err) {
				LOG_ERR("SD disabled for this round; RF continues: %d", err);
				(void)close_files(generation, false);
				recording = false;
				failed = true;
				last_error = err;
				(void)fs_unmount(&mount_point);
				mounted = false;
				publish(false, false, !state.running, err);
			}
		}
	}
}

int sd_recorder_init(void)
{
	if (initialized) return -EALREADY;
	initialized = true;
	k_thread_create(&sd_thread_data, sd_stack, K_THREAD_STACK_SIZEOF(sd_stack),
		sd_thread, NULL, NULL, NULL, 9, 0, K_NO_WAIT);
	k_thread_name_set(&sd_thread_data, "sd_recorder");
	return 0;
}

void sd_recorder_take_control(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	desired.gui = true;
	k_spin_unlock(&lock, key);
}

int sd_recorder_start(const struct sd_record_config *config)
{
	if (!initialized || !config) return -EINVAL;
	k_spinlock_key_t key = k_spin_lock(&lock);
	desired.config = *config;
	desired.running = true;
	desired.generation++;
	stats.stopped = false;
	k_spin_unlock(&lock, key);
	return 0;
}

int sd_recorder_stop(void)
{
	if (!initialized) return -ENODEV;
	k_spinlock_key_t key = k_spin_lock(&lock);
	if (desired.running) { desired.running = false; desired.generation++; }
	stats.stopped = false;
	k_spin_unlock(&lock, key);
	return 0;
}

void sd_recorder_submit(const uint8_t frame[BCI_STREAM_FRAME_SIZE])
{
	struct queued_frame f;
	k_spinlock_key_t key = k_spin_lock(&lock);
	bool accept = desired.running && stats.recording;
	f.generation = desired.generation;
	k_spin_unlock(&lock, key);
	if (!accept || !frame) return;
	memcpy(f.data, frame, sizeof(f.data));
	if (k_msgq_put(&frames, &f, K_NO_WAIT)) drop();
}

void sd_recorder_event(uint32_t number, uint8_t code, uint32_t sequence,
			       uint64_t start_us, int64_t uptime_ms)
{
	struct queued_event e = {
		.number = number, .code = code, .sequence = sequence,
		.start_us = start_us, .uptime_ms = uptime_ms,
	};
	k_spinlock_key_t key = k_spin_lock(&lock);
	bool accept = desired.running && stats.recording;
	e.generation = desired.generation;
	k_spin_unlock(&lock, key);
	if (accept && k_msgq_put(&events, &e, K_NO_WAIT)) {
		key = k_spin_lock(&lock);
		event_drops++;
		k_spin_unlock(&lock, key);
	}
}

void sd_recorder_get_stats(struct sd_recorder_stats *out)
{
	if (!out) return;
	k_spinlock_key_t key = k_spin_lock(&lock);
	*out = stats;
	k_spin_unlock(&lock, key);
}
