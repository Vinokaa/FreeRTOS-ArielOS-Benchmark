//! Infraestrutura comum de medição para as aplicações Blinky e IoT.
//!
//! Protocolo (igual ao de Wang et al., 2026): contador de ciclos da CPU, 1005 amostras
//! brutas por experimento, 5 descartadas (aquecimento); as 1000 restantes são exportadas
//! e as estatísticas descritivas são calculadas no host (`tools/collect.py`).
#![no_std]
#![feature(asm_experimental_arch)]

pub mod clock;
pub mod pin;
pub mod report;
pub mod stats;
pub mod sync;
