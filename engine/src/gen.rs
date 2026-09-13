//! Seeded synthetic order flow for benchmarks, determinism checks and the no-allocation test.
//!
//! The mix leans on adds and cancels near the touch, which is what real feeds look like.

use crate::types::*;

pub struct FlowGen {
    rng: u64,
    next_id: u64,
    mid: i64,
    recent: Vec<u64>,
    pos: usize,
    pub min_price: i64,
    pub levels: i64,
}

impl FlowGen {
    pub fn new(seed: u64, min_price: i64, levels: i64) -> FlowGen {
        FlowGen {
            rng: seed.max(1),
            next_id: 1,
            mid: min_price + levels / 2,
            recent: vec![0; 4096],
            pos: 0,
            min_price,
            levels,
        }
    }

    #[inline]
    fn rand(&mut self) -> u64 {
        // xorshift64*. Deterministic across platforms, which the determinism test depends on.
        self.rng ^= self.rng >> 12;
        self.rng ^= self.rng << 25;
        self.rng ^= self.rng >> 27;
        self.rng.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.rand() % n
    }

    /// Ticks away from the touch, mostly 0 to 3 with a thin tail.
    fn offset(&mut self) -> i64 {
        let mut k = 0;
        while k < 40 && self.below(100) < 60 {
            k += 1;
        }
        k
    }

    fn remembered(&mut self) -> u64 {
        let i = self.below(self.recent.len() as u64) as usize;
        self.recent[i]
    }

    pub fn next_command(&mut self) -> Command {
        if self.below(1000) < 5 {
            let step = if self.below(2) == 0 { 1 } else { -1 };
            self.mid = (self.mid + step).clamp(self.min_price + 64, self.min_price + self.levels - 64);
        }
        let side = if self.below(2) == 0 { Side::Buy } else { Side::Sell };
        let r = self.below(100);
        let qty = 1 + self.below(10) * self.below(10);
        if r < 52 {
            let id = self.next_id;
            self.next_id += 1;
            self.recent[self.pos] = id;
            self.pos = (self.pos + 1) & (self.recent.len() - 1);
            // One add in ten is priced through the touch, so the matcher has real work.
            let aggressive = self.below(10) == 0;
            let off = self.offset();
            let price = match (side, aggressive) {
                (Side::Buy, false) => self.mid - 1 - off,
                (Side::Sell, false) => self.mid + 1 + off,
                (Side::Buy, true) => self.mid + off,
                (Side::Sell, true) => self.mid - off,
            };
            let tif = if self.below(20) == 0 { Tif::Ioc } else { Tif::Gtc };
            Command::Limit { id, owner: (id % 8) as u32, side, price, qty, tif }
        } else if r < 90 {
            Command::Cancel { id: self.remembered() }
        } else if r < 95 {
            let id = self.next_id;
            self.next_id += 1;
            Command::Market { id, owner: 0, side, qty }
        } else {
            let id = self.remembered();
            let off = self.offset();
            Command::Replace { id, price: self.mid + if side == Side::Buy { -1 - off } else { 1 + off }, qty }
        }
    }
}
