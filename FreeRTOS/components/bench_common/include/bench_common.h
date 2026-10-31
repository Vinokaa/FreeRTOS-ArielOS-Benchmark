#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Same sampling policy as Ariel OS/bench-common and Wang et al., section 6. */
#define BENCH_N 1000
#define BENCH_WARMUP 5
#define BENCH_CORE 0

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

/* Unsigned subtraction handles the 32-bit microsecond timer wrapping. */
static inline int32_t bench_delta(uint32_t from, uint32_t to)
{
    return (int32_t)(to - from);
}

void bench_begin(const char *app);
void bench_info(const char *app, const char *key, uint32_t value);
void bench_dump(const bench_collector_t *collector, const char *app,
                const char *metric, const char *cfg);
void bench_end(const char *app);
