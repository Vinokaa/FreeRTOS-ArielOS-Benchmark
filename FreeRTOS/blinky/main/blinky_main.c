/* Application 1 (simple): a single periodic task toggles an LED.
 *
 * Equivalent to Ariel OS/blinky2. Embedded metric: PERIODIC JITTER,
 *   J_i = (C_i - C_{i-1}) - expected_period
 * where C_i is the CPU cycle counter (CCOUNT, 240 MHz) read right after the
 * task wakes up. The deadline is absolute (xTaskDelayUntil, like Ariel's
 * sleep_until), so the error of one cycle does not accumulate into the next.
 *
 * Periods: 1, 10 and 100 ms (as in Wang et al.). LED: GPIO2. */
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#include "bench_common.h"
#include "driver/gpio.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define APP "blinky"
#define LED_GPIO GPIO_NUM_2
#define BLINKY_TASK_PRIORITY 2
#define FUNCTIONAL_PERIOD_MS 500

static const uint32_t periods_ms[] = {1, 10, 100};
static const char *const period_labels[] = {"1ms", "10ms", "100ms"};

/* Static storage, like the other apps: measurement RAM is visible in the ELF. */
static bench_collector_t jitter_col;

static void blinky_task(void *argument)
{
    (void)argument;
    bool led_on = false;

    for (size_t i = 0; i < sizeof(periods_ms) / sizeof(periods_ms[0]); ++i) {
        const TickType_t period = pdMS_TO_TICKS(periods_ms[i]);
        const int32_t expected = (int32_t)(periods_ms[i] * BENCH_CYCLES_PER_MS);
        uint32_t previous = 0;
        bool have_previous = false;

        jitter_col = (bench_collector_t){0};
        TickType_t deadline = xTaskGetTickCount();
        while (!bench_full(&jitter_col)) {
            xTaskDelayUntil(&deadline, period);
            uint32_t now = bench_cycles(); /* Before any work of the task. */

            /* Application workload (outside the timestamp path). */
            led_on = !led_on;
            gpio_set_level(LED_GPIO, led_on);

            /* Instrumentation: one integer stored in the buffer. */
            if (have_previous) {
                bench_push(&jitter_col, bench_delta(previous, now) - expected);
            }
            previous = now;
            have_previous = true;
        }

        /* Raw samples are printed only after the measurement window. */
        bench_dump(&jitter_col, APP, "jitter", period_labels[i]);
    }

    bench_end(APP);

    /* Functional mode: keep blinking (500 ms) after the benchmark. */
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(FUNCTIONAL_PERIOD_MS));
        led_on = !led_on;
        gpio_set_level(LED_GPIO, led_on);
    }
}

void app_main(void)
{
    ESP_ERROR_CHECK(gpio_reset_pin(LED_GPIO));
    ESP_ERROR_CHECK(gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT));
    ESP_ERROR_CHECK(gpio_set_level(LED_GPIO, 0));
    bench_begin(APP);

    if (xTaskCreatePinnedToCore(blinky_task, "blinky", 4096, NULL, BLINKY_TASK_PRIORITY,
                                NULL, BENCH_CORE) != pdPASS) {
        abort();
    }
}
