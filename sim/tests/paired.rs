//! The paired design only works if two runs with the same seed see identical background flow.

use bourse::Side;
use sim::*;

fn params() -> Params {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let mut p = Params::load(root.join("flow/params.json").to_str().unwrap(), "2026-09-21");
    p.liquidity = 4.0;
    p.cancel_scale = 0.75;
    p
}

#[test]
fn same_seed_same_path_and_a_buy_metaorder_pushes_the_price_up() {
    let p = params();
    let times = [100.0, 400.0];
    let a = run(&p, 5, 400.0, None, false, &times, (100.0, 400.0));
    let b = run(&p, 5, 400.0, None, false, &times, (100.0, 400.0));
    assert_eq!(a.mid2_at, b.mid2_at);
    assert_eq!(a.commands, b.commands);
    let mut up = 0.0;
    for seed in 0..20 {
        let base = run(&p, seed, 400.0, None, false, &times, (100.0, 300.0));
        let meta = Meta { side: Side::Buy, total: 2_000_000, children: 30, start: 100.0, duration: 200.0 };
        let with = run(&p, seed, 400.0, Some(&meta), false, &times, (100.0, 300.0));
        assert_eq!(base.mid2_at[0], with.mid2_at[0], "paths must agree before the metaorder starts");
        up += (with.mid2_at[1] - base.mid2_at[1]) as f64;
    }
    assert!(up > 0.0);
}
