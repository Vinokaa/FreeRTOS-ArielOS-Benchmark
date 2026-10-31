//! Sinalizadores simples para sincronizar a inicialização entre tarefas/threads.

use core::sync::atomic::{AtomicBool, Ordering};

pub struct Flag(AtomicBool);

impl Flag {
    pub const fn new() -> Self {
        Self(AtomicBool::new(false))
    }

    pub fn set(&self) {
        self.0.store(true, Ordering::Release);
    }

    pub fn is_set(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }

    /// Espera em uma *thread* (polling de 1 ms; fora de qualquer janela de medição).
    pub fn wait(&self) {
        while !self.is_set() {
            ariel_os::thread::sleep(ariel_os::time::Duration::from_millis(1));
        }
    }
}