idf_component_register(SRCS "blinky_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES bench_common esp_driver_gpio)
