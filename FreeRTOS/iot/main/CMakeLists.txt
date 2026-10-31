idf_component_register(SRCS "iot_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES bench_common esp_driver_gpio esp_hw_support esp_timer)
