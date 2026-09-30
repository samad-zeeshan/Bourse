//! The engine compiled for the browser, so the demo page replays orders through the same code as the benchmarks.

use bourse::codec::command_from_text;
use bourse::*;
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct Engine {
    book: Book,
    ev: Vec<Event>,
    events: u64,
    trades: u64,
    rejects: u64,
}

#[wasm_bindgen]
impl Engine {
    #[wasm_bindgen(constructor)]
    pub fn new(min_price: f64, levels: u32, max_orders: u32) -> Engine {
        let cfg = Config { min_price: min_price as i64, levels: levels as usize, max_orders: max_orders as usize, stp: Stp::Off };
        Engine { book: Book::new(cfg), ev: Vec::with_capacity(64), events: 0, trades: 0, rejects: 0 }
    }

    /// Apply newline-separated commands in the replay tool's text format. Returns how many were applied.
    pub fn apply(&mut self, text: &str) -> u32 {
        let mut n = 0;
        for line in text.lines().filter(|l| !l.is_empty()) {
            let Ok(c) = command_from_text(line) else { continue };
            self.ev.clear();
            self.book.process(&c, &mut self.ev);
            for e in &self.ev {
                self.events += 1;
                match e {
                    Event::Trade { .. } => self.trades += 1,
                    Event::Rejected { .. } => self.rejects += 1,
                    _ => {}
                }
            }
            n += 1;
        }
        n
    }

    /// Up to `n` levels from the best price outward, flattened as price, quantity, price, quantity.
    pub fn depth(&self, side: u8, n: u32) -> Vec<f64> {
        let mut buf = vec![(0i64, 0u64); n as usize];
        let got = self.book.depth(if side == 0 { Side::Buy } else { Side::Sell }, &mut buf);
        buf[..got].iter().flat_map(|&(p, q)| [p as f64, q as f64]).collect()
    }

    pub fn events(&self) -> f64 {
        self.events as f64
    }

    pub fn trades(&self) -> f64 {
        self.trades as f64
    }

    pub fn rejects(&self) -> f64 {
        self.rejects as f64
    }

    pub fn crossed(&self) -> bool {
        matches!((self.book.best_bid(), self.book.best_ask()), (Some((b, _)), Some((a, _))) if b >= a)
    }
}
