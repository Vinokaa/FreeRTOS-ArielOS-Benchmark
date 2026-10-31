//! UART-to-LED instrumentado para o benchmark FreeRTOS x Ariel OS.
//!
//! - `uart_thread` (prioridade 2): recebe o comando pela UART e envia à `led_thread`.
//! - `led_thread` (prioridade 3): bloqueada no canal; ao receber, muda o LED.
//!
//! Medição (latência de task-switch): ciclos de CPU (registrador CCOUNT, 240 MHz) entre
//! o instante logo antes do `CMD.send` e a primeira instrução da `led_thread` depois do
//! `CMD.recv`. O resultado volta pela UART numa linha `R,<seq>,<cmd>,<ciclos>`.
//! A versão FreeRTOS mede o mesmo intervalo com `esp_cpu_get_cycle_count()` em volta de
//! `xQueueSend` / `xQueueReceive` e imprime a mesma linha.

#![no_main]
#![no_std]
#![feature(asm_experimental_arch)]

mod pins;

use core::sync::atomic::{AtomicU32, Ordering};

use ariel_os::{
    gpio::{Level, Output},
    hal,
    reexports::static_cell::StaticCell,
    thread::{block_on, sync::Channel},
    uart::Baudrate,
};
use embassy_sync::{blocking_mutex::raw::CriticalSectionRawMutex, signal::Signal};
use embedded_io_async::{Read as _, Write as _};

/// Canal entre as threads: (comando, ciclo de CPU no envio).
static CMD: Channel<(u8, u32)> = Channel::new();

/// Última latência medida pela `led_thread`, em ciclos de CPU.
static LAST_SWITCH: AtomicU32 = AtomicU32::new(0);

/// Entrega única dos periféricos para as threads, só na inicialização.
static LED_HW: Signal<CriticalSectionRawMutex, Output> = Signal::new();
// UART0::new devolve o tipo genérico `Uart`, que implementa read/write_all.
static UART_HW: Signal<CriticalSectionRawMutex, hal::uart::Uart<'static>> = Signal::new();

/// Contador de ciclos do núcleo Xtensa (CCOUNT). A 240 MHz, 1 ciclo ≈ 4,17 ns.
#[inline(always)]
fn cycles() -> u32 {
    let c: u32;
    // SAFETY: só lê um registrador especial, sem efeitos colaterais.
    unsafe { core::arch::asm!("rsr.ccount {0}", out(reg) c, options(nomem, nostack)) };
    c
}

/// Escreve `n` em decimal no fim de `buf`, devolvendo o trecho usado.
fn fmt_u32(mut n: u32, buf: &mut [u8; 10]) -> &[u8] {
    let mut i = buf.len();
    loop {
        i -= 1;
        buf[i] = b'0' + (n % 10) as u8;
        n /= 10;
        if n == 0 {
            break;
        }
    }
    &buf[i..]
}

#[ariel_os::task(autostart, peripherals)]
async fn init(peripherals: pins::Peripherals) {
    LED_HW.signal(Output::new(peripherals.led, Level::Low));

    let mut config = hal::uart::Config::default();
    config.baudrate = Baudrate::_115200;

    static RX_BUF: StaticCell<[u8; 32]> = StaticCell::new();
    static TX_BUF: StaticCell<[u8; 32]> = StaticCell::new();
    let uart = pins::Uart::new(
        peripherals.uart_rx,
        peripherals.uart_tx,
        RX_BUF.init([0; 32]),
        TX_BUF.init([0; 32]),
        config,
    )
    .expect("Invalid UART configuration");
    UART_HW.signal(uart);
}

#[ariel_os::thread(autostart, stacksize = 4096, priority = 2)]
fn uart_thread() {
    let mut uart = block_on(UART_HW.wait());
    block_on(uart.write_all(b"\r\nREADY\r\n")).unwrap();

    let mut input = [0u8; 16];
    let mut seq: u32 = 0;
    loop {
        let n = block_on(uart.read(&mut input)).unwrap();
        for &cmd in &input[..n] {
            match cmd {
                b'1' | b'0' | b't' => {}
                b'\r' | b'\n' => continue,
                _ => {
                    block_on(uart.write_all(b"?\r\n")).unwrap();
                    continue;
                }
            }

            // A led_thread tem prioridade maior e está esperando: o envio troca de thread
            // na hora, e quando `send` retorna a medição já está em LAST_SWITCH.
            CMD.send(&(cmd, cycles()));
            let delta = LAST_SWITCH.load(Ordering::Acquire);

            // Linha de resultado: R,<seq>,<cmd>,<ciclos>
            let mut a = [0u8; 10];
            let mut b = [0u8; 10];
            block_on(uart.write_all(b"R,")).unwrap();
            block_on(uart.write_all(fmt_u32(seq, &mut a))).unwrap();
            block_on(uart.write_all(&[b',', cmd, b','])).unwrap();
            block_on(uart.write_all(fmt_u32(delta, &mut b))).unwrap();
            block_on(uart.write_all(b"\r\n")).unwrap();
            seq = seq.wrapping_add(1);
        }
    }
}

#[ariel_os::thread(autostart, stacksize = 2048, priority = 3)]
fn led_thread() {
    let mut led = block_on(LED_HW.wait());

    loop {
        let (cmd, sent_at) = CMD.recv();
        let now = cycles();
        // wrapping_sub cobre a volta do contador de 32 bits (~17,9 s a 240 MHz).
        LAST_SWITCH.store(now.wrapping_sub(sent_at), Ordering::Release);

        match cmd {
            b'1' => led.set_high(),
            b'0' => led.set_low(),
            _ => led.toggle(),
        }
    }
}