#include "bench_common.h"

#include <inttypes.h>
#include <stdio.h>

#include "esp_app_desc.h"
#include "esp_private/esp_clk.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#define BENCH_VERSION "0.2.0"

/* CCOUNT cycles elapsed over 100 ms of the independent 1 us hardware timer.
 * Expected: 24,000,000 at 240 MHz. Validates the cycle clock on the board. */
static uint32_t ccount_per_100ms(void)
{
    vTaskDelay(1); /* Start right after a tick. */
    int64_t t0 = esp_timer_get_time();
    uint32_t c0 = bench_cycles();
    vTaskDelay(pdMS_TO_TICKS(100));
    int64_t t1 = esp_timer_get_time();
    uint32_t c1 = bench_cycles();
    int64_t elapsed_us = t1 - t0;
    return (uint32_t)((uint64_t)(c1 - c0) * 100000u / (uint64_t)elapsed_us);
}

void bench_begin(const char *app)
{
    char sha256[65];
    esp_app_get_elf_sha256(sha256, sizeof(sha256));
    printf("BENCH_BEGIN,%s,freertos," BENCH_VERSION ",unit=cycles,cpu_hz=%u,n=%d,warmup=%d\n",
           app, BENCH_CPU_HZ, BENCH_N, BENCH_WARMUP);
    printf("META,%s,idf,%s\n", app, esp_get_idf_version());
    printf("META,%s,compiler,%s\n", app, __VERSION__);
    printf("META,%s,app_sha256,%s\n", app, sha256);
    bench_info(app, "cpu_hz", esp_clk_cpu_freq());
    bench_info(app, "tick_hz", configTICK_RATE_HZ);
    bench_info(app, "core", BENCH_CORE);
    bench_info(app, "ccount_per_100ms", ccount_per_100ms());
}

void bench_info(const char *app, const char *key, uint32_t value)
{
    printf("INFO,%s,%s,%" PRIu32 "\n", app, key, value);
}

void bench_dump(const bench_collector_t *collector, const char *app,
                const char *metric, const char *cfg)
{
    puts("SAMPLE_HEADER,app,metric,cfg,idx,value_cycles");
    for (size_t i = 0; i < collector->len; ++i) {
        printf("SAMPLE,%s,%s,%s,%u,%" PRId32 "\n",
               app, metric, cfg, (unsigned)i, collector->samples[i]);
    }
}

void bench_end(const char *app)
{
    printf("BENCH_END,%s\n", app);
}
