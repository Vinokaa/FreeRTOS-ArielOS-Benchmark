#include <inttypes.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>

#include "bench_common.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_attr.h"
#include "esp_cpu.h"
#include "esp_err.h"
#include "esp_intr_alloc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "soc/dport_reg.h"
#include "soc/interrupts.h"

#if CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ != 240 || defined(CONFIG_PM_ENABLE)
#error "UART cycle conversion requires a fixed 240 MHz CPU (see sdkconfig.defaults)"
#endif

#if !CONFIG_FREERTOS_UNICORE || defined(CONFIG_ESP_IPC_ISR_ENABLE)
#error "UART IRQ benchmark requires UNICORE: SWI2 must be free of IPC ISR use"
#endif

/* Kept equal to Ariel OS uart-to-led/src/pins.rs for benchmark parity. */
#define LED_GPIO GPIO_NUM_2
#define COMMAND_UART UART_NUM_0
#define UART_RX_BUFFER_SIZE 256
#define COMMAND_QUEUE_LENGTH 1
#define UART_TASK_PRIORITY 2
#define LED_TASK_PRIORITY 3
#define BENCHMARK_CORE BENCH_CORE
#define APP "uart-to-led"
#define CYCLES_PER_US 240

typedef struct {
    uint8_t value;
    uint32_t sent_at;
} led_command_t;

static QueueHandle_t command_queue;
static _Atomic uint32_t last_switch_cycles;
static bench_collector_t switch_col;
static bench_collector_t irq_col;
static TaskHandle_t benchmark_uart_task;
static _Atomic uint32_t irq_entered_us;

/* Same software-triggered interrupt method as IoT/Wang; this does not measure
 * UART electrical reception or the driver's RX interrupt. No logging in ISR. */
static void IRAM_ATTR benchmark_swi(void *argument)
{
    uint32_t entered = (uint32_t)esp_timer_get_time();
    DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, 0);
    atomic_store_explicit(&irq_entered_us, entered, memory_order_release);
    BaseType_t higher_priority_woken = pdFALSE;
    (void)argument;
    vTaskNotifyGiveFromISR(benchmark_uart_task, &higher_priority_woken);
    if (higher_priority_woken) {
        portYIELD_FROM_ISR();
    }
}

static void measure_interrupt_latency(void)
{
    if (bench_full(&irq_col)) {
        return;
    }
    uint32_t triggered = (uint32_t)esp_timer_get_time();
    DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, DPORT_CPU_INTR_FROM_CPU_2);
    /* A counting notification also works when the IRQ ran before this wait.
     * Finish this event before another command can overwrite its timestamp. */
    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000)) != 1) {
        abort();
    }
    uint32_t entered = atomic_load_explicit(&irq_entered_us, memory_order_acquire);
    bench_push(&irq_col, bench_delta(triggered, entered));
}

static void write_response(uint32_t sequence, uint8_t command, uint32_t cycles)
{
    char response[48];
    int length = snprintf(response, sizeof(response), "R,%" PRIu32 ",%c,%" PRIu32 "\r\n",
                          sequence, command, cycles);

    if (length > 0) {
        uart_write_bytes(COMMAND_UART, response, length);
    }
}

static void uart_task(void *argument)
{
    uint8_t byte;
    uint32_t sequence = 0;
    bool reported = false;

    (void)argument;
    benchmark_uart_task = xTaskGetCurrentTaskHandle();
    /* Allocate on the pinned UART task so SWI2 is routed to CPU0. */
    DPORT_REG_WRITE(DPORT_CPU_INTR_FROM_CPU_2_REG, 0);
    ESP_ERROR_CHECK(esp_intr_alloc(ETS_FROM_CPU_INTR2_SOURCE,
                                 ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM,
                                 benchmark_swi, NULL, NULL));
    bench_begin(APP);
    uart_write_bytes(COMMAND_UART, "\r\nREADY\r\n", 9);

    for (;;) {
        led_command_t command;
        int received = uart_read_bytes(COMMAND_UART, &byte, 1, portMAX_DELAY);

        if (received != 1) {
            continue;
        }
        if (byte == '\r' || byte == '\n') {
            continue;
        }
        /* Explicit export keeps bulk logging out of all command measurements. */
        if (byte == 'b') {
            if (bench_full(&switch_col) && bench_full(&irq_col) && !reported) {
                reported = true;
                bench_dump(&switch_col, APP, "task_switch", "uart_to_led");
                bench_dump(&irq_col, APP, "irq_latency", "swi_uart");
                bench_end(APP);
            } else {
                uart_write_bytes(COMMAND_UART, "?\r\n", 3);
            }
            continue;
        }
        if (byte != '0' && byte != '1' && byte != 't') {
            uart_write_bytes(COMMAND_UART, "?\r\n", 3);
            continue;
        }

        /* The IRQ experiment finishes before the queue timing window starts. */
        measure_interrupt_latency();

        /* The LED task has a higher priority and is waiting on this queue. */
        command.value = byte;
        command.sent_at = esp_cpu_get_cycle_count();
        if (xQueueSend(command_queue, &command, portMAX_DELAY) != pdTRUE) {
            abort();
        }

        /* The higher-priority LED task records the latency before we resume.
         * Keep R in cycles; quantize only the common SAMPLE export to us. */
        uint32_t cycles = atomic_load_explicit(&last_switch_cycles, memory_order_acquire);
        bench_push(&switch_col, (int32_t)(cycles / CYCLES_PER_US));
        write_response(sequence, byte, cycles);
        sequence++;
    }
}

static void led_task(void *argument)
{
    led_command_t command;
    uint32_t led_state = 0;

    (void)argument;
    for (;;) {
        if (xQueueReceive(command_queue, &command, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        atomic_store_explicit(&last_switch_cycles,
                              esp_cpu_get_cycle_count() - command.sent_at,
                              memory_order_release);

        if (command.value == '1') {
            led_state = 1;
        } else if (command.value == '0') {
            led_state = 0;
        } else {
            led_state = !led_state;
        }
        ESP_ERROR_CHECK(gpio_set_level(LED_GPIO, led_state));
    }
}

void app_main(void)
{
    const uart_config_t uart_config = {
        .baud_rate = 115200,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(gpio_reset_pin(LED_GPIO));
    ESP_ERROR_CHECK(gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT));
    ESP_ERROR_CHECK(gpio_set_level(LED_GPIO, 0));
    ESP_ERROR_CHECK(uart_param_config(COMMAND_UART, &uart_config));
    ESP_ERROR_CHECK(uart_driver_install(COMMAND_UART, UART_RX_BUFFER_SIZE, 0, 0, NULL, 0));

    command_queue = xQueueCreate(COMMAND_QUEUE_LENGTH, sizeof(led_command_t));
    if (command_queue == NULL) {
        abort();
    }
    /* Both tasks use one core so CCOUNT measures a real task switch. */
    if (xTaskCreatePinnedToCore(led_task, "led", 2048, NULL, LED_TASK_PRIORITY, NULL,
                                BENCHMARK_CORE) != pdPASS ||
        xTaskCreatePinnedToCore(uart_task, "uart", 4096, NULL, UART_TASK_PRIORITY, NULL,
                                BENCHMARK_CORE) != pdPASS) {
        abort();
    }
}
