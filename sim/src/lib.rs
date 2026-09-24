//! Calibrated background flow driven through the real engine, and metaorders executed against it.
//!
//! Every random draw for the background comes from its own seeded stream, so a run with a metaorder and
//! the same run without it see identical arrivals. Impact is the difference between the two.

use bourse::*;
use serde_json::Value;

pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed ^ 0x5DEE_CE66_D1CE_4E5B)
    }
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    pub fn uniform(&mut self) -> f64 {
        ((self.next() >> 11) as f64 + 0.5) / (1u64 << 53) as f64
    }
    pub fn exp(&mut self, rate: f64) -> f64 {
        -self.uniform().ln() / rate
    }
}

#[derive(Clone)]
pub struct Params {
    pub mu: [f64; 2],
    pub alpha: [[[f64; 2]; 2]; 2],
    pub beta: [f64; 2],
    pub add_rate: f64,
    pub cancel_rate: f64,
    pub add_offset: Vec<f64>,
    pub add_size_q: Vec<f64>,
    pub mo_size_q: Vec<f64>,
    /// Scales the add and cancel rates together. The diffs undercount both, see flow/calibrate.py.
    pub liquidity: f64,
    /// Scales the cancel rate alone. Measured decreases include the ones trades cause, so they overstate cancels.
    pub cancel_scale: f64,
}

fn f(v: &Value) -> f64 {
    v.as_f64().unwrap()
}

impl Params {
    pub fn load(path: &str, day: &str) -> Params {
        let j: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let d = &j["days"][day];
        let bf = &j["book_flow"];
        let mut alpha = [[[0.0; 2]; 2]; 2];
        for i in 0..2 {
            for jj in 0..2 {
                for k in 0..2 {
                    alpha[i][jj][k] = f(&d["alpha"][i][jj][k]);
                }
            }
        }
        let arr = |v: &Value| v.as_array().unwrap().iter().map(f).collect::<Vec<_>>();
        Params {
            mu: [f(&d["mu"][0]), f(&d["mu"][1])],
            alpha,
            beta: [f(&d["beta"][0]), f(&d["beta"][1])],
            add_rate: f(&bf["add_rate_per_side"]),
            cancel_rate: f(&bf["size_decrease_rate_per_side"]),
            add_offset: arr(&bf["add_offset_probs"]),
            add_size_q: arr(&bf["add_size_lots_quantiles"]),
            // Market order sizes are recorded in ETH. The engine counts lots of 0.0001 ETH.
            mo_size_q: arr(&d["mo_size_quantiles"]).iter().map(|q| (q * 1e4).round().max(1.0)).collect(),
            liquidity: 1.0,
            cancel_scale: 1.0,
        }
    }
}

/// Draw from a distribution given by 1,001 evenly spaced quantiles, interpolating linearly between them.
/// Coarse quantiles with log interpolation invented far too many huge orders in the top bucket.
fn from_quantiles(q: &[f64], u: f64) -> u64 {
    let x = u * (q.len() - 1) as f64;
    let i = (x.floor() as usize).min(q.len() - 2);
    let w = x - i as f64;
    (q[i] + w * (q[i + 1] - q[i])).round().max(1.0) as u64
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind {
    MarketBuy,
    MarketSell,
    Add(Side),
    Cancel(Side),
}

/// Background arrival times for `seconds`, all exogenous to the book, merged in time order.
pub fn arrivals(p: &Params, seconds: f64, seed: u64) -> Vec<(f64, Kind)> {
    let mut out = Vec::new();
    // Hawkes market orders by thinning, two decay rates.
    let mut r = Rng::new(seed.wrapping_mul(3) + 1);
    let mut s = [[0.0f64; 2]; 2];
    let mut t = 0.0;
    loop {
        let lam = |s: &[[f64; 2]; 2], i: usize| p.mu[i] + (0..2).map(|j| (0..2).map(|k| p.alpha[i][j][k] * p.beta[k] * s[j][k]).sum::<f64>()).sum::<f64>();
        let bar = lam(&s, 0) + lam(&s, 1);
        let w = r.exp(bar);
        t += w;
        if t > seconds {
            break;
        }
        for row in s.iter_mut() {
            for (k, v) in row.iter_mut().enumerate() {
                *v *= (-p.beta[k] * w).exp();
            }
        }
        let (l0, l1) = (lam(&s, 0), lam(&s, 1));
        let u = r.uniform() * bar;
        if u <= l0 + l1 {
            let i = if u <= l0 { 0 } else { 1 };
            for k in 0..2 {
                s[i][k] += 1.0;
            }
            out.push((t, if i == 0 { Kind::MarketBuy } else { Kind::MarketSell }));
        }
    }
    let (add, cancel) = (p.add_rate * p.liquidity, p.cancel_rate * p.liquidity * p.cancel_scale);
    for (stream, side) in [(2u64, Side::Buy), (3, Side::Sell)] {
        let mut r = Rng::new(seed.wrapping_mul(7) + stream);
        let mut t = r.exp(add);
        while t < seconds {
            out.push((t, Kind::Add(side)));
            t += r.exp(add);
        }
        let mut r = Rng::new(seed.wrapping_mul(11) + stream);
        let mut t = r.exp(cancel);
        while t < seconds {
            out.push((t, Kind::Cancel(side)));
            t += r.exp(cancel);
        }
    }
    out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    out
}

pub struct Market {
    pub book: Book,
    live: [Vec<OrderId>; 2],
    pos: std::collections::HashMap<OrderId, (Side, usize)>,
    next_id: OrderId,
    pub last_mid2: i64,
    draws: Rng,
    ev: Vec<Event>,
    pub volume: u64,
    pub meta_filled: u64,
    pub meta_cost: i128,
    pub bg_requested: u64,
    anchor2: i64,
    pub bg_filled: u64,
}

pub const CENTER: i64 = 1 << 20;
const META_OWNER: u32 = 99;

impl Market {
    /// A fresh book seeded with a few orders on each of the first ten levels, around a fixed centre.
    pub fn new(p: &Params, seed: u64) -> Market {
        let cfg = Config { min_price: 0, levels: 1 << 21, max_orders: 1 << 20, stp: Stp::Off };
        let mut m = Market {
            book: Book::new(cfg),
            live: [Vec::new(), Vec::new()],
            pos: Default::default(),
            next_id: 1,
            last_mid2: 2 * CENTER + 1,
            draws: Rng::new(seed.wrapping_mul(13) + 5),
            ev: Vec::with_capacity(256),
            volume: 0,
            meta_filled: 0,
            meta_cost: 0,
            bg_requested: 0,
            anchor2: 2 * CENTER + 1,
            bg_filled: 0,
        };
        for lvl in 0..10 {
            for _ in 0..3 {
                let q = from_quantiles(&p.add_size_q, m.draws.uniform());
                m.send(Command::Limit { id: 0, owner: 1, side: Side::Buy, price: CENTER - lvl, qty: q, tif: Tif::Gtc });
                let q = from_quantiles(&p.add_size_q, m.draws.uniform());
                m.send(Command::Limit { id: 0, owner: 1, side: Side::Sell, price: CENTER + 1 + lvl, qty: q, tif: Tif::Gtc });
            }
        }
        m
    }

    fn idx(s: Side) -> usize {
        s as usize
    }

    /// Process one command, giving it a fresh id, and keep the live lists and volume in step.
    pub fn send(&mut self, mut c: Command) {
        let id = self.next_id;
        self.next_id += 1;
        match &mut c {
            Command::Limit { id: i, .. } | Command::Market { id: i, .. } => *i = id,
            _ => {}
        }
        self.ev.clear();
        let mut ev = std::mem::take(&mut self.ev);
        self.book.process(&c, &mut ev);
        let meta = matches!(c, Command::Market { owner: META_OWNER, .. });
        let bg_market = matches!(c, Command::Market { owner: 2, .. });
        if let Command::Market { owner: 2, qty, .. } = c {
            self.bg_requested += qty;
        }
        for e in &ev {
            match *e {
                Event::Rested { id, side, .. } => {
                    self.pos.insert(id, (side, self.live[Self::idx(side)].len()));
                    self.live[Self::idx(side)].push(id);
                }
                Event::Trade { maker, qty, price, taker_side, .. } => {
                    self.volume += qty;
                    if bg_market {
                        self.bg_filled += qty;
                    }
                    if meta {
                        self.meta_filled += qty;
                        self.meta_cost += (qty as i128) * (price as i128) * if taker_side == Side::Buy { 1 } else { -1 };
                    }
                    if self.book.order(maker).is_none() {
                        self.forget(maker);
                    }
                }
                Event::Cancelled { id, .. } => self.forget(id),
                _ => {}
            }
        }
        self.ev = ev;
        if let (Some((b, _)), Some((a, _))) = (self.book.best_bid(), self.book.best_ask()) {
            self.last_mid2 = a + b;
        }
    }

    fn forget(&mut self, id: OrderId) {
        if let Some((side, i)) = self.pos.remove(&id) {
            let v = &mut self.live[Self::idx(side)];
            v.swap_remove(i);
            if i < v.len() {
                self.pos.insert(v[i], (side, i));
            }
        }
    }

    /// Twice the mid price in ticks, so a one-tick spread stays an integer.
    /// Resting quantity within ten ticks of the touch, averaged over the two sides.
    pub fn depth10(&self) -> f64 {
        let mut buf = [(0i64, 0u64); 10];
        let mut tot = 0u64;
        for side in [Side::Buy, Side::Sell] {
            let n = self.book.depth(side, &mut buf);
            let touch = buf[0].0;
            tot += buf[..n].iter().filter(|(p, _)| (p - touch).abs() < 10).map(|x| x.1).sum::<u64>();
        }
        tot as f64 / 2.0
    }

    pub fn mid2(&self) -> i64 {
        self.last_mid2
    }

    pub fn background(&mut self, p: &Params, k: Kind, follow: bool) {
        let u = self.draws.uniform();
        let u2 = self.draws.uniform();
        match k {
            Kind::MarketBuy | Kind::MarketSell => {
                let side = if k == Kind::MarketBuy { Side::Buy } else { Side::Sell };
                let qty = from_quantiles(&p.mo_size_q, u);
                self.send(Command::Market { id: 0, owner: 2, side, qty });
            }
            Kind::Add(side) => {
                // Priced from the opposite best, one tick back, then the measured offset. With a one-tick
                // spread that is the same as the offset from its own best, which is how it was measured, and
                // when a spread opens the first adds close it. It is also what makes liquidity follow the
                // price after a metaorder moves it.
                let mut acc = 0.0;
                let mut off = p.add_offset.len() as i64 - 1;
                for (i, w) in p.add_offset.iter().enumerate() {
                    acc += w;
                    if u <= acc {
                        off = i as i64;
                        break;
                    }
                }
                let (bb, ba) = (self.book.best_bid().map(|x| x.0), self.book.best_ask().map(|x| x.0));
                let price = match (side, follow) {
                    (Side::Buy, true) => ba.or(bb.map(|b| b + 1)).unwrap_or(self.last_mid2 / 2 + 1) - 1 - off,
                    (Side::Sell, true) => bb.or(ba.map(|a| a - 1)).unwrap_or(self.last_mid2 / 2) + 1 + off,
                    // The ablation: priced from where the book stood before, so refills do not chase the price.
                    (Side::Buy, false) => self.anchor2 / 2 - off,
                    (Side::Sell, false) => self.anchor2 / 2 + 1 + off,
                };
                let qty = from_quantiles(&p.add_size_q, u2);
                self.send(Command::Limit { id: 0, owner: 1, side, price, qty, tif: Tif::Gtc });
            }
            Kind::Cancel(side) => {
                let v = &self.live[Self::idx(side)];
                if v.len() > 20 {
                    let id = v[(u * v.len() as f64) as usize % v.len()];
                    self.send(Command::Cancel { id });
                }
            }
        }
    }

    pub fn meta_child(&mut self, side: Side, qty: u64) {
        self.send(Command::Market { id: 0, owner: META_OWNER, side, qty });
    }
}

pub struct Meta {
    pub side: Side,
    pub total: u64,
    pub children: u32,
    pub start: f64,
    pub duration: f64,
}

pub struct RunResult {
    pub mid2_at: Vec<i64>,
    pub spread_at: Vec<i64>,
    pub depth10_mean: f64,
    pub bg_fill_ratio: f64,
    pub volume_in_window: u64,
    pub meta_filled: u64,
    pub commands: u64,
}

/// One run: background for `seconds`, optional metaorder, mid sampled at `sample_times`.
pub fn run(p: &Params, seed: u64, seconds: f64, meta: Option<&Meta>, freeze_replenish: bool, sample_times: &[f64], window: (f64, f64)) -> RunResult {
    let arr = arrivals(p, seconds, seed);
    let mut m = Market::new(p, seed);
    let mut children: Vec<(f64, u64)> = Vec::new();
    if let Some(mt) = meta {
        let n = mt.children.max(1) as u64;
        for c in 0..n {
            let q = mt.total / n + if c < mt.total % n { 1 } else { 0 };
            children.push((mt.start + mt.duration * c as f64 / n as f64, q));
        }
    }
    let mut ci = 0;
    let mut si = 0;
    let mut out = vec![0i64; sample_times.len()];
    let mut spread = vec![-1i64; sample_times.len()];
    let mut depth_sum = 0.0;
    let mut vol_start = None;
    let mut vol_end = None;
    let mut commands = 0u64;
    for &(t, k) in &arr {
        while si < sample_times.len() && sample_times[si] <= t {
            out[si] = m.mid2();
            if let (Some((b, _)), Some((a, _))) = (m.book.best_bid(), m.book.best_ask()) {
                spread[si] = a - b;
            }
            depth_sum += m.depth10();
            si += 1;
        }
        while ci < children.len() && children[ci].0 <= t {
            m.meta_child(meta.unwrap().side, children[ci].1);
            commands += 1;
            ci += 1;
        }
        if t >= window.0 && vol_start.is_none() {
            vol_start = Some(m.volume);
        }
        if t >= window.1 && vol_end.is_none() {
            vol_end = Some(m.volume);
        }
        // The ablation holds refills at the pre-window price inside the window, in both paired runs.
        let pinned = freeze_replenish && t >= window.0 && t < window.1;
        if !pinned {
            m.anchor2 = m.mid2();
        }
        m.background(p, k, !pinned);
        commands += 1;
    }
    while si < sample_times.len() {
        out[si] = m.mid2();
        si += 1;
    }
    RunResult { mid2_at: out, spread_at: spread, depth10_mean: depth_sum / sample_times.len().max(1) as f64, bg_fill_ratio: m.bg_filled as f64 / m.bg_requested.max(1) as f64, volume_in_window: vol_end.unwrap_or(m.volume) - vol_start.unwrap_or(m.volume), meta_filled: m.meta_filled, commands }
}
