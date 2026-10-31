//! Saída estruturada (linhas CSV no log serial), para o script `scripts/collect.py`.
//!
//! Formatos:
//!   BENCH_BEGIN,<app>,ariel-os,<versão do bench>,clock_res_us=<n>,n=<N>,warmup=<W>
//!   SAMPLE_HEADER,app,metric,cfg,idx,value_us             (cabeçalho de cada bloco)
//!   SAMPLE,<app>,<métrica>,<cfg>,<idx>,<valor>             (uma linha por amostra bruta)
//!   RESULT,<app>,<métrica>,<cfg>,<n>,<min>,<max>,<média>,<std>,<p50>,<p90>,<p99>,us
//!                                                          (só via `result`; `finish` não emite)
//!   INFO,<app>,<chave>,<valor>
//!   BENCH_END,<app>

use ariel_os::log::info;

use crate::{
    clock,
    stats::{Collector, Summary, N, WARMUP},
};

pub fn begin(app: &str) {
    info!(
        "BENCH_BEGIN,{},ariel-os,{},clock_res_us={},n={},warmup={}",
        app,
        env!("CARGO_PKG_VERSION"),
        clock::RESOLUTION_US,
        N,
        WARMUP
    );
}

pub fn end(app: &str) {
    info!("BENCH_END,{}", app);
}

pub fn info_kv(app: &str, key: &str, value: u32) {
    info!("INFO,{},{},{}", app, key, value);
}

pub fn result(app: &str, metric: &str, cfg: &str, s: &Summary) {
    info!(
        "RESULT,{},{},{},{},{},{},{},{},{},{},{},us",
        app, metric, cfg, s.n, s.min, s.max, s.mean, s.std, s.p50, s.p90, s.p99
    );
}

impl Collector {
    /// Fecha o experimento: despeja as amostras brutas (ordem de coleta) como CSV.
    /// As estatísticas são calculadas no host (`scripts/collect.py`).
    pub fn finish(&mut self, app: &str, metric: &str, cfg: &str) {
        info!("SAMPLE_HEADER,app,metric,cfg,idx,value_us");
        for (i, v) in self.samples().iter().enumerate() {
            info!("SAMPLE,{},{},{},{},{}", app, metric, cfg, i, *v);
        }
    }
}