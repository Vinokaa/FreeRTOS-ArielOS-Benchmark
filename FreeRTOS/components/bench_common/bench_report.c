#include "bench_common.h"

#include <inttypes.h>
#include <stdio.h>

#include "esp_app_desc.h"
#include "esp_private/esp_clk.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "sdkconfig.h"

void bench_begin(const char *app)
{
    char sha256[65];
    esp_app_get_elf_sha256(sha256, sizeof(sha256));
    printf("BENCH_BEGIN,%s,freertos,0.1.0,clock_res_us=1,n=%d,warmup=%d\n",
           app, BENCH_N, BENCH_WARMUP);
    printf("META,%s,idf,%s\n", app, esp_get_idf_version());
    printf("META,%s,compiler,%s\n", app, __VERSION__);
    printf("META,%s,app_sha256,%s\n", app, sha256);
    bench_info(app, "cpu_hz", esp_clk_cpu_freq());
    bench_info(app, "tick_hz", configTICK_RATE_HZ);
    bench_info(app, "core", BENCH_CORE);
}

void bench_info(const char *app, const char *key, uint32_t value)
{
    printf("INFO,%s,%s,%" PRIu32 "\n", app, key, value);
}

void bench_dump(const bench_collector_t *collector, const char *app,
                const char *metric, const char *cfg)
{
    puts("SAMPLE_HEADER,app,metric,cfg,idx,value_us");
    for (size_t i = 0; i < collector->len; ++i) {
        printf("SAMPLE,%s,%s,%s,%u,%" PRId32 "\n",
               app, metric, cfg, (unsigned)i, collector->samples[i]);
    }
}

void bench_end(const char *app)
{
    printf("BENCH_END,%s\n", app);
}
