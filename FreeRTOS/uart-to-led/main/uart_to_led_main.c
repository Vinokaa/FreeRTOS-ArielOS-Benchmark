#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_cpu.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

/* Kept equal to Ariel OS uart-to-led/src/pins.rs for benchmark parity. */
#define LED_GPIO GPIO_NUM_2
#define COMMAND_UART UART_NUM_0
#define UART_RX_BUFFER_SIZE 256
#define COMMAND_QUEUE_LENGTH 1
#define UART_TASK_PRIORITY 2
#define LED_TASK_PRIORITY 3
#define BENCHMARK_CORE 0

typedef struct {
    uint8_t value;
    uint32_t sent_at;
} led_command_t;

static QueueHandle_t command_queue;
static volatile uint32_t last_switch_cycles;

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

    (void)argument;
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
        if (byte != '0' && byte != '1' && byte != 't') {
            uart_write_bytes(COMMAND_UART, "?\r\n", 3);
            continue;
        }

        /* The LED task has a higher priority and is waiting on this queue. */
        command.value = byte;
        command.sent_at = esp_cpu_get_cycle_count();
        xQueueOverwrite(command_queue, &command);

        /* The preempted LED task records the switch time before we resume. */
        write_response(sequence, byte, last_switch_cycles);
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

        last_switch_cycles = esp_cpu_get_cycle_count() - command.sent_at;

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
