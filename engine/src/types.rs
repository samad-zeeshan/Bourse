//! Orders going in and events coming out of the book.
//!
//! Both are small `Copy` structs so they can sit in a ring buffer slot without a heap pointer.

pub type OrderId = u64;
/// Price in whole ticks. The book never sees a fractional price.
pub type Price = i64;
/// Quantity in whole lots.
pub type Qty = u64;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Side {
    Buy = 0,
    Sell = 1,
}

impl Side {
    pub fn opposite(self) -> Side {
        match self {
            Side::Buy => Side::Sell,
            Side::Sell => Side::Buy,
        }
    }
    pub fn from_u8(v: u8) -> Option<Side> {
        match v {
            0 => Some(Side::Buy),
            1 => Some(Side::Sell),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Tif {
    /// Rest whatever does not fill.
    Gtc = 0,
    /// Immediate or cancel. Fill what crosses now, cancel the rest.
    Ioc = 1,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Command {
    Limit { id: OrderId, owner: u32, side: Side, price: Price, qty: Qty, tif: Tif },
    Market { id: OrderId, owner: u32, side: Side, qty: Qty },
    Cancel { id: OrderId },
    /// Replace the resting order's price and remaining quantity.
    Replace { id: OrderId, price: Price, qty: Qty },
}

impl Command {
    pub fn id(&self) -> OrderId {
        match *self {
            Command::Limit { id, .. } | Command::Market { id, .. } | Command::Cancel { id } | Command::Replace { id, .. } => id,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CancelReason {
    User = 0,
    IocRemainder = 1,
    NoLiquidity = 2,
    SelfTrade = 3,
    /// The remainder would have rested but every order slot is taken.
    BookFull = 4,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum RejectReason {
    DuplicateId = 0,
    UnknownId = 1,
    ZeroQty = 2,
    PriceOutOfRange = 3,
    /// `u64::MAX` is reserved as the empty marker in the id table.
    InvalidId = 4,
}

/// What the matcher tells the outside world. `seq` is assigned by the book and rises by one per event.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    Trade { seq: u64, taker: OrderId, maker: OrderId, taker_side: Side, price: Price, qty: Qty },
    Rested { seq: u64, id: OrderId, side: Side, price: Price, qty: Qty },
    Cancelled { seq: u64, id: OrderId, qty: Qty, reason: CancelReason },
    Replaced { seq: u64, id: OrderId, price: Price, qty: Qty },
    Rejected { seq: u64, id: OrderId, reason: RejectReason },
}

impl Event {
    pub fn seq(&self) -> u64 {
        match *self {
            Event::Trade { seq, .. }
            | Event::Rested { seq, .. }
            | Event::Cancelled { seq, .. }
            | Event::Replaced { seq, .. }
            | Event::Rejected { seq, .. } => seq,
        }
    }
}

/// Where the book sends events. Generic rather than `dyn` so the benchmark sink inlines to nothing.
pub trait EventSink {
    fn on(&mut self, e: Event);
}

impl EventSink for Vec<Event> {
    fn on(&mut self, e: Event) {
        self.push(e);
    }
}

/// Counts events and folds them into a checksum. Used where the events themselves are not needed.
#[derive(Default)]
pub struct CountingSink {
    pub events: u64,
    pub trades: u64,
    pub hash: u64,
}

impl EventSink for CountingSink {
    #[inline(always)]
    fn on(&mut self, e: Event) {
        self.events += 1;
        if let Event::Trade { qty, price, .. } = e {
            self.trades += 1;
            self.hash = self.hash.rotate_left(7) ^ qty ^ (price as u64);
        }
    }
}

/// Which order gives way when an owner's buy meets the same owner's sell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Stp {
    Off = 0,
    CancelIncoming = 1,
    CancelResting = 2,
}
