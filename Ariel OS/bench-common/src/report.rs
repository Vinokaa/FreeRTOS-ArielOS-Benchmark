//! Saída estruturada (linhas CSV no log serial), para `tools/capture.py` e `tools/collect.py`.
//!
//! Formatos (iguais aos do `bench_common` do FreeRTOS):
//!   BENCH_BEGIN,<app>,ariel-os,<versão>,unit=cycles,cpu_hz=<Hz>,n=<N>,warmup=<W>
//!   INFO,<app>,<chave>,<valor>
//!   SAMPLE_HEADER,app,metric,cfg,idx,value_cycles        (cabeçalho de cada bloco)
//!   SAMPLE,<app>,<métrica>,<cfg>,<idx>,<valor>            (uma linha por amostra)
//!   BENCH_END,<app>

use ariel_os::{
    log::info,
    thread,
    time::{Duration, Instant},
};

use crate::{
    clock,
    stats::{Collector, N, WARMUP},
};

/// Versão do formato/protocolo; igual à do lado FreeRTOS.
const BENCH_VERSION: &str = "0.2.0";

/// Ciclos de CCOUNT decorridos em 100 ms do relógio de tempo do Ariel OS (µs).
/// Esperado: 24 000 000 a 240 MHz; valida o relógio de ciclos na placa.
fn ccount_per_100ms() -> u32 {
    let t0 = Instant::now();
    let c0 = clock::now_cycles();
    thread::sleep(Duration::from_millis(100));
    let t1 = Instant::now();
    let c1 = clock::now_cycles();
    let us = t1.duration_since(t0).as_micros().max(1);
    (clock::elapsed(c0, c1) as u64 * 100_000 / us) as u32
}

/// Cabeçalho do experimento. Bloqueia ~100 ms (verificação do relógio): chame de uma
/// thread, antes de qualquer janela de medição.
pub fn begin(app: &str) {
    info!(
        "BENCH_BEGIN,{},ariel-os,{},unit=cycles,cpu_hz={},n={},warmup={}",
        app,
        BENCH_VERSION,
        clock::CPU_HZ,
        N,
        WARMUP
    );
    info_kv(app, "cpu_hz", clock::CPU_HZ);
    info_kv(app, "ccount_per_100ms", ccount_per_100ms());
}

pub fn end(app: &str) {
    info!("BENCH_END,{}", app);
}

pub fn info_kv(app: &str, key: &str, value: u32) {
    info!("INFO,{},{},{}", app, key, value);
}

impl Collector {
    /// Fecha o experimento: despeja as amostras brutas (ordem de coleta) como CSV.
    /// As estatísticas são calculadas no host (`tools/collect.py`).
    pub fn finish(&self, app: &str, metric: &str, cfg: &str) {
        info!("SAMPLE_HEADER,app,metric,cfg,idx,value_cycles");
        for (i, v) in self.samples().iter().enumerate() {
            info!("SAMPLE,{},{},{},{},{}", app, metric, cfg, i, *v);
        }
    }
}
