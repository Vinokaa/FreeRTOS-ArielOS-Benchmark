#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Same sampling policy as Ariel OS/bench-common and Wang et al., section 6. */
#define BENCH_N 1000
#define BENCH_WARMUP 5
#define BENCH_CORE 0

/* All timestamps are CPU cycles (Xtensa CCOUNT), the ESP32 counterpart of the
 * ARM DWT cycle counter used by Wang et al. The CPU clock must be fixed. */
#define BENCH_CPU_HZ 240000000u
#define BENCH_CYCLES_PER_MS (BENCH_CPU_HZ / 1000u)

#ifndef BENCH_HOST_TEST
#include "esp_cpu.h"
#include "sdkconfig.h"

#if CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ != 240 || defined(CONFIG_PM_ENABLE)
#error "Cycle timestamps require a fixed 240 MHz CPU (see sdkconfig.defaults)"
#endif

/* 32-bit counter: wraps every ~17.9 s at 240 MHz; use bench_delta. */
static inline uint32_t bench_cycles(void)
{
    return (uint32_t)esp_cpu_get_cycle_count();
}
#endif

typedef struct {
    int32_t samples[BENCH_N];
    size_t len;
    size_t warmup_seen;
} bench_collector_t;

/* Zero-initialize before use. No allocation, logging or sorting on this path. */
static inline void bench_push(bench_collector_t *collector, int32_t value)
{
    if (collector->warmup_seen < BENCH_WARMUP) {
        ++collector->warmup_seen;
    } else if (collector->len < BENCH_N) {
        collector->samples[collector->len++] = value;
    }
}

static inline bool bench_full(const bench_collector_t *collector)
{
    return collector->len == BENCH_N;
}

/* Unsigned subtraction handles the 32-bit cycle counter wrapping. */
static inline int32_t bench_delta(uint32_t from, uint32_t to)
{
    return (int32_t)(to - from);
}

/* Prints BENCH_BEGIN/META/INFO, including the ccount_per_100ms sanity check
 * (blocks ~100 ms: call from a task, before any measurement window). */
void bench_begin(const char *app);
void bench_info(const char *app, const char *key, uint32_t value);
void bench_dump(const bench_collector_t *collector, const char *app,
                const char *metric, const char *cfg);
void bench_end(const char *app);
