use ariel_os::hal::uart;

pub type Uart<'a> = uart::UART0<'a>;

ariel_os::hal::define_peripherals!(Peripherals {
    led: GPIO2,
    uart_tx: GPIO1,
    uart_rx: GPIO3,
});