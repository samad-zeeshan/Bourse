"""
Tests for the Binance loaders and the depth-to-command builder.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from binance import market_orders, ticks, lots  # noqa: E402
from reconstruct import Builder, level_id  # noqa: E402


def test_units_round_to_the_symbol_steps():
    assert ticks("2688.22000000") == 268822
    assert lots("0.00690000") == 69


def test_fills_sharing_a_time_and_side_are_one_market_order():
    t = pd.DataFrame({"t": [0.0, 0.0, 0.5, 0.5], "ts_us": [10, 10, 20, 20], "price": [1.0, 1.01, 2.0, 2.0],
                      "qty": [1.0, 2.0, 1.0, 1.0], "side": [1, 1, 1, -1]})
    m = market_orders(t)
    assert list(m["qty"]) == [3.0, 1.0, 1.0]
    assert list(m["levels"]) == [2, 1, 1]


def test_a_batch_that_moves_the_touch_does_not_cross_on_the_way():
    b = Builder()
    b.apply([["100.00", "1"]], [["100.01", "1"]])
    # The ask at 100.01 leaves and a bid arrives there in the same batch. Adds applied first would cross.
    b.apply([["100.01", "2"]], [["100.01", "0"], ["100.02", "1"]])
    assert b.crossing_adds == 0
    assert b.cmds[2] == f"C,{level_id('S', 10001)}"
    assert b.book["B"][10001] == 20000
