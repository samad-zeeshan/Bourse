//! Three threads joined by two SPSC rings: a reader feeds commands, one matcher owns the book, a writer drains events.
//!
//! The matcher is the only thread that touches the book, so the output order depends only on the input order.

use std::thread;

use crate::affinity::pin_current;
use crate::book::{Book, Config};
use crate::spsc::{channel, Producer};
use crate::types::*;

#[derive(Clone, Copy)]
enum In {
    Cmd(Command),
    End,
}

#[derive(Clone, Copy)]
enum Out {
    Ev(Event),
    End,
}

struct RingSink<'a>(&'a mut Producer<Out>);

impl EventSink for RingSink<'_> {
    #[inline(always)]
    fn on(&mut self, e: Event) {
        self.0.push(Out::Ev(e));
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Pins {
    pub reader: Option<usize>,
    pub matcher: Option<usize>,
    pub writer: Option<usize>,
}

/// Run `input` through a fresh book on its own thread and hand every event to `on_event` on this thread.
pub fn run<I, F>(cfg: Config, input: I, ring: usize, pins: Pins, mut on_event: F) -> u64
where
    I: IntoIterator<Item = Command> + Send,
    I::IntoIter: Send,
    F: FnMut(&Event),
{
    let (mut in_tx, mut in_rx) = channel::<In>(ring);
    let (mut out_tx, mut out_rx) = channel::<Out>(ring);
    thread::scope(|s| {
        s.spawn(move || {
            if let Some(c) = pins.reader {
                pin_current(c);
            }
            for c in input {
                in_tx.push(In::Cmd(c));
            }
            in_tx.push(In::End);
        });
        s.spawn(move || {
            if let Some(c) = pins.matcher {
                pin_current(c);
            }
            let mut book = Book::new(cfg);
            let mut sink = RingSink(&mut out_tx);
            loop {
                match in_rx.pop() {
                    In::Cmd(c) => book.process(&c, &mut sink),
                    In::End => break,
                }
            }
            out_tx.push(Out::End);
        });
        if let Some(c) = pins.writer {
            pin_current(c);
        }
        let mut n = 0u64;
        loop {
            match out_rx.pop() {
                Out::Ev(e) => {
                    n += 1;
                    on_event(&e);
                }
                Out::End => break,
            }
        }
        n
    })
}
