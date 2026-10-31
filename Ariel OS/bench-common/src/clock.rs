//! Fonte única de timestamps, em µs (32 bits, com wrap).
//!
//! O ESP32 (Xtensa) não tem DWT; usamos `esp_hal::time::Instant` (resolução de 1 µs).
//! Timestamps de 32 bits evitam atômicos de 64 bits (inexistentes em Xtensa) e dão
//! ~71 min de faixa; sempre calcule intervalos com `elapsed`/`delta_i32` (wrapping).

#[cfg(feature = "esp-clock")]
#[inline(always)]
pub fn now_us() -> u32 {
    esp_hal::time::Instant::now()
        .duration_since_epoch()
        .as_micros() as u32
}

#[cfg(not(feature = "esp-clock"))]
#[inline(always)]
pub fn now_us() -> u32 {
    ariel_os::time::Instant::now().as_micros() as u32
}

/// Resolução nominal do relógio (µs).
pub const RESOLUTION_US: u32 = 1;

/// Intervalo `to − from` (µs), tolerante a wrap de 32 bits.
#[inline(always)]
pub fn elapsed(from: u32, to: u32) -> u32 {
    to.wrapping_sub(from)
}

/// Igual a `elapsed`, mas com sinal (útil para desvios, ex.: jitter).
#[inline(always)]
pub fn delta_i32(from: u32, to: u32) -> i32 {
    to.wrapping_sub(from) as i32
}