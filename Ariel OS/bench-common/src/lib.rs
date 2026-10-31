//! Infraestrutura comum de medição para as três aplicações (Blinky, UART-to-LED, IoT).
//!
//! Protocolo (igual ao de Wang et al., 2026): relógio de hardware, 1005 amostras brutas
//! por experimento, 5 descartadas (aquecimento), estatísticas descritivas sobre as 1000
//! restantes (mín, máx, média, desvio-padrão, P50, P90, P99).
#![no_std]

pub mod clock;
pub mod pin;
pub mod report;
pub mod stats;
pub mod sync;