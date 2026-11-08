//! Fonte única de timestamps: ciclos de CPU (registrador CCOUNT do Xtensa, 240 MHz).
//!
//! O ESP32 não tem o DWT do ARM usado por Wang et al.; o CCOUNT é o equivalente
//! (1 ciclo ≈ 4,17 ns). O contador tem 32 bits e dá a volta a cada ~17,9 s; sempre
//! calcule intervalos com `elapsed`/`delta_i32` (wrapping). O Ariel OS roda o ESP32
//! em um único núcleo, então todas as leituras vêm do mesmo contador.

/// Frequência fixa da CPU (o Ariel OS configura `CpuClock::max()` = 240 MHz no ESP32).
pub const CPU_HZ: u32 = 240_000_000;
/// Ciclos por milissegundo.
pub const CYCLES_PER_MS: u32 = CPU_HZ / 1000;

/// Leitura do contador de ciclos (CCOUNT).
#[inline(always)]
pub fn now_cycles() -> u32 {
    let c: u32;
    // SAFETY: só lê um registrador especial, sem efeitos colaterais.
    unsafe { core::arch::asm!("rsr.ccount {0}", out(reg) c, options(nomem, nostack)) };
    c
}

/// Intervalo `to − from` (ciclos), tolerante a wrap de 32 bits.
#[inline(always)]
pub fn elapsed(from: u32, to: u32) -> u32 {
    to.wrapping_sub(from)
}

/// Igual a `elapsed`, mas com sinal (útil para desvios, ex.: jitter).
#[inline(always)]
pub fn delta_i32(from: u32, to: u32) -> i32 {
    to.wrapping_sub(from) as i32
}
