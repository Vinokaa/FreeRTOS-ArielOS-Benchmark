idf_component_register(SRCS "uart_to_led_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES bench_common esp_driver_gpio esp_driver_uart esp_hw_support esp_timer)
