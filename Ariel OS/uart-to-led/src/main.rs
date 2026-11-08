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
//! Uma SWI2 separada mede a latência de interrupção antes de cada envio. O comando
//! `b` exporta os dois coletores (5 descartes + 1000 amostras) uma única vez.

#![no_main]
#![no_std]

mod pins;

use core::{
    cell::Cell,
    fmt::{self, Write as _},
    sync::atomic::{AtomicU32, Ordering},
};

use ariel_os::{
    gpio::{Level, Output},
    hal,
    reexports::static_cell::{ConstStaticCell, StaticCell},
    thread::{self, block_on, sync::Channel, thread_flags, ThreadId},
    uart::Baudrate,
};
use bench_common::{clock, report, stats::Collector, sync::Flag};
use critical_section::Mutex;
use embassy_sync::{blocking_mutex::raw::CriticalSectionRawMutex, signal::Signal};
use embedded_io_async::{Read as _, Write as _};
use esp_hal::{
    interrupt::{software::SoftwareInterrupt, Priority},
    ram,
};

const APP: &str = "uart-to-led";
// block_on usa o bit 0 em Ariel OS v0.6.0. A confirmação da SWI não pode
// consumir um wake da UART (ou aceitar esse wake como se fosse a ISR).
const FLAG_IRQ: u16 = 1 << 1;
const _: () = assert!(thread::CORE_COUNT == 1, "CCOUNT benchmark requires one core");
static UART_TID: Mutex<Cell<Option<ThreadId>>> = Mutex::new(Cell::new(None));
static IRQ_ENTERED: AtomicU32 = AtomicU32::new(0);
static LED_READY: Flag = Flag::new();

// Inicialização estática: os buffers de ~8 KB nunca passam pela pilha da thread.
// Só uart_thread recebe as referências mutáveis; ISR e LED não tocam nos coletores.
static SWITCH_COL: ConstStaticCell<Collector> = ConstStaticCell::new(Collector::new());
static IRQ_COL: ConstStaticCell<Collector> = ConstStaticCell::new(Collector::new());

/// Canal entre as threads: (comando, ciclo de CPU no envio).
static CMD: Channel<(u8, u32)> = Channel::new();

/// Última latência medida pela `led_thread`, em ciclos de CPU.
static LAST_SWITCH: AtomicU32 = AtomicU32::new(0);

/// Entrega única dos periféricos para as threads, só na inicialização.
static LED_HW: Signal<CriticalSectionRawMutex, Output> = Signal::new();
// UART0::new devolve o tipo genérico `Uart`, que implementa read/write_all.
static UART_HW: Signal<CriticalSectionRawMutex, hal::uart::Uart<'static>> = Signal::new();

/// SWI2 exclusiva desta aplicação; o escalonador usa SWI0/SWI1, como no IoT.
#[inline(always)]
fn benchmark_swi_line() -> SoftwareInterrupt<'static, 2> {
    // SAFETY: nenhuma outra parte desta aplicação usa SWI2. raise/reset escrevem
    // um único registrador; instalação do handler ocorre antes de READY.
    unsafe { SoftwareInterrupt::<2>::steal() }
}

#[esp_hal::handler(priority = Priority::Priority1)]
#[ram]
fn benchmark_swi() {
    let entered = clock::now_cycles(); // primeira instrução da aplicação no handler
    benchmark_swi_line().reset();
    IRQ_ENTERED.store(entered, Ordering::Release);
    if let Some(tid) = critical_section::with(|cs| UART_TID.borrow(cs).get()) {
        thread_flags::set(tid, FLAG_IRQ);
    }
}

fn measure_interrupt_latency(col: &mut Collector) {
    if col.is_full() {
        return;
    }
    thread_flags::clear(FLAG_IRQ);
    let triggered = clock::now_cycles();
    benchmark_swi_line().raise();
    // O flag retém a conclusão mesmo se a ISR executar antes do wait.
    // Nenhum próximo evento ou envio ao LED começa antes dessa confirmação.
    thread_flags::wait_any(FLAG_IRQ);
    thread_flags::clear(FLAG_IRQ);
    let entered = IRQ_ENTERED.load(Ordering::Acquire);
    col.push(clock::delta_i32(triggered, entered));
}

/// Toda a saída passa pelo mesmo driver, sem concorrência com o logger global.
struct UartWriter<'a>(&'a mut hal::uart::Uart<'static>);

impl fmt::Write for UartWriter<'_> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        block_on(self.0.write_all(s.as_bytes())).map_err(|_| fmt::Error)
    }
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
    LED_READY.wait();
    let switch_col = SWITCH_COL.take();
    let irq_col = IRQ_COL.take();
    let mut reported = false;
    critical_section::with(|cs| UART_TID.borrow(cs).set(thread::current_tid()));
    benchmark_swi_line().reset();
    benchmark_swi_line().set_interrupt_handler(benchmark_swi);
    report::begin_to(&mut UartWriter(&mut uart), APP).unwrap();
    block_on(uart.write_all(b"\r\nREADY\r\n")).unwrap();

    let mut input = [0u8; 16];
    let mut seq: u32 = 0;
    loop {
        let n = block_on(uart.read(&mut input)).unwrap();
        for &cmd in &input[..n] {
            match cmd {
                b'1' | b'0' | b't' => {}
                b'\r' | b'\n' => continue,
                b'b' => {
                    if switch_col.is_full() && irq_col.is_full() && !reported {
                        let mut out = UartWriter(&mut uart);
                        switch_col.finish_to(&mut out, APP, "task_switch", "uart_to_led").unwrap();
                        irq_col.finish_to(&mut out, APP, "irq_latency", "swi_uart").unwrap();
                        report::end_to(&mut out, APP).unwrap();
                        reported = true;
                    } else {
                        block_on(uart.write_all(b"?\r\n")).unwrap();
                    }
                    continue;
                }
                _ => {
                    block_on(uart.write_all(b"?\r\n")).unwrap();
                    continue;
                }
            }

            // A confirmação da ISR termina antes da janela UART -> LED.
            measure_interrupt_latency(irq_col);

            // A led_thread tem prioridade maior e está esperando: o envio troca de thread
            // na hora, e quando `send` retorna a medição já está em LAST_SWITCH.
            CMD.send(&(cmd, clock::now_cycles()));
            let delta = LAST_SWITCH.load(Ordering::Acquire);
            switch_col.push(delta as i32);

            // Linha de resultado: R,<seq>,<cmd>,<ciclos>
            write!(UartWriter(&mut uart), "R,{},{},{}\r\n", seq, cmd as char, delta).unwrap();
            seq = seq.wrapping_add(1);
        }
    }
}

#[ariel_os::thread(autostart, stacksize = 2048, priority = 3)]
fn led_thread() {
    let mut led = block_on(LED_HW.wait());
    LED_READY.set();

    loop {
        let (cmd, sent_at) = CMD.recv();
        let now = clock::now_cycles();
        // wrapping_sub cobre a volta do contador de 32 bits (~17,9 s a 240 MHz).
        LAST_SWITCH.store(now.wrapping_sub(sent_at), Ordering::Release);

        match cmd {
            b'1' => led.set_high(),
            b'0' => led.set_low(),
            _ => led.toggle(),
        }
    }
}
