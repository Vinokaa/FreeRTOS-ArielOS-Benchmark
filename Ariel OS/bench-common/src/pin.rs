//! Pino de saída compartilhável entre contextos.
//!
//! No Ariel OS, periféricos só chegam a tarefas assíncronas (`#[task(peripherals)]`),
//! não a threads. Uma tarefa de init instala o `Output` aqui e as threads o acionam.

use core::cell::RefCell;

use ariel_os::gpio::Output;
use critical_section::Mutex;

pub struct PinSlot(Mutex<RefCell<Option<Output<'static>>>>);

impl PinSlot {
    pub const fn new() -> Self {
        Self(Mutex::new(RefCell::new(None)))
    }

    pub fn install(&self, out: Output<'static>) {
        critical_section::with(|cs| {
            self.0.borrow_ref_mut(cs).replace(out);
        });
    }

    /// Nível lógico: `true` = alto. (Seção crítica de ~centenas de ns.)
    #[inline]
    pub fn set(&self, high: bool) {
        critical_section::with(|cs| {
            if let Some(o) = self.0.borrow_ref_mut(cs).as_mut() {
                if high {
                    o.set_high();
                } else {
                    o.set_low();
                }
            }
        });
    }
}