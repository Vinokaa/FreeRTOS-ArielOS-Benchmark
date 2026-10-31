/* Equivalent to Ariel OS/iot: sensor -> software IRQ -> processing -> shared state.
 * All timestamps are hardware microseconds, not FreeRTOS ticks. */
#include <inttypes.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "bench_common.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_intr_alloc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "soc/dport_reg.h"
#include "soc/interrupts.h"

/* In SMP builds ESP-IDF reserves SWI2 for its IPC ISR. UNICORE frees it. */
#if !CONFIG_FREERTOS_UNICORE || defined(CONFIG_ESP_IPC_ISR_ENABLE)
#error "IoT requires CONFIG_FREERTOS_UNICORE=y: SWI2 must be free of IPC ISR use"
#endif

#define APP "iot"
#define LED_GPIO GPIO_NUM_2
#define SENSOR_PERIOD_MS 10
#define ACTUATOR_PERIOD_MS 20
#define LOG_PERIOD_MS 1000
#define FILTER_WINDOW 8
#define ALARM_ON_MV 1800
#define ALARM_OFF_MV 1700

typedef struct {
    uint32_t seq;
    uint16_t raw;
    uint32_t mv;
    bool alarm;
} shared_t;

static shared_t shared;
static portMUX_TYPE shared_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t processing_handle;
static _Atomic uint32_t raw_value, t_event, t_irq, t_signal;
static _Atomic bool event_pending, sensor_done, processing_done;
static _Atomic uint32_t events_lost;

/* Static storage exposes measurement RAM in the ELF, outside task stacks. */
static bench_collector_t jitter_col, irq_col, switch_col, exec_col;

static inline uint32_t now_us(void)
{
    return (uint32_t)esp_timer_get_time();
}

/* Same wrapping LCG, triangle and 12-bit range as Ariel's synth(). */
static uint16_t synth(uint32_t seq)
{
    int32_t t = (int32_t)(seq % 400);
    int32_t tri = t < 200 ? t : 400 - t;
    int32_t noise = (int32_t)(((seq * UINT32_C(1664525) + UINT32_C(1013904223)) >> 24) & 0x1f) - 16;
    int32_t value = 1500 + tri * 5 + noise;
    return (uint16_t)(value < 0 ? 0 : value > 4095 ? 4095 : value);
}

static void IRAM_ATTR drdy_swi(void *argument)
{
    uint32_t entered = (uint32_t)esp_timer_get_time(); /* First application statement. */
    DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, 0);
    atomic_store_explicit(&t_irq, entered, memory_order_release);
    BaseType_t higher_priority_woken = pdFALSE;
    (void)argument;
    atomic_store_explicit(&t_signal, (uint32_t)esp_timer_get_time(), memory_order_release);
    vTaskNotifyGiveFromISR(processing_handle, &higher_priority_woken);
    if (higher_priority_woken) {
        portYIELD_FROM_ISR();
    }
}

static void sensor_task(void *argument)
{
    (void)argument;
    /* Allocation on the pinned task routes the interrupt to the benchmark core. */
    DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, 0);
    ESP_ERROR_CHECK(esp_intr_alloc(ETS_FROM_CPU_INTR2_SOURCE,
                                 ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM,
                                 drdy_swi, NULL, NULL));

    TickType_t deadline = xTaskGetTickCount();
    uint32_t previous = 0, seq = 0;
    bool have_previous = false;
    for (;;) {
        xTaskDelayUntil(&deadline, pdMS_TO_TICKS(SENSOR_PERIOD_MS));
        uint32_t started = now_us(); /* Before acquisition or instrumentation. */

        /* Never overwrite an event's timestamps if processing fell behind. */
        if (!atomic_exchange_explicit(&event_pending, true, memory_order_acq_rel)) {
            atomic_store_explicit(&raw_value, synth(seq), memory_order_release);
            atomic_store_explicit(&t_event, now_us(), memory_order_release);
            DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, DPORT_CPU_INTR_FROM_CPU_2);
        } else if (!atomic_load_explicit(&sensor_done, memory_order_acquire) ||
                   !atomic_load_explicit(&processing_done, memory_order_acquire)) {
            atomic_fetch_add_explicit(&events_lost, 1, memory_order_relaxed);
        }
        ++seq;

        if (have_previous && !bench_full(&jitter_col)) {
            bench_push(&jitter_col, bench_delta(previous, started) - SENSOR_PERIOD_MS * 1000);
            if (bench_full(&jitter_col)) {
                atomic_store_explicit(&sensor_done, true, memory_order_release);
            }
        }
        previous = started;
        have_previous = true;
    }
}

static void processing_task(void *argument)
{
    (void)argument;
    uint16_t window[FILTER_WINDOW] = {0};
    size_t index = 0, filled = 0;
    uint32_t seq = 0;
    bool alarm = false;

    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        uint32_t started = now_us(); /* First statement after notification wait. */
        uint32_t signaled = atomic_load_explicit(&t_signal, memory_order_acquire);
        uint32_t entered = atomic_load_explicit(&t_irq, memory_order_acquire);
        uint32_t event = atomic_load_explicit(&t_event, memory_order_acquire);
        uint16_t raw = (uint16_t)atomic_load_explicit(&raw_value, memory_order_acquire);

        window[index] = raw;
        index = (index + 1) % FILTER_WINDOW;
        if (filled < FILTER_WINDOW) {
            ++filled;
        }
        uint32_t sum = 0;
        for (size_t i = 0; i < filled; ++i) {
            sum += window[i];
        }
        uint32_t mv = (sum / filled) * 3300 / 4095;
        if (!alarm && mv >= ALARM_ON_MV) {
            alarm = true;
        } else if (alarm && mv <= ALARM_OFF_MV) {
            alarm = false;
        }
        ++seq;
        portENTER_CRITICAL(&shared_lock);
        shared = (shared_t){.seq = seq, .raw = raw, .mv = mv, .alarm = alarm};
        portEXIT_CRITICAL(&shared_lock);
        uint32_t ended = now_us();

        if (!bench_full(&switch_col)) {
            bench_push(&irq_col, bench_delta(event, entered));
            bench_push(&switch_col, bench_delta(signaled, started));
            bench_push(&exec_col, bench_delta(started, ended));
            if (bench_full(&switch_col)) {
                atomic_store_explicit(&processing_done, true, memory_order_release);
            }
        }
        atomic_store_explicit(&event_pending, false, memory_order_release);
    }
}

static shared_t read_shared(void)
{
    portENTER_CRITICAL(&shared_lock);
    shared_t snapshot = shared;
    portEXIT_CRITICAL(&shared_lock);
    return snapshot;
}

static void actuator_task(void *argument)
{
    (void)argument;
    TickType_t deadline = xTaskGetTickCount();
    for (;;) {
        xTaskDelayUntil(&deadline, pdMS_TO_TICKS(ACTUATOR_PERIOD_MS));
        shared_t snapshot = read_shared();
        ESP_ERROR_CHECK(gpio_set_level(LED_GPIO, snapshot.alarm));
    }
}

static void logger_task(void *argument)
{
    (void)argument;
    bool reported = false;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(LOG_PERIOD_MS));
        shared_t snapshot = read_shared();
        printf("LOG,seq=%" PRIu32 ",raw=%u,mv=%" PRIu32 ",alarm=%s\n",
               snapshot.seq, (unsigned)snapshot.raw, snapshot.mv,
               snapshot.alarm ? "true" : "false");

        /* Every collector must finish before serial export can perturb timing. */
        if (!reported && atomic_load_explicit(&sensor_done, memory_order_acquire) &&
            atomic_load_explicit(&processing_done, memory_order_acquire)) {
            reported = true;
            bench_dump(&jitter_col, APP, "jitter", "sensor_10ms");
            bench_dump(&irq_col, APP, "irq_latency", "swi_drdy");
            bench_dump(&switch_col, APP, "task_switch", "isr_to_thread");
            bench_dump(&exec_col, APP, "proc_exec", "avg8_alarm");
            bench_info(APP, "events_lost", atomic_load_explicit(&events_lost, memory_order_relaxed));
            bench_end(APP);
        }
    }
}

void app_main(void)
{
    ESP_ERROR_CHECK(gpio_reset_pin(LED_GPIO));
    ESP_ERROR_CHECK(gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT));
    ESP_ERROR_CHECK(gpio_set_level(LED_GPIO, 0));
    bench_begin(APP);

    if (xTaskCreatePinnedToCore(processing_task, "processing", 4096, NULL, 4,
                                &processing_handle, BENCH_CORE) != pdPASS ||
        xTaskCreatePinnedToCore(actuator_task, "actuator", 2048, NULL, 2,
                                NULL, BENCH_CORE) != pdPASS ||
        xTaskCreatePinnedToCore(logger_task, "logger", 4096, NULL, 1,
                                NULL, BENCH_CORE) != pdPASS ||
        xTaskCreatePinnedToCore(sensor_task, "sensor", 4096, NULL, 3,
                                NULL, BENCH_CORE) != pdPASS) {
        abort();
    }
}
