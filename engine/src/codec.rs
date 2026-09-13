//! Fixed-width binary and one-line text encodings for commands and events.
//!
//! The binary forms are what the determinism check hashes. The text forms are what the Python reference speaks.

use crate::types::*;

pub const CMD_BYTES: usize = 32;
pub const EVENT_BYTES: usize = 48;

fn side_char(s: Side) -> char {
    match s {
        Side::Buy => 'B',
        Side::Sell => 'S',
    }
}

fn parse_side(s: &str) -> Result<Side, String> {
    match s {
        "B" => Ok(Side::Buy),
        "S" => Ok(Side::Sell),
        _ => Err(format!("bad side {s:?}")),
    }
}

pub fn encode_command(c: &Command, out: &mut [u8; CMD_BYTES]) {
    *out = [0; CMD_BYTES];
    let (kind, side, tif, owner, id, price, qty) = match *c {
        Command::Limit { id, owner, side, price, qty, tif } => (0u8, side as u8, tif as u8, owner, id, price, qty),
        Command::Market { id, owner, side, qty } => (1, side as u8, 0, owner, id, 0, qty),
        Command::Cancel { id } => (2, 0, 0, 0, id, 0, 0),
        Command::Replace { id, price, qty } => (3, 0, 0, 0, id, price, qty),
    };
    out[0] = kind;
    out[1] = side;
    out[2] = tif;
    out[4..8].copy_from_slice(&owner.to_le_bytes());
    out[8..16].copy_from_slice(&id.to_le_bytes());
    out[16..24].copy_from_slice(&price.to_le_bytes());
    out[24..32].copy_from_slice(&qty.to_le_bytes());
}

pub fn decode_command(b: &[u8]) -> Result<Command, String> {
    let u64_at = |i: usize| u64::from_le_bytes(b[i..i + 8].try_into().unwrap());
    let owner = u32::from_le_bytes(b[4..8].try_into().unwrap());
    let (id, price, qty) = (u64_at(8), u64_at(16) as i64, u64_at(24));
    let side = || Side::from_u8(b[1]).ok_or_else(|| format!("bad side byte {}", b[1]));
    Ok(match b[0] {
        0 => Command::Limit { id, owner, side: side()?, price, qty, tif: if b[2] == 1 { Tif::Ioc } else { Tif::Gtc } },
        1 => Command::Market { id, owner, side: side()?, qty },
        2 => Command::Cancel { id },
        3 => Command::Replace { id, price, qty },
        k => return Err(format!("bad command kind {k}")),
    })
}

pub fn command_to_text(c: &Command) -> String {
    match *c {
        Command::Limit { id, owner, side, price, qty, tif } => {
            format!("L,{id},{owner},{},{price},{qty},{}", side_char(side), if tif == Tif::Ioc { "IOC" } else { "GTC" })
        }
        Command::Market { id, owner, side, qty } => format!("M,{id},{owner},{},{qty}", side_char(side)),
        Command::Cancel { id } => format!("C,{id}"),
        Command::Replace { id, price, qty } => format!("R,{id},{price},{qty}"),
    }
}

pub fn command_from_text(line: &str) -> Result<Command, String> {
    let f: Vec<&str> = line.trim().split(',').collect();
    let num = |i: usize| f.get(i).ok_or_else(|| format!("short line {line:?}"))?.parse::<i128>().map_err(|e| format!("{e} in {line:?}"));
    let u = |i: usize| num(i).map(|v| v as u64);
    Ok(match f[0] {
        "L" if f.len() == 7 => Command::Limit {
            id: u(1)?,
            owner: u(2)? as u32,
            side: parse_side(f[3])?,
            price: num(4)? as i64,
            qty: u(5)?,
            tif: match f[6] {
                "IOC" => Tif::Ioc,
                "GTC" => Tif::Gtc,
                t => return Err(format!("bad tif {t:?}")),
            },
        },
        "M" if f.len() == 5 => Command::Market { id: u(1)?, owner: u(2)? as u32, side: parse_side(f[3])?, qty: u(4)? },
        "C" if f.len() == 2 => Command::Cancel { id: u(1)? },
        "R" if f.len() == 4 => Command::Replace { id: u(1)?, price: num(2)? as i64, qty: u(3)? },
        _ => return Err(format!("bad command line {line:?}")),
    })
}

pub fn encode_event(e: &Event, out: &mut [u8; EVENT_BYTES]) {
    *out = [0; EVENT_BYTES];
    let (kind, side, reason, seq, id, other, price, qty) = match *e {
        Event::Trade { seq, taker, maker, taker_side, price, qty } => (0u8, taker_side as u8, 0u8, seq, taker, maker, price, qty),
        Event::Rested { seq, id, side, price, qty } => (1, side as u8, 0, seq, id, 0, price, qty),
        Event::Cancelled { seq, id, qty, reason } => (2, 0, reason as u8, seq, id, 0, 0, qty),
        Event::Replaced { seq, id, price, qty } => (3, 0, 0, seq, id, 0, price, qty),
        Event::Rejected { seq, id, reason } => (4, 0, reason as u8, seq, id, 0, 0, 0),
    };
    out[0] = kind;
    out[1] = side;
    out[2] = reason;
    out[8..16].copy_from_slice(&seq.to_le_bytes());
    out[16..24].copy_from_slice(&id.to_le_bytes());
    out[24..32].copy_from_slice(&other.to_le_bytes());
    out[32..40].copy_from_slice(&price.to_le_bytes());
    out[40..48].copy_from_slice(&qty.to_le_bytes());
}

pub fn cancel_reason_name(r: CancelReason) -> &'static str {
    match r {
        CancelReason::User => "user",
        CancelReason::IocRemainder => "ioc",
        CancelReason::NoLiquidity => "noliq",
        CancelReason::SelfTrade => "stp",
        CancelReason::BookFull => "full",
    }
}

pub fn reject_reason_name(r: RejectReason) -> &'static str {
    match r {
        RejectReason::DuplicateId => "dup",
        RejectReason::UnknownId => "unknown",
        RejectReason::ZeroQty => "zero",
        RejectReason::PriceOutOfRange => "range",
        RejectReason::InvalidId => "invalid",
    }
}

pub fn event_to_text(e: &Event) -> String {
    match *e {
        Event::Trade { seq, taker, maker, taker_side, price, qty } => {
            format!("T {seq} {taker} {maker} {} {price} {qty}", side_char(taker_side))
        }
        Event::Rested { seq, id, side, price, qty } => format!("A {seq} {id} {} {price} {qty}", side_char(side)),
        Event::Cancelled { seq, id, qty, reason } => format!("X {seq} {id} {qty} {}", cancel_reason_name(reason)),
        Event::Replaced { seq, id, price, qty } => format!("R {seq} {id} {price} {qty}"),
        Event::Rejected { seq, id, reason } => format!("J {seq} {id} {}", reject_reason_name(reason)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_round_trip_through_both_encodings() {
        let cmds = [
            Command::Limit { id: 7, owner: 3, side: Side::Sell, price: -5, qty: 10, tif: Tif::Ioc },
            Command::Market { id: 8, owner: 0, side: Side::Buy, qty: 1 },
            Command::Cancel { id: u64::MAX - 1 },
            Command::Replace { id: 9, price: 100, qty: 2 },
        ];
        for c in cmds {
            let mut b = [0u8; CMD_BYTES];
            encode_command(&c, &mut b);
            assert_eq!(decode_command(&b).unwrap(), c);
            assert_eq!(command_from_text(&command_to_text(&c)).unwrap(), c);
        }
    }
}
