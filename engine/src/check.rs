//! An independent shadow of the book, rebuilt only from commands and events, that checks the stated invariants.
//!
//! It never reads the engine's internals, so a bug in the book cannot hide itself here.

use std::collections::{BTreeMap, HashMap, VecDeque};

use crate::book::Config;
use crate::types::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Violation {
    pub invariant: &'static str,
    pub detail: String,
}

fn v(invariant: &'static str, detail: String) -> Violation {
    Violation { invariant, detail }
}

pub const SEQUENCE: &str = "unique increasing sequence";
pub const CROSSED: &str = "no crossed book";
pub const CONSERVED: &str = "quantity conserved";
pub const FIFO: &str = "price-time priority";
pub const CANCEL_EXACT: &str = "cancel removes exactly the resting quantity";
pub const WELL_FORMED: &str = "events follow from the command";

pub struct Shadow {
    cfg: Config,
    next_seq: u64,
    resting: HashMap<OrderId, (Side, Price, Qty)>,
    bids: BTreeMap<Price, VecDeque<OrderId>>,
    asks: BTreeMap<Price, VecDeque<OrderId>>,
}

impl Shadow {
    pub fn new(cfg: Config) -> Shadow {
        Shadow { cfg, next_seq: 1, resting: HashMap::new(), bids: BTreeMap::new(), asks: BTreeMap::new() }
    }

    fn book(&mut self, side: Side) -> &mut BTreeMap<Price, VecDeque<OrderId>> {
        match side {
            Side::Buy => &mut self.bids,
            Side::Sell => &mut self.asks,
        }
    }

    fn best(&self, side: Side) -> Option<Price> {
        match side {
            Side::Buy => self.bids.keys().next_back().copied(),
            Side::Sell => self.asks.keys().next().copied(),
        }
    }

    fn remove(&mut self, id: OrderId) {
        let (side, price, _) = self.resting.remove(&id).unwrap();
        let book = self.book(side);
        let q = book.get_mut(&price).unwrap();
        q.retain(|&x| x != id);
        if q.is_empty() {
            book.remove(&price);
        }
    }

    fn add(&mut self, id: OrderId, side: Side, price: Price, qty: Qty) {
        self.resting.insert(id, (side, price, qty));
        self.book(side).entry(price).or_default().push_back(id);
    }

    /// The order at the front of the best level on `side`, which is the only one allowed to trade next.
    fn front(&self, side: Side) -> Option<OrderId> {
        let book = match side {
            Side::Buy => &self.bids,
            Side::Sell => &self.asks,
        };
        let level = match side {
            Side::Buy => book.values().next_back(),
            Side::Sell => book.values().next(),
        };
        level.and_then(|q| q.front().copied())
    }

    pub fn step(&mut self, cmd: &Command, events: &[Event]) -> Result<(), Violation> {
        for e in events {
            if e.seq() != self.next_seq {
                return Err(v(SEQUENCE, format!("expected seq {} got {:?}", self.next_seq, e)));
            }
            self.next_seq += 1;
        }
        if let [Event::Rejected { id, reason, .. }] = events {
            return self.check_reject(cmd, *id, *reason);
        }
        match *cmd {
            Command::Limit { id, owner: _, side, price, qty, tif } => self.check_take(id, side, Some(price), qty, tif == Tif::Gtc, events),
            Command::Market { id, side, qty, .. } => self.check_take(id, side, None, qty, false, events),
            Command::Cancel { id } => match (events, self.resting.get(&id).copied()) {
                ([Event::Cancelled { id: eid, qty, reason: CancelReason::User, .. }], Some((_, _, rq))) if *eid == id => {
                    if *qty != rq {
                        return Err(v(CANCEL_EXACT, format!("cancel of {id} removed {qty}, resting was {rq}")));
                    }
                    self.remove(id);
                    Ok(())
                }
                _ => Err(v(WELL_FORMED, format!("{cmd:?} gave {events:?}"))),
            },
            Command::Replace { id, price, qty } => {
                let Some((side, old_price, old_qty)) = self.resting.get(&id).copied() else {
                    return Err(v(WELL_FORMED, format!("replace of dead {id} gave {events:?}")));
                };
                match events.first() {
                    Some(Event::Replaced { id: eid, price: p, qty: q, .. }) if *eid == id && *p == price && *q == qty => {}
                    _ => return Err(v(WELL_FORMED, format!("{cmd:?} gave {events:?}"))),
                }
                if price == old_price && qty <= old_qty {
                    if events.len() != 1 {
                        return Err(v(WELL_FORMED, format!("size cut produced extra events {events:?}")));
                    }
                    self.resting.get_mut(&id).unwrap().2 = qty;
                    return Ok(());
                }
                self.remove(id);
                self.check_take(id, side, Some(price), qty, true, &events[1..])
            }
        }
    }

    fn check_reject(&self, cmd: &Command, id: OrderId, reason: RejectReason) -> Result<(), Violation> {
        let live = self.resting.contains_key(&id);
        let in_range = |p: Price| p >= self.cfg.min_price && p - self.cfg.min_price < self.cfg.levels as i64;
        let ok = id == cmd.id()
            && match (reason, *cmd) {
                (RejectReason::InvalidId, _) => id == u64::MAX,
                (RejectReason::ZeroQty, Command::Limit { qty, .. } | Command::Market { qty, .. } | Command::Replace { qty, .. }) => qty == 0,
                (RejectReason::PriceOutOfRange, Command::Limit { price, .. } | Command::Replace { price, .. }) => !in_range(price),
                (RejectReason::DuplicateId, Command::Limit { .. } | Command::Market { .. }) => live,
                (RejectReason::UnknownId, Command::Cancel { .. } | Command::Replace { .. }) => !live,
                _ => false,
            };
        if ok {
            Ok(())
        } else {
            Err(v(WELL_FORMED, format!("{cmd:?} rejected as {reason:?}")))
        }
    }

    fn check_take(&mut self, id: OrderId, side: Side, limit: Option<Price>, qty: Qty, may_rest: bool, events: &[Event]) -> Result<(), Violation> {
        let mut pending = qty;
        let opp = side.opposite();
        for e in events {
            match *e {
                Event::Trade { taker, maker, taker_side, price, qty: q, .. } => {
                    if taker != id || taker_side != side || q == 0 || q > pending {
                        return Err(v(CONSERVED, format!("trade {e:?} with {pending} pending")));
                    }
                    if self.best(opp) != Some(price) || limit.is_some_and(|l| if side == Side::Buy { price > l } else { price < l }) {
                        return Err(v(FIFO, format!("trade {e:?} not at the best crossing price {:?}", self.best(opp))));
                    }
                    if self.front(opp) != Some(maker) {
                        return Err(v(FIFO, format!("trade {e:?} hit {maker}, front of queue is {:?}", self.front(opp))));
                    }
                    let entry = self.resting.get_mut(&maker).unwrap();
                    if q > entry.2 {
                        return Err(v(CONSERVED, format!("trade {e:?} exceeds maker's {}", entry.2)));
                    }
                    entry.2 -= q;
                    pending -= q;
                    if entry.2 == 0 {
                        self.remove(maker);
                    }
                }
                Event::Cancelled { id: cid, qty: q, reason: CancelReason::SelfTrade, .. } if cid != id => {
                    if self.front(opp) != Some(cid) || self.resting[&cid].2 != q {
                        return Err(v(CANCEL_EXACT, format!("self-trade cancel {e:?} is not the resting front")));
                    }
                    self.remove(cid);
                }
                Event::Cancelled { id: cid, qty: q, reason, .. } if cid == id => {
                    let allowed = match reason {
                        CancelReason::IocRemainder => limit.is_some() && !may_rest,
                        CancelReason::NoLiquidity => limit.is_none(),
                        CancelReason::SelfTrade => true,
                        CancelReason::BookFull => may_rest && self.resting.len() == self.cfg.max_orders,
                        CancelReason::User => false,
                    };
                    if !allowed || q != pending {
                        return Err(v(CONSERVED, format!("{e:?} with {pending} pending")));
                    }
                    pending = 0;
                }
                Event::Rested { id: rid, side: s, price, qty: q, .. } if rid == id => {
                    if !may_rest || s != side || Some(price) != limit || q != pending {
                        return Err(v(CONSERVED, format!("{e:?} with {pending} pending")));
                    }
                    self.add(id, side, price, q);
                    pending = 0;
                }
                _ => return Err(v(WELL_FORMED, format!("unexpected {e:?} for order {id}"))),
            }
        }
        if pending != 0 {
            return Err(v(CONSERVED, format!("order {id} left {pending} unaccounted")));
        }
        if let (Some(b), Some(a)) = (self.best(Side::Buy), self.best(Side::Sell)) {
            if b >= a {
                return Err(v(CROSSED, format!("bid {b} ask {a} after order {id}")));
            }
        }
        Ok(())
    }

    /// Resting orders the shadow believes in, for comparing against the engine's own view.
    pub fn resting(&self) -> &HashMap<OrderId, (Side, Price, Qty)> {
        &self.resting
    }
}
