//! Aplicação 1 — BLINKY (simples): uma única tarefa periódica alterna um LED.
//!
//! Métrica embutida: JITTER PERIÓDICO.
//!   J_i = (C_i − C_{i−1}) − período_esperado
//! onde C_i é o timestamp (µs, relógio de hardware via esp-hal) logo após o despertar da
//! tarefa. O deadline é absoluto (`sleep_until`, análogo a `vTaskDelayUntil` do FreeRTOS),
//! portanto o erro de um ciclo não se acumula no seguinte.
//!
//! Períodos testados: 1, 10 e 100 ms (como em Wang et al.). LED: GPIO2.
#![no_main]
#![no_std]

use ariel_os::{
    gpio::{Level, Output},
    thread,
    time::{Duration, Instant},
};
use bench_common::{
    clock, report,
    pin::PinSlot,
    stats::Collector,
    sync::Flag,
};

const APP: &str = "blinky";
const PERIODS_MS: [u32; 3] = [1, 10, 100];
const PERIOD_LABELS: [&str; 3] = ["1ms", "10ms", "100ms"];

static LED: PinSlot = PinSlot::new();
static READY: Flag = Flag::new();

ariel_os::hal::define_peripherals!(LedPins { led: GPIO2 });

/// Tarefa de inicialização: threads não recebem periféricos, então ela instala o LED.
#[ariel_os::task(autostart, peripherals)]
async fn init(p: LedPins) {
    LED.install(Output::new(p.led, Level::Low));
    READY.set();
}

#[ariel_os::thread(autostart, stacksize = 8192, priority = 2)]
fn blinky() {
    READY.wait();
    report::begin(APP);

    for (i, &p_ms) in PERIODS_MS.iter().enumerate() {
        let period = Duration::from_millis(p_ms as u64);
        let period_us = (p_ms * 1000) as i32;

        let mut col = Collector::new();
        let mut led_on = false;
        let mut prev: Option<u32> = None;
        let mut deadline = Instant::now() + period;

        while !col.is_full() {
            thread::sleep_until(deadline);
            let t = clock::now_us(); // timestamp ANTES de qualquer trabalho da tarefa

            // Carga de trabalho da aplicação (fora do caminho do timestamp).
            led_on = !led_on;
            LED.set(led_on);

            // Instrumentação: apenas um inteiro gravado no buffer.
            if let Some(p) = prev {
                col.push(clock::delta_i32(p, t) - period_us);
            }
            prev = Some(t);
            deadline += period;
        }

        // Impressão das amostras brutas (CSV) só depois da janela de medição.
        col.finish(APP, "jitter", PERIOD_LABELS[i]);
    }

    report::end(APP);

    // Modo funcional: continua piscando (500 ms) após o benchmark.
    let mut on = false;
    loop {
        thread::sleep(Duration::from_millis(500));
        on = !on;
        LED.set(on);
    }
}