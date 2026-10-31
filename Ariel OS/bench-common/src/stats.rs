//! Coletor de amostras (sem alloc) e estatísticas descritivas.
//!
//! O caminho quente (`push`) só grava um inteiro: nada de formatação/log durante a
//! janela de medição. O cálculo e a impressão ocorrem depois, em `finish`.

/// Amostras analisadas por experimento.
pub const N: usize = 1000;
/// Amostras iniciais descartadas.
pub const WARMUP: usize = 5;
/// Amostras brutas a coletar (N + WARMUP = 1005, como em Wang et al.).
pub const N_RAW: usize = N + WARMUP;

#[derive(Clone, Copy)]
pub struct Summary {
    pub n: u32,
    pub min: i32,
    pub max: i32,
    pub mean: i32,
    pub std: u32,
    pub p50: i32,
    pub p90: i32,
    pub p99: i32,
}

impl Summary {
    pub const EMPTY: Summary = Summary { n: 0, min: 0, max: 0, mean: 0, std: 0, p50: 0, p90: 0, p99: 0 };

    pub fn range(&self) -> i32 {
        self.max - self.min
    }
}

pub struct Collector {
    buf: [i32; N],
    len: usize,
    seen: usize,
}

impl Collector {
    pub const fn new() -> Self {
        Self { buf: [0; N], len: 0, seen: 0 }
    }

    /// Registra uma amostra. As primeiras `WARMUP` são descartadas; após `N` válidas,
    /// novas amostras são ignoradas.
    #[inline(always)]
    pub fn push(&mut self, v: i32) {
        if self.seen < WARMUP {
            self.seen += 1;
            return;
        }
        if self.len < N {
            self.buf[self.len] = v;
            self.len += 1;
        }
    }

    #[inline(always)]
    pub fn is_full(&self) -> bool {
        self.len == N
    }

    pub fn len(&self) -> usize {
        self.len
    }

    /// Amostras na ordem de coleta (use antes de `summary`, que ordena in-place).
    pub fn samples(&self) -> &[i32] {
        &self.buf[..self.len]
    }

    /// Ordena as amostras in-place e calcula o resumo.
    pub fn summary(&mut self) -> Summary {
        if self.len == 0 {
            return Summary::EMPTY;
        }
        let s = &mut self.buf[..self.len];
        s.sort_unstable();
        let n = s.len() as i64;
        let sum: i64 = s.iter().map(|&v| v as i64).sum();
        let mean = sum / n;
        let var: u64 = (s.iter().map(|&v| { let d = v as i64 - mean; d * d }).sum::<i64>() / n) as u64;
        Summary {
            n: s.len() as u32,
            min: s[0],
            max: s[s.len() - 1],
            mean: mean as i32,
            std: isqrt(var),
            p50: percentile(s, 50),
            p90: percentile(s, 90),
            p99: percentile(s, 99),
        }
    }
}

fn percentile(sorted: &[i32], p: usize) -> i32 {
    sorted[(p * (sorted.len() - 1) + 50) / 100]
}

fn isqrt(n: u64) -> u32 {
    if n < 2 {
        return n as u32;
    }
    let mut x = n;
    let mut y = (x + 1) / 2;
    while y < x {
        x = y;
        y = (x + n / x) / 2;
    }
    x as u32
}