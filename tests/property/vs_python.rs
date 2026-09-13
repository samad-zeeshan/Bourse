//! Property tests: random command streams must give the same events from the engine and the Python reference.
//!
//! Every case also runs the invariant shadow and the book's own structure walk after each command.

use std::path::Path;
use std::sync::{Mutex, OnceLock};

use bourse::check::Shadow;
use bourse::codec::event_to_text;
use bourse::pyref::PyRef;
use bourse::*;
use proptest::prelude::*;

fn reference() -> &'static Mutex<PyRef> {
    static R: OnceLock<Mutex<PyRef>> = OnceLock::new();
    R.get_or_init(|| {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
        Mutex::new(PyRef::spawn(&root).expect("start the Python reference book"))
    })
}

fn side() -> impl Strategy<Value = Side> {
    prop_oneof![Just(Side::Buy), Just(Side::Sell)]
}

// A small id space and price band on purpose. Collisions produce duplicates, unknown cancels and
// deep queues at one price, which is where priority bugs live.
fn command(min_price: i64, levels: i64) -> impl Strategy<Value = Command> {
    let id = prop_oneof![20 => 1u64..24, 1 => Just(u64::MAX)];
    let price = (min_price - 2)..(min_price + levels + 2);
    let qty = prop_oneof![1 => Just(0u64), 12 => 1u64..12];
    let owner = 0u32..4;
    prop_oneof![
        8 => (id.clone(), owner.clone(), side(), price.clone(), qty.clone(), prop_oneof![4 => Just(Tif::Gtc), 1 => Just(Tif::Ioc)])
            .prop_map(|(id, owner, side, price, qty, tif)| Command::Limit { id, owner, side, price, qty, tif }),
        2 => (id.clone(), owner, side(), qty.clone()).prop_map(|(id, owner, side, qty)| Command::Market { id, owner, side, qty }),
        3 => id.clone().prop_map(|id| Command::Cancel { id }),
        2 => (id, price, qty).prop_map(|(id, price, qty)| Command::Replace { id, price, qty }),
    ]
}

fn case() -> impl Strategy<Value = (Config, Vec<Command>)> {
    (90i64..110, 4usize..24, 1usize..16, prop_oneof![Just(Stp::Off), Just(Stp::CancelIncoming), Just(Stp::CancelResting)])
        .prop_flat_map(|(min_price, levels, max_orders, stp)| {
            let cfg = Config { min_price, levels, max_orders, stp };
            (Just(cfg), prop::collection::vec(command(min_price, levels as i64), 1..160))
        })
}

fn run_engine(cfg: Config, cmds: &[Command]) -> Result<Vec<Vec<String>>, TestCaseError> {
    let mut book = Book::new(cfg);
    let mut shadow = Shadow::new(cfg);
    let mut out = Vec::new();
    for c in cmds {
        let mut ev = Vec::new();
        book.process(c, &mut ev);
        if let Err(v) = shadow.step(c, &ev) {
            return Err(TestCaseError::fail(format!("{}: {} at {c:?}", v.invariant, v.detail)));
        }
        book.check_structure().map_err(|e| TestCaseError::fail(format!("{e} after {c:?}")))?;
        out.push(ev.iter().map(event_to_text).collect());
    }
    Ok(out)
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 3000, ..ProptestConfig::default() })]

    #[test]
    fn engine_matches_python_reference((cfg, cmds) in case()) {
        let ours = run_engine(cfg, &cmds)?;
        let theirs = reference().lock().unwrap().run(&cfg, &cmds).expect("reference reply");
        prop_assert_eq!(ours.len(), theirs.len());
        for (i, (a, b)) in ours.iter().zip(theirs.iter()).enumerate() {
            prop_assert_eq!(a, b, "command {} {:?}", i, cmds[i]);
        }
    }
}

#[test]
fn a_size_cut_keeps_queue_position_but_a_size_increase_loses_it() {
    let mut b = Book::new(Config { min_price: 0, levels: 100, max_orders: 16, stp: Stp::Off });
    let mut ev = Vec::new();
    for id in 1..=3 {
        b.process(&Command::Limit { id, owner: 0, side: Side::Sell, price: 50, qty: 10, tif: Tif::Gtc }, &mut ev);
    }
    b.process(&Command::Replace { id: 1, price: 50, qty: 4 }, &mut ev);
    assert_eq!(b.queue(Side::Sell, 50), vec![(1, 4), (2, 10), (3, 10)]);
    b.process(&Command::Replace { id: 2, price: 50, qty: 11 }, &mut ev);
    assert_eq!(b.queue(Side::Sell, 50), vec![(1, 4), (3, 10), (2, 11)]);
}

#[test]
fn self_trade_prevention_cancels_the_chosen_side() {
    for (stp, expect) in [(Stp::CancelIncoming, "X 2 2 5 stp"), (Stp::CancelResting, "X 2 1 5 stp")] {
        let mut b = Book::new(Config { min_price: 0, levels: 100, max_orders: 16, stp });
        let mut ev = Vec::new();
        b.process(&Command::Limit { id: 1, owner: 7, side: Side::Sell, price: 50, qty: 5, tif: Tif::Gtc }, &mut ev);
        b.process(&Command::Limit { id: 2, owner: 7, side: Side::Buy, price: 50, qty: 5, tif: Tif::Gtc }, &mut ev);
        let text: Vec<String> = ev.iter().map(event_to_text).collect();
        assert_eq!(text[1], expect);
        b.check_structure().unwrap();
    }
}
