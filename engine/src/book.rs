//! Price-time priority limit order book and the matcher that runs against it.
//!
//! Every structure is sized in `Book::new`. After that, processing a command never touches the allocator.

use crate::bitset::LevelBits;
use crate::idmap::IdMap;
use crate::types::*;

const NIL: u32 = u32::MAX;
const NONE: usize = usize::MAX;

#[derive(Clone, Copy, Debug)]
pub struct Config {
    /// Lowest price the book accepts, in ticks.
    pub min_price: Price,
    /// Number of tick levels on each side. Prices at or above `min_price + levels` are rejected.
    pub levels: usize,
    /// Most orders that can rest at once, both sides together.
    pub max_orders: usize,
    pub stp: Stp,
}

impl Default for Config {
    fn default() -> Config {
        Config { min_price: 0, levels: 1 << 16, max_orders: 1 << 20, stp: Stp::Off }
    }
}

#[derive(Clone, Copy)]
struct Node {
    id: OrderId,
    qty: Qty,
    owner: u32,
    level: u32,
    prev: u32,
    next: u32,
    side: Side,
}

#[derive(Clone, Copy)]
struct Level {
    head: u32,
    tail: u32,
    qty: Qty,
    count: u32,
}

const EMPTY_LEVEL: Level = Level { head: NIL, tail: NIL, qty: 0, count: 0 };

struct Ladder {
    levels: Box<[Level]>,
    bits: LevelBits,
    best: usize,
}

impl Ladder {
    fn new(n: usize) -> Ladder {
        Ladder { levels: vec![EMPTY_LEVEL; n].into_boxed_slice(), bits: LevelBits::new(n), best: NONE }
    }
}

pub struct Book {
    cfg: Config,
    nodes: Box<[Node]>,
    free: u32,
    live: usize,
    ids: IdMap,
    bids: Ladder,
    asks: Ladder,
    seq: u64,
}

impl Book {
    pub fn new(cfg: Config) -> Book {
        assert!(cfg.max_orders > 0 && cfg.max_orders < NIL as usize);
        assert!(cfg.levels > 0 && cfg.levels < u32::MAX as usize);
        let blank = Node { id: 0, qty: 0, owner: 0, level: 0, prev: NIL, next: NIL, side: Side::Buy };
        let mut nodes = vec![blank; cfg.max_orders].into_boxed_slice();
        for (i, n) in nodes.iter_mut().enumerate() {
            n.next = if i + 1 < cfg.max_orders { (i + 1) as u32 } else { NIL };
        }
        Book {
            cfg,
            nodes,
            free: 0,
            live: 0,
            ids: IdMap::with_capacity(cfg.max_orders),
            bids: Ladder::new(cfg.levels),
            asks: Ladder::new(cfg.levels),
            seq: 0,
        }
    }

    pub fn config(&self) -> &Config {
        &self.cfg
    }

    /// Sequence number of the last event emitted.
    pub fn seq(&self) -> u64 {
        self.seq
    }

    pub fn live_orders(&self) -> usize {
        self.live
    }

    #[inline(always)]
    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    #[inline(always)]
    fn in_range(&self, price: Price) -> bool {
        price >= self.cfg.min_price && price - self.cfg.min_price < self.cfg.levels as i64
    }

    #[inline(always)]
    fn price_of(&self, level: usize) -> Price {
        self.cfg.min_price + level as i64
    }

    pub fn process<S: EventSink>(&mut self, cmd: &Command, sink: &mut S) {
        match *cmd {
            Command::Limit { id, owner, side, price, qty, tif } => {
                if let Some(r) = self.validate_new(id, qty, Some(price)) {
                    let seq = self.next_seq();
                    return sink.on(Event::Rejected { seq, id, reason: r });
                }
                let rem = self.take(id, owner, side, Some(price), qty, sink);
                if rem > 0 {
                    match tif {
                        Tif::Ioc => {
                            let seq = self.next_seq();
                            sink.on(Event::Cancelled { seq, id, qty: rem, reason: CancelReason::IocRemainder });
                        }
                        Tif::Gtc => self.rest(id, owner, side, price, rem, sink),
                    }
                }
            }
            Command::Market { id, owner, side, qty } => {
                if let Some(r) = self.validate_new(id, qty, None) {
                    let seq = self.next_seq();
                    return sink.on(Event::Rejected { seq, id, reason: r });
                }
                let rem = self.take(id, owner, side, None, qty, sink);
                if rem > 0 {
                    let seq = self.next_seq();
                    sink.on(Event::Cancelled { seq, id, qty: rem, reason: CancelReason::NoLiquidity });
                }
            }
            Command::Cancel { id } => match self.ids.get(id) {
                None => {
                    let seq = self.next_seq();
                    sink.on(Event::Rejected { seq, id, reason: RejectReason::UnknownId });
                }
                Some(slot) => {
                    let qty = self.nodes[slot as usize].qty;
                    self.unlink(slot);
                    let seq = self.next_seq();
                    sink.on(Event::Cancelled { seq, id, qty, reason: CancelReason::User });
                }
            },
            Command::Replace { id, price, qty } => self.replace(id, price, qty, sink),
        }
    }

    fn validate_new(&self, id: OrderId, qty: Qty, price: Option<Price>) -> Option<RejectReason> {
        if id == u64::MAX {
            Some(RejectReason::InvalidId)
        } else if qty == 0 {
            Some(RejectReason::ZeroQty)
        } else if price.is_some_and(|p| !self.in_range(p)) {
            Some(RejectReason::PriceOutOfRange)
        } else if self.ids.get(id).is_some() {
            // Only live ids are checked. Remembering every id ever seen would need unbounded memory.
            Some(RejectReason::DuplicateId)
        } else {
            None
        }
    }

    fn replace<S: EventSink>(&mut self, id: OrderId, price: Price, qty: Qty, sink: &mut S) {
        let reject = match self.ids.get(id) {
            None => Some(RejectReason::UnknownId),
            Some(_) if qty == 0 => Some(RejectReason::ZeroQty),
            Some(_) if !self.in_range(price) => Some(RejectReason::PriceOutOfRange),
            Some(_) => None,
        };
        if let Some(reason) = reject {
            let seq = self.next_seq();
            return sink.on(Event::Rejected { seq, id, reason });
        }
        let slot = self.ids.get(id).unwrap();
        let n = self.nodes[slot as usize];
        let same_price = self.price_of(n.level as usize) == price;
        if same_price && qty <= n.qty {
            // A size cut at the same price keeps its place in the queue. Anything else is a new order
            // for priority purposes, otherwise a trader could grow an order without going to the back.
            let lv = match n.side {
                Side::Buy => &mut self.bids.levels[n.level as usize],
                Side::Sell => &mut self.asks.levels[n.level as usize],
            };
            lv.qty -= n.qty - qty;
            self.nodes[slot as usize].qty = qty;
            let seq = self.next_seq();
            return sink.on(Event::Replaced { seq, id, price, qty });
        }
        self.unlink(slot);
        let seq = self.next_seq();
        sink.on(Event::Replaced { seq, id, price, qty });
        let rem = self.take(id, n.owner, n.side, Some(price), qty, sink);
        if rem > 0 {
            self.rest(id, n.owner, n.side, price, rem, sink);
        }
    }

    /// Match an incoming order against the opposite side. Returns the quantity left over.
    #[inline]
    fn take<S: EventSink>(&mut self, taker: OrderId, owner: u32, side: Side, limit: Option<Price>, qty: Qty, sink: &mut S) -> Qty {
        let mut rem = qty;
        while rem > 0 {
            let best = match side {
                Side::Buy => self.asks.best,
                Side::Sell => self.bids.best,
            };
            if best == NONE {
                break;
            }
            let price = self.price_of(best);
            let crosses = match (side, limit) {
                (_, None) => true,
                (Side::Buy, Some(l)) => price <= l,
                (Side::Sell, Some(l)) => price >= l,
            };
            if !crosses {
                break;
            }
            let head = match side {
                Side::Buy => self.asks.levels[best].head,
                Side::Sell => self.bids.levels[best].head,
            };
            let maker = self.nodes[head as usize];
            if self.cfg.stp != Stp::Off && owner != 0 && maker.owner == owner {
                if self.cfg.stp == Stp::CancelIncoming {
                    let seq = self.next_seq();
                    sink.on(Event::Cancelled { seq, id: taker, qty: rem, reason: CancelReason::SelfTrade });
                    return 0;
                }
                self.unlink(head);
                let seq = self.next_seq();
                sink.on(Event::Cancelled { seq, id: maker.id, qty: maker.qty, reason: CancelReason::SelfTrade });
                continue;
            }
            let q = rem.min(maker.qty);
            rem -= q;
            self.nodes[head as usize].qty -= q;
            match side {
                Side::Buy => self.asks.levels[best].qty -= q,
                Side::Sell => self.bids.levels[best].qty -= q,
            }
            let seq = self.next_seq();
            sink.on(Event::Trade { seq, taker, maker: maker.id, taker_side: side, price, qty: q });
            if maker.qty == q {
                self.unlink(head);
            }
        }
        rem
    }

    fn rest<S: EventSink>(&mut self, id: OrderId, owner: u32, side: Side, price: Price, qty: Qty, sink: &mut S) {
        if self.free == NIL {
            let seq = self.next_seq();
            return sink.on(Event::Cancelled { seq, id, qty, reason: CancelReason::BookFull });
        }
        let slot = self.free;
        self.free = self.nodes[slot as usize].next;
        let li = (price - self.cfg.min_price) as usize;
        let ladder = match side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        };
        let lv = &mut ladder.levels[li];
        let tail = lv.tail;
        self.nodes[slot as usize] = Node { id, qty, owner, level: li as u32, prev: tail, next: NIL, side };
        if tail == NIL {
            lv.head = slot;
            ladder.bits.set(li);
        } else {
            self.nodes[tail as usize].next = slot;
        }
        lv.tail = slot;
        lv.qty += qty;
        lv.count += 1;
        let better = match side {
            Side::Buy => ladder.best == NONE || li > ladder.best,
            Side::Sell => ladder.best == NONE || li < ladder.best,
        };
        if better {
            ladder.best = li;
        }
        self.ids.insert(id, slot);
        self.live += 1;
        let seq = self.next_seq();
        sink.on(Event::Rested { seq, id, side, price, qty });
    }

    /// Take a resting order out of its level, fix the best price if the level emptied, and free the slot.
    fn unlink(&mut self, slot: u32) {
        let n = self.nodes[slot as usize];
        let li = n.level as usize;
        let ladder = match n.side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        };
        let lv = &mut ladder.levels[li];
        if n.prev == NIL {
            lv.head = n.next;
        } else {
            self.nodes[n.prev as usize].next = n.next;
        }
        if n.next == NIL {
            lv.tail = n.prev;
        } else {
            self.nodes[n.next as usize].prev = n.prev;
        }
        lv.qty -= n.qty;
        lv.count -= 1;
        if lv.count == 0 {
            ladder.bits.clear(li);
            if ladder.best == li {
                ladder.best = match n.side {
                    Side::Buy => {
                        if li == 0 {
                            NONE
                        } else {
                            ladder.bits.next_down(li - 1).unwrap_or(NONE)
                        }
                    }
                    Side::Sell => ladder.bits.next_up(li + 1).unwrap_or(NONE),
                };
            }
        }
        self.ids.remove(n.id);
        self.nodes[slot as usize].next = self.free;
        self.free = slot;
        self.live -= 1;
    }

    pub fn best_bid(&self) -> Option<(Price, Qty)> {
        (self.bids.best != NONE).then(|| (self.price_of(self.bids.best), self.bids.levels[self.bids.best].qty))
    }

    pub fn best_ask(&self) -> Option<(Price, Qty)> {
        (self.asks.best != NONE).then(|| (self.price_of(self.asks.best), self.asks.levels[self.asks.best].qty))
    }

    /// Fill `out` with up to `out.len()` levels from the best price outward. Returns how many were filled.
    pub fn depth(&self, side: Side, out: &mut [(Price, Qty)]) -> usize {
        let ladder = match side {
            Side::Buy => &self.bids,
            Side::Sell => &self.asks,
        };
        let mut n = 0;
        let mut cur = ladder.best;
        while cur != NONE && n < out.len() {
            out[n] = (self.price_of(cur), ladder.levels[cur].qty);
            n += 1;
            cur = match side {
                Side::Buy if cur == 0 => NONE,
                Side::Buy => ladder.bits.next_down(cur - 1).unwrap_or(NONE),
                Side::Sell => ladder.bits.next_up(cur + 1).unwrap_or(NONE),
            };
        }
        n
    }

    /// Side, price and remaining quantity of a live order.
    pub fn order(&self, id: OrderId) -> Option<(Side, Price, Qty)> {
        self.ids.get(id).map(|s| {
            let n = &self.nodes[s as usize];
            (n.side, self.price_of(n.level as usize), n.qty)
        })
    }

    /// Ids resting at one price, front of the queue first. Allocates, so it is for tests and tools only.
    pub fn queue(&self, side: Side, price: Price) -> Vec<(OrderId, Qty)> {
        let mut out = Vec::new();
        if !self.in_range(price) {
            return out;
        }
        let ladder = match side {
            Side::Buy => &self.bids,
            Side::Sell => &self.asks,
        };
        let mut cur = ladder.levels[(price - self.cfg.min_price) as usize].head;
        while cur != NIL {
            out.push((self.nodes[cur as usize].id, self.nodes[cur as usize].qty));
            cur = self.nodes[cur as usize].next;
        }
        out
    }

    /// Walk every structure and check that they agree. O(levels + orders), for tests and the fuzzer.
    pub fn check_structure(&self) -> Result<(), String> {
        let mut seen = 0usize;
        for (side, ladder) in [(Side::Buy, &self.bids), (Side::Sell, &self.asks)] {
            let mut best = NONE;
            for (li, lv) in ladder.levels.iter().enumerate() {
                let mut cur = lv.head;
                let mut prev = NIL;
                let (mut qty, mut count) = (0u64, 0u32);
                while cur != NIL {
                    let n = &self.nodes[cur as usize];
                    if n.prev != prev || n.level as usize != li || n.side != side || n.qty == 0 {
                        return Err(format!("bad node {} at level {li}", n.id));
                    }
                    if self.ids.get(n.id) != Some(cur) {
                        return Err(format!("id map disagrees for {}", n.id));
                    }
                    qty += n.qty;
                    count += 1;
                    prev = cur;
                    cur = n.next;
                }
                if lv.tail != prev || lv.qty != qty || lv.count != count {
                    return Err(format!("level {li} totals wrong"));
                }
                let bit = ladder.bits.next_up(li) == Some(li);
                if bit != (count > 0) {
                    return Err(format!("bit for level {li} is {bit} with {count} orders"));
                }
                if count > 0 && (side == Side::Buy || best == NONE) {
                    best = li;
                }
                seen += count as usize;
            }
            if best != ladder.best {
                return Err(format!("best on {side:?} is {} but should be {}", ladder.best, best));
            }
        }
        if seen != self.live || self.ids.len() != self.live {
            return Err(format!("{seen} orders linked, {} live, {} in id map", self.live, self.ids.len()));
        }
        if let (Some((b, _)), Some((a, _))) = (self.best_bid(), self.best_ask()) {
            if b >= a {
                return Err(format!("crossed book: bid {b} ask {a}"));
            }
        }
        Ok(())
    }
}
