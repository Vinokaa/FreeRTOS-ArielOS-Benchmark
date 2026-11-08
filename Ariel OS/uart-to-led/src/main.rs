//! UART-to-LED instrumentado para o benchmark FreeRTOS x Ariel OS (protocolo bench-common).
//!
//! - `uart_thread` (prioridade 2): recebe o comando pela UART e envia à `led_thread`.
//! - `led_thread` (prioridade 3): bloqueada no canal; ao receber, muda o LED.
//!
//! Métricas, em ciclos de CPU (CCOUNT, 240 MHz), iguais às da versão FreeRTOS:
//!  * `task_switch / uart_to_led`: CCOUNT logo antes do `CMD.send` até a primeira leitura
//!    da `led_thread` depois do `CMD.recv`. O valor também volta pela UART numa linha
//!    `R,<seq>,<cmd>,<ciclos>`.
//!  * `irq_latency / swi_uart`: CCOUNT antes de disparar a SWI2 (FROM_CPU_INTR2) até a
//!    primeira leitura no handler (em RAM, nível 1). Mesmo método de disparo por software
//!    do IoT e de Wang et al.; não mede a chegada elétrica do byte nem a IRQ do driver.
//!
//! A SWI termina antes da janela de task-switch. São 1005 amostras por métrica (5 de
//! aquecimento descartadas), guardadas em memória estática; nada é exportado durante a
//! coleta. Depois de os dois coletores encherem, o comando `b` exporta tudo uma única vez.

#![no_main]
#![no_std]

mod pins;

use core::{
    cell::Cell,
    sync::atomic::{AtomicU32, Ordering},
};

use ariel_os::{
    gpio::{Level, Output},
    hal,
    reexports::static_cell::{ConstStaticCell, StaticCell},
    thread::{self, block_on, sync::Channel, thread_flags, ThreadId},
    time::Duration,
    uart::Baudrate,
};
use bench_common::{clock, report, stats::Collector};
use critical_section::Mutex;
use embassy_sync::{blocking_mutex::raw::CriticalSectionRawMutex, signal::Signal};
use embedded_io_async::{Read as _, Write as _};
use esp_hal::{
    interrupt::{software::SoftwareInterrupt, Priority},
    ram,
};

const APP: &str = "uart-to-led";
const FLAG_IRQ: u16 = 1 << 0;

/// Canal entre as threads: (comando, ciclo de CPU no envio).
static CMD: Channel<(u8, u32)> = Channel::new();

/// Última latência de task-switch medida pela `led_thread`, em ciclos de CPU.
static LAST_SWITCH: AtomicU32 = AtomicU32::new(0);

/// CCOUNT lido na primeira linha do handler da SWI2.
static T_IRQ: AtomicU32 = AtomicU32::new(0);
/// Thread notificada pelo handler (a `uart_thread`).
static UART_TID: Mutex<Cell<Option<ThreadId>>> = Mutex::new(Cell::new(None));

/// Coletores estáticos (RAM de medição visível no ELF, como no FreeRTOS).
/// Só a `uart_thread` os usa.
static SWITCH_COL: ConstStaticCell<Collector> = ConstStaticCell::new(Collector::new());
static IRQ_COL: ConstStaticCell<Collector> = ConstStaticCell::new(Collector::new());

/// Entrega única dos periféricos para as threads, só na inicialização.
static LED_HW: Signal<CriticalSectionRawMutex, Output> = Signal::new();
// UART0::new devolve o tipo genérico `Uart`, que implementa read/write_all.
static UART_HW: Signal<CriticalSectionRawMutex, hal::uart::Uart<'static>> = Signal::new();

/// Interrupção de software usada no experimento (FROM_CPU_INTR2).
///
/// SAFETY: o Ariel OS usa só as SWI 0 e 1 (escalonador); a 2 é exclusiva desta app e
/// `raise`/`reset` são escritas atômicas em um único registrador.
#[inline(always)]
fn swi_line() -> SoftwareInterrupt<'static, 2> {
    unsafe { SoftwareInterrupt::<2>::steal() }
}

/// Handler da SWI2: registra o instante de entrada e acorda a `uart_thread`.
/// Sem log aqui: a amostra é empurrada pela própria thread.
#[esp_hal::handler(priority = Priority::Priority1)]
#[ram]
fn uart_swi() {
    let t_irq = clock::now_cycles(); // primeira linha de código da app após a interrupção
    swi_line().reset(); // limpa FROM_CPU_INTR2
    T_IRQ.store(t_irq, Ordering::Release);
    if let Some(tid) = critical_section::with(|cs| UART_TID.borrow(cs).get()) {
        thread_flags::set(tid, FLAG_IRQ);
    }
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
    swi_line().set_interrupt_handler(uart_swi);

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

/// Mede uma latência de interrupção, se o coletor ainda não estiver cheio.
/// A espera pela flag confirma o fim da ISR e fica fora da janela medida.
fn measure_interrupt_latency(irq_col: &mut Collector) {
    if irq_col.is_full() {
        return;
    }
    let triggered = clock::now_cycles();
    swi_line().raise();
    thread_flags::wait_any(FLAG_IRQ);
    thread_flags::clear(FLAG_IRQ);
    let t_irq = T_IRQ.load(Ordering::Acquire);
    irq_col.push(clock::delta_i32(triggered, t_irq));
}

#[ariel_os::thread(autostart, stacksize = 4096, priority = 2)]
fn uart_thread() {
    critical_section::with(|cs| UART_TID.borrow(cs).set(thread::current_tid()));
    let mut uart = block_on(UART_HW.wait());
    thread::sleep(Duration::from_millis(10));
    report::begin(APP);
    block_on(uart.write_all(b"\r\nREADY\r\n")).unwrap();

    let switch_col = SWITCH_COL.take();
    let irq_col = IRQ_COL.take();
    let mut input = [0u8; 16];
    let mut seq: u32 = 0;
    let mut reported = false;
    loop {
        let n = block_on(uart.read(&mut input)).unwrap();
        for &cmd in &input[..n] {
            match cmd {
                b'1' | b'0' | b't' => {}
                b'\r' | b'\n' => continue,
                // Exportação explícita: nenhum log em massa durante as medições.
                b'b' => {
                    if switch_col.is_full() && irq_col.is_full() && !reported {
                        reported = true;
                        block_on(uart.flush()).unwrap();
                        switch_col.finish(APP, "task_switch", "uart_to_led");
                        irq_col.finish(APP, "irq_latency", "swi_uart");
                        report::end(APP);
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

            // O experimento de interrupção termina antes da janela do canal.
            measure_interrupt_latency(irq_col);

            // A led_thread tem prioridade maior e está esperando: o envio troca de thread
            // na hora, e quando `send` retorna a medição já está em LAST_SWITCH.
            CMD.send(&(cmd, clock::now_cycles()));
            let delta = LAST_SWITCH.load(Ordering::Acquire);
            switch_col.push(delta as i32);

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
        // `elapsed` cobre a volta do contador de 32 bits (~17,9 s a 240 MHz).
        LAST_SWITCH.store(clock::elapsed(sent_at, clock::now_cycles()), Ordering::Release);

        match cmd {
            b'1' => led.set_high(),
            b'0' => led.set_low(),
            _ => led.toggle(),
        }
    }
}
