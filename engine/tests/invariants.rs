//! Runs a long synthetic flow through the book with the invariant shadow watching every command.

use bourse::check::Shadow;
use bourse::gen::FlowGen;
use bourse::*;

#[test]
fn three_hundred_thousand_generated_orders_break_no_invariant() {
    for (seed, stp) in [(1u64, Stp::Off), (2, Stp::CancelIncoming), (3, Stp::CancelResting)] {
        let cfg = Config { min_price: 0, levels: 4096, max_orders: 3000, stp };
        let mut book = Book::new(cfg);
        let mut shadow = Shadow::new(cfg);
        let mut g = FlowGen::new(seed, 0, 4096);
        let mut ev = Vec::new();
        for i in 0..100_000 {
            let c = g.next_command();
            ev.clear();
            book.process(&c, &mut ev);
            if let Err(v) = shadow.step(&c, &ev) {
                panic!("{}: {} at command {i} {c:?}", v.invariant, v.detail);
            }
            if i % 10_000 == 0 {
                book.check_structure().unwrap();
            }
        }
        assert_eq!(book.live_orders(), shadow.resting().len());
    }
}
