//! Aplicação 3 — IoT (complexa): aquisição -> processamento -> atuação + log (Fig. 1c).
//!
//! * Task Sensor (thread, 10 ms): gera uma leitura sintética (12 bits, determinística) e
//!   dispara a interrupção de software FROM_CPU_INTR2 da CPU, que faz o papel do pino
//!   "data-ready" de um sensor real (sem jumper; como o EXTI disparado por software de
//!   Wang et al. no STM32).
//! * Interrupção: handler `drdy_swi` de FROM_CPU_INTR2, que sinaliza a thread de
//!   processamento. (O escalonador Xtensa do Ariel OS usa só as SWI 0 e 1.)
//! * Task Processamento (thread, prio. alta): média móvel de 8 amostras, conversão para mV,
//!   alarme com histerese; escreve no struct compartilhado.
//! * Task Atuador (thread, 20 ms): lê o struct e aciona o LED (GPIO2) conforme o alarme.
//! * Task Log (thread, 1 s): lê o struct e transmite uma linha de log; ao fim da coleta,
//!   é a única que exporta as amostras (nada de exportação concorrente entre threads).
//!
//! Métricas embutidas (todas medidas COM a carga completa da aplicação em execução), em
//! ciclos de CPU (CCOUNT, 240 MHz):
//!  * JITTER PERIÓDICO do Task Sensor        J = (C_i − C_{i−1}) − 10 ms
//!  * LATÊNCIA DE INTERRUPÇÃO                C_irq (1ª linha do handler) − C_evento
//!  * LATÊNCIA DE TASK-SWITCH                C_início_processamento − C_sinalização
//!  * (extra) tempo de execução do processamento, para validar a equivalência da carga.
//!
//! Igual à versão FreeRTOS: um evento pendente nunca é sobrescrito; eventos descartados
//! durante a coleta aparecem em `INFO,iot,events_lost,N`.
#![no_main]
#![no_std]

use core::{
    cell::{Cell, RefCell},
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
};

use ariel_os::{
    gpio::{Level, Output},
    thread::{self, thread_flags, ThreadId},
    time::{Duration, Instant},
};
use bench_common::{
    clock, report,
    pin::PinSlot,
    stats::Collector,
    sync::Flag,
};
use critical_section::Mutex;
use esp_hal::{
    interrupt::{software::SoftwareInterrupt, Priority},
    ram,
};

const APP: &str = "iot";
const SENSOR_PERIOD_MS: u32 = 10;
const ACTUATOR_PERIOD_MS: u64 = 20;
const LOG_PERIOD_MS: u64 = 1000;
const FILTER_WINDOW: usize = 8;
const ALARM_ON_MV: u32 = 1800;
const ALARM_OFF_MV: u32 = 1700;

const FLAG_DRDY: u16 = 1 << 0;

// ---- Struct compartilhado (Fig. 1c): escrito pelo Processamento, lido por Atuador/Log ----
#[derive(Clone, Copy)]
struct Shared {
    seq: u32,
    raw: u16,
    mv: u32,
    alarm: bool,
}
static SHARED: Mutex<RefCell<Shared>> =
    Mutex::new(RefCell::new(Shared { seq: 0, raw: 0, mv: 0, alarm: false }));

// ---- Comunicação sensor -> IRQ -> processamento ----
static RAW_VAL: AtomicU32 = AtomicU32::new(0);
static T_EVENT: AtomicU32 = AtomicU32::new(0);
static T_IRQ: AtomicU32 = AtomicU32::new(0);
static T_SIG: AtomicU32 = AtomicU32::new(0);
static PROC_TID: Mutex<Cell<Option<ThreadId>>> = Mutex::new(Cell::new(None));
static EVENT_PENDING: AtomicBool = AtomicBool::new(false);
static EVENTS_LOST: AtomicU32 = AtomicU32::new(0);

// ---- Coletores estáticos (RAM de medição visível no ELF, como no FreeRTOS) ----
static JITTER_COL: Mutex<RefCell<Collector>> = Mutex::new(RefCell::new(Collector::new()));
static IRQ_COL: Mutex<RefCell<Collector>> = Mutex::new(RefCell::new(Collector::new()));
static SWITCH_COL: Mutex<RefCell<Collector>> = Mutex::new(RefCell::new(Collector::new()));
static EXEC_COL: Mutex<RefCell<Collector>> = Mutex::new(RefCell::new(Collector::new()));
static SENSOR_DONE: Flag = Flag::new();
static PROC_DONE: Flag = Flag::new();

static ACT_LED: PinSlot = PinSlot::new();
static HW_READY: Flag = Flag::new();
static PROC_UP: Flag = Flag::new();

ariel_os::hal::define_peripherals!(Pins {
    led: GPIO2,
});

/// Interrupção de software usada como DRDY (FROM_CPU_INTR2).
///
/// SAFETY: o Ariel OS usa só as SWI 0 e 1 (escalonador); a 2 é exclusiva desta app e
/// `raise`/`reset` são escritas atômicas em um único registrador.
#[inline(always)]
fn drdy_swi_line() -> SoftwareInterrupt<'static, 2> {
    unsafe { SoftwareInterrupt::<2>::steal() }
}

/// Leitura sintética de 12 bits: triângulo lento + ruído pseudoaleatório determinístico.
fn synth(seq: u32) -> u16 {
    let t = (seq % 400) as i32;
    let tri = if t < 200 { t } else { 400 - t }; // 0..200
    let noise = (((seq.wrapping_mul(1_664_525).wrapping_add(1_013_904_223)) >> 24) & 0x1F) as i32 - 16;
    (1500 + tri * 5 + noise).clamp(0, 4095) as u16
}

/// Registra uma amostra (fora das janelas medidas: os timestamps já foram lidos).
/// Devolve `true` quando o coletor está cheio.
fn push(col: &Mutex<RefCell<Collector>>, v: i32) -> bool {
    critical_section::with(|cs| {
        let mut c = col.borrow_ref_mut(cs);
        c.push(v);
        c.is_full()
    })
}

/// Exporta um coletor já congelado (cópia rápida; impressão fora da seção crítica).
fn dump(col: &Mutex<RefCell<Collector>>, metric: &str, cfg: &str) {
    let snapshot = critical_section::with(|cs| col.borrow_ref(cs).clone());
    snapshot.finish(APP, metric, cfg);
}

// ======================= Interrupção (DRDY) =======================

/// Handler de FROM_CPU_INTR2: registra C_irq e sinaliza o processamento.
/// Sem coletor/log aqui: a amostra de latência é empurrada pela thread de processamento.
#[esp_hal::handler(priority = Priority::Priority1)]
#[ram]
fn drdy_swi() {
    let t_irq = clock::now_cycles(); // primeira linha de código da app após a interrupção
    drdy_swi_line().reset(); // limpa FROM_CPU_INTR2
    T_IRQ.store(t_irq, Ordering::Release);

    if let Some(tid) = critical_section::with(|cs| PROC_TID.borrow(cs).get()) {
        T_SIG.store(clock::now_cycles(), Ordering::Release); // C_sinalização
        thread_flags::set(tid, FLAG_DRDY);
    }
}

/// Dona do pino do LED; instala o handler da interrupção de software.
#[ariel_os::task(autostart, peripherals)]
async fn hw_init(p: Pins) {
    ACT_LED.install(Output::new(p.led, Level::Low));
    drdy_swi_line().set_interrupt_handler(drdy_swi);
    HW_READY.set();
}

// ======================= Task Sensor =======================

#[ariel_os::thread(autostart, stacksize = 4096, priority = 3)]
fn sensor() {
    HW_READY.wait();
    PROC_UP.wait();
    report::begin(APP);

    let period = Duration::from_millis(SENSOR_PERIOD_MS as u64);
    let expected = (SENSOR_PERIOD_MS * clock::CYCLES_PER_MS) as i32;
    let mut prev: Option<u32> = None;
    let mut seq: u32 = 0;
    let mut deadline = Instant::now() + period;

    loop {
        thread::sleep_until(deadline);
        let t = clock::now_cycles(); // timestamp ANTES do trabalho da tarefa
        deadline += period;

        // ---- Aquisição (carga da aplicação) ----
        // Nunca sobrescreve os timestamps de um evento ainda não processado.
        if !EVENT_PENDING.swap(true, Ordering::AcqRel) {
            RAW_VAL.store(synth(seq) as u32, Ordering::Release);
            T_EVENT.store(clock::now_cycles(), Ordering::Release); // C_evento
            drdy_swi_line().raise(); // gera a interrupção
        } else if !(SENSOR_DONE.is_set() && PROC_DONE.is_set()) {
            EVENTS_LOST.fetch_add(1, Ordering::Relaxed);
        }
        seq = seq.wrapping_add(1);

        // ---- Instrumentação: jitter periódico ----
        if let Some(p) = prev {
            if !SENSOR_DONE.is_set() && push(&JITTER_COL, clock::delta_i32(p, t) - expected) {
                SENSOR_DONE.set();
            }
        }
        prev = Some(t);
    }
}

// ======================= Task Processamento =======================

#[ariel_os::thread(autostart, stacksize = 4096, priority = 4)]
fn processing() {
    HW_READY.wait();
    critical_section::with(|cs| PROC_TID.borrow(cs).set(thread::current_tid()));
    PROC_UP.set();

    let mut win = [0u16; FILTER_WINDOW];
    let mut idx = 0usize;
    let mut filled = 0usize;
    let mut alarm = false;
    let mut seq: u32 = 0;

    loop {
        thread_flags::wait_any(FLAG_DRDY);
        let t_start = clock::now_cycles(); // C_início_processamento
        thread_flags::clear(FLAG_DRDY);
        let t_sig = T_SIG.load(Ordering::Acquire);
        let t_irq = T_IRQ.load(Ordering::Acquire);
        let t_ev = T_EVENT.load(Ordering::Acquire);

        // ---- Processamento: média móvel + conversão para mV + alarme com histerese ----
        let raw = RAW_VAL.load(Ordering::Acquire) as u16;
        win[idx] = raw;
        idx = (idx + 1) % FILTER_WINDOW;
        filled = (filled + 1).min(FILTER_WINDOW);
        let sum: u32 = win[..filled].iter().map(|&v| v as u32).sum();
        let avg = sum / filled as u32;
        let mv = avg * 3300 / 4095;
        if !alarm && mv >= ALARM_ON_MV {
            alarm = true;
        } else if alarm && mv <= ALARM_OFF_MV {
            alarm = false;
        }
        seq = seq.wrapping_add(1);

        // ---- Escrita no struct compartilhado ----
        critical_section::with(|cs| {
            *SHARED.borrow_ref_mut(cs) = Shared { seq, raw, mv, alarm };
        });
        let t_end = clock::now_cycles();

        // ---- Instrumentação ----
        if !PROC_DONE.is_set() {
            push(&IRQ_COL, clock::delta_i32(t_ev, t_irq));
            push(&EXEC_COL, clock::delta_i32(t_start, t_end));
            if push(&SWITCH_COL, clock::delta_i32(t_sig, t_start)) {
                PROC_DONE.set();
            }
        }
        EVENT_PENDING.store(false, Ordering::Release);
    }
}

// ======================= Task Atuador =======================

#[ariel_os::thread(autostart, stacksize = 2048, priority = 2)]
fn actuator() {
    HW_READY.wait();
    let period = Duration::from_millis(ACTUATOR_PERIOD_MS);
    let mut deadline = Instant::now() + period;
    loop {
        thread::sleep_until(deadline);
        deadline += period;
        let s = critical_section::with(|cs| *SHARED.borrow_ref(cs)); // leitura
        ACT_LED.set(s.alarm);
    }
}

// ======================= Task Log =======================

// Pilha maior: `dump` copia um coletor (~4 KB) antes de imprimir.
#[ariel_os::thread(autostart, stacksize = 8192, priority = 1)]
fn logger() {
    HW_READY.wait();
    let mut reported = false;
    loop {
        thread::sleep(Duration::from_millis(LOG_PERIOD_MS));
        let s = critical_section::with(|cs| *SHARED.borrow_ref(cs)); // leitura
        // Prefixo LOG, para o coletor ignorar estas linhas. `quiet` só suprime esta linha.
        #[cfg(not(feature = "quiet"))]
        ariel_os::log::info!("LOG,seq={},raw={},mv={},alarm={}", s.seq, s.raw, s.mv, s.alarm);
        #[cfg(feature = "quiet")]
        let _ = s;

        // Exporta só depois que TODOS os coletores terminaram (a impressão não perturba
        // nenhuma janela de medição e as linhas não se intercalam entre threads).
        if !reported && SENSOR_DONE.is_set() && PROC_DONE.is_set() {
            reported = true;
            dump(&JITTER_COL, "jitter", "sensor_10ms");
            dump(&IRQ_COL, "irq_latency", "swi_drdy");
            dump(&SWITCH_COL, "task_switch", "isr_to_thread");
            dump(&EXEC_COL, "proc_exec", "avg8_alarm");
            report::info_kv(APP, "events_lost", EVENTS_LOST.load(Ordering::Relaxed));
            report::end(APP);
        }
    }
}
