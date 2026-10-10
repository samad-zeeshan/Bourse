# Bourse

Bourse is a small stock exchange and a robot that trades on it. Tests check both, so nobody has to trust them. The exchange matches buyers and sellers first by price and then by arrival time. It never loses an order. It gives the same answer every time it gets the same orders. The robot tries to earn the gap between the buy price and the sell price.

Strict tests judge the robot. They treat every profit as luck or a cheat until the robot proves otherwise. None of the honest robots made money.

**Demo:** [samad-zeeshan.github.io/Bourse](https://samad-zeeshan.github.io/Bourse/) replays three minutes of the Binance order book through the exchange in your browser. Then it shows the speed of the exchange, a test of price impact, and a cheat that the checks catch. A 90-second video is in [docs/media/demo.mp4](docs/media/demo.mp4).

![The Bourse demo replays the order book, times each order, and catches a robot that cheats](docs/media/demo.gif)

## How it works

- ![Threads and queues of the exchange](docs/diagrams/engine.png) One thread owns the order book. Orders go in and come out through lock-free queues. So the output depends only on the input.
- ![Life of an order](docs/diagrams/lifecycle.png) The exchange checks an order and matches it against the best open prices. Then the order rests or fills, or the trader cancels or replaces it.
- ![How the tests judge a robot](docs/diagrams/evaluation.png) The test tunes each robot on earlier days and runs it once on the next day. Then a leakage check and a referee judge it. The referee knows how many settings the project tried.

Each diagram also has an interactive version in `docs/diagrams/`.

## Speed

<!-- gen:speed -->
| Exchange | Machine | Median | 99th percentile | 99.9th percentile | Slowest | Orders per second |
|---|---|---|---|---|---|---|
| Bourse, matcher alone | AMD Ryzen 5 7600X3D, Windows PC | 40 ns | 180 ns | 280 ns | 54.4 µs | 42 million |
| Bourse, three threads with queues | same | | | | | 25 million |
| exchange-core at 1 million a second | Xeon X5690, tuned server | 0.5 µs | 4 µs | 22 µs | 45 µs | published |
| exchange-core at 5 million a second | Xeon X5690, tuned server | 1.5 µs | 42 µs | 150 µs | 190 µs | published |
<!-- /gen -->

<!-- gen:speed-summary -->
Half of all orders took 40 nanoseconds or less. Out of every 100 orders, 99 took 180 nanoseconds or less. The goal was one microsecond, so both times are well inside it. One thread handled about 42 million orders a second. The goal was one million, so this is well above it.
<!-- /gen -->
The benchmark timed each order alone with the cycle counter of the processor. The test used no network and no disk. The numbers for the other exchange include risk checks and queues at a fixed rate on a tuned server. So they are a rough guide, not a fair race.

<!-- gen:method -->
| Setting | Value |
|---|---|
| Processor | AMD Ryzen 5 7600X3D 6-Core Processor, 12 logical cores |
| System | Microsoft Windows [Version 10.0.26200.9457], power plan High performance |
| Compiler | rustc 1.98.1 (48a229cea 2026-09-01), release profile, fat LTO, one codegen unit |
| Pinning | matcher on logical core 4, time-critical priority, no core isolation |
| Clock | processor time stamp counter, 4.10 ticks per nanosecond |
| Orders | 2,000,000 to warm up, then 20,000,000 timed one by one |
| Timer cost | reported separately and not subtracted |
<!-- /gen -->

## Correctness

After every order, the exchange keeps five promises:

- The best buy price stays below the best sell price.
- No quantity appears or disappears.
- The best price trades first. At one price, the oldest order trades before newer ones.
- A cancel removes exactly the open quantity of that order.
- Each event carries a number one higher than the event before.

<!-- gen:correctness -->
| Check | What it tried | Result |
|---|---|---|
| Property tests against the Python reference copy | 3,000 random order streams per run | all match |
| Fuzz run against the same reference copy | 5.5 hours, 1.5 billion orders | 0 disagreements or broken rules |
| TLA+ model of the order life and the hand-off | 541,993 distinct states | no rule broken |
| Same model, tail published before the slot is written | planted bug | caught (HandoffExact) |
| Same model, new orders put at the front | planted bug | caught (FifoWithinLevel) |
| Replay of 5,000,000 orders in 4 separate runs | Windows processes and a Linux container | identical bytes |
<!-- /gen -->

<!-- gen:fuzz-summary -->
The fuzz run sent about 1.5 billion random orders to the exchange and to the reference copy. It found 0 problems, which is good news. The run stopped after about 5.5 hours, so it did not reach the 24 hours it aimed for. The shorter run gives less proof than planned.
<!-- /gen -->
The TLA+ model in `spec/Bourse.tla` covers limit orders, cancels and the hand-off between threads. It does not cover market orders, replaces or self-trade prevention. Only the tests and the fuzz run check those. The author wrote the model from the code. So the code and the model can agree on the same mistake. The paper arXiv 2607.05076 describes this gap.

## Real data

<!-- gen:data-summary -->
The order book of the exchange matched the Binance snapshot in 238 of 238 checks. Each check compared the top 20 prices on each side. The checks covered 240 minutes of recorded trades.
<!-- /gen -->
The free LOBSTER samples now need an account. So the data comes from Binance instead. It is four hours of the public ETHUSDT depth stream, plus seven days of the public trade archive.

## Order flow and price impact

Market orders arrive in bursts. A Hawkes process models this, because each order makes the next one more likely for a short time. It fits the real orders much better than constant rates. The fit error below is the largest gap between the order times of the model and the real order times. A smaller error is better.

<!-- gen:flow -->
| Day | Market orders in two hours | Self-excitation | Fit error, buys | Fit error, sells | Fit error with constant rates | Error with the fit of the day before |
|---|---|---|---|---|---|---|
| 2026-09-18 | 80,621 | 0.87 | 0.068 | 0.035 | 0.56 | first day |
| 2026-09-19 | 17,507 | 0.53 | 0.038 | 0.034 | 0.41 | 0.18 |
| 2026-09-20 | 16,857 | 0.59 | 0.032 | 0.040 | 0.52 | 0.12 |
| 2026-09-21 | 67,558 | 0.67 | 0.027 | 0.045 | 0.48 | 0.25 |
| 2026-09-22 | 53,012 | 0.69 | 0.038 | 0.041 | 0.54 | 0.07 |
| 2026-09-23 | 51,276 | 0.73 | 0.037 | 0.038 | 0.61 | 0.06 |
| 2026-09-24 | 38,129 | 0.68 | 0.052 | 0.033 | 0.51 | 0.14 |
<!-- /gen -->

The fit gets worse when the next day uses the model of the day before. But it stays much better than constant rates.
<!-- gen:synthetic-summary -->
Synthetic days made from these fits match the real days on these measures:

- order rate
- bursts of orders
- spread
- book depth
- volume traded

But prices in them move about 11 times less than real prices. Nothing in the simulation reacts to news. This is a weak point of the simulator.
<!-- /gen -->

<!-- gen:impact -->
| How the order is sent | Exponent | 95% interval | Sizes used in the fit |
|---|---|---|---|
| split into 60 pieces | 0.96 | 0.82 to 1.26 | 6 of 8 |
| split, refills do not follow the price | could not be fitted | | 1 of 8 |
| one order | could not be fitted | | 0 of 8 |
<!-- /gen -->

<!-- gen:impact-summary -->
The test split a large order into 60 pieces. The price moved by the size of the order to the power 0.96, with a 95% interval from 0.82 to 1.3. The square-root law predicts 0.5. So this simulator does not reproduce the law, which is a weak point.
<!-- /gen -->
Here is a likely reason. In the simulation, new orders arrive at a steady rate and do not rush in after the price moves. The paper arXiv 2607.04280 finds that this kind of refill is one of two things the law needs. When the test held refills at the old price, the impact almost disappeared. A single large order took all of the visible book before its impact grew large enough to measure.

## Market making

<!-- gen:strategies -->
| Robot | Profit per hour, USDT | 95% interval | Hourly Sharpe | Robustness grade | Chance it is luck, after the search | Leakage check |
|---|---|---|---|---|---|---|
| Avellaneda-Stoikov | -3.23 | -4.81 to -1.93 | -0.58 | 20 of 100 | 1.00 | clean |
| Multi-level | -27.64 | -34.46 to -21.58 | -1.09 | 20 of 100 | 1.00 | clean |
| RL candidate | -5.88 | -8.92 to -3.03 | -0.42 | 20 of 100 | 1.00 | clean |
| Planted cheat | 8.36 | 6.06 to 11.00 | 0.90 | 60 of 100 | 0.00 | refused, reads the future |
<!-- /gen -->

<!-- gen:strategy-summary -->
No honest robot made money on the test days. The trained robot lost 5.9 USDT an hour on average. The textbook robot (Avellaneda-Stoikov) lost 3.2 USDT an hour.

The trained robot did worse than the textbook robot by 2.7 USDT an hour. The 95% interval for this gap is -4.9 to -0.33. The interval does not include zero, so the trained robot is worse. The planted cheat made 8.4 USDT an hour and passed the referee. But the leakage check refused it.
<!-- /gen -->
In this test, a quote fills only when a market order trades through its price. This is as if the quote were always last in the queue. So every fill is a sign that the price moves against the robot. This rule is harsh, but it is honest. The grade follows the method in arXiv 2608.23808 but not its calibration. So the grade only ranks these robots against each other.
<!-- gen:fee-summary -->
A fee of one hundredth of a percent on each fill makes every robot lose more. The cheat then loses too, about 4.0 USDT an hour.
<!-- /gen -->

## Research note

**Hypothesis.** A robot trained against a worst-case market earns more on new days than the Avellaneda-Stoikov formula.

**Method.** The test has four rounds. Each round tunes every robot on the days before a test day. It picks one setting for each robot on the last of those days. Then it runs that setting once on the test day, in 24 parts of one hour each.
<!-- gen:register-summary -->
The project logged every setting it tried before it saw any test result. There were 412 settings in all.
<!-- /gen -->
A betting referee judges only the results that come after that point. It divides by the number of settings tried.

**Result.** The hypothesis is false. The trained robot lost more than the textbook robot. The interval for the difference does not include zero. No honest robot made money. Only the planted cheat looked profitable.

**What would break it.** The fill rule may be too strict. If a quote sometimes filled at the front of its queue, the textbook robots could make money. The sample is narrow, with seven days of one symbol. The policy is small and linear, but the paper uses a neural network.

**What to test next.** The project can test three things next:

- queue positions recorded from a full order-by-order feed
- a policy with memory
- negative fees, so that the exchange pays a market maker to wait

## Design decisions

- The exchange is in Rust, and the core crate has no dependencies. Prices are whole ticks and sizes are whole lots. So the exchange never rounds a value.
- The book sets the size of each structure when it starts. These are a price ladder, a slab of order slots and a fixed id table. A test proves that two million orders allocate no memory.
- If a replace only cuts the size at the same price, the order keeps its place in the queue. Any other replace sends the order to the back.
- The trained robot uses reinforcement learning, made small enough to train on one PC in minutes. It has a linear policy and uses evolution strategies instead of gradient methods. Each round, an adversary picks the worst of eight fixed market regimes.
- The strength of the planted cheat came from a look at one test day. This is allowed only because the robot is a cheat.

## Run it

```
cargo test --release --workspace                  # engine, property tests against Python, determinism
cargo run --release -p bench                      # timing on your machine, writes bench/results.json
python eval/report.py --check                     # README and demo numbers match the result files
```

Run `pip install -r requirements.txt` before the Python steps. To rebuild the flow and robot results, run `flow/calibrate.py`, `flow/sqrt_law.py` and `eval/mm/harness.py`. To run TLC again, run `spec/check.py`.

## Papers

- arXiv 2609.22785, Robust Market Making with Hawkes Order Flow and Price Impact. It gives the flow model and the adversarial robot.
- arXiv 2608.13096, FlowLOB. The project cites it for synthetic order books and does not reproduce it.
- arXiv 2608.19227, M3. The project cites it as another way to make order flow and does not reproduce it.
- arXiv 2607.04280, Order Splitting and Liquidity Replenishment and the square-root law. It is the basis of the impact test.
- arXiv 2606.29556, Persona-Trained Monte Carlo. The project cites it for mixed traders and does not build them.
- arXiv 2606.21784, KineticSim. It gives a speed reference for market simulators.
- arXiv 2608.18195, Multi-Level Market Making with Reinforcement Learning. It gives the multi-level robot.
- arXiv 2606.25986, The Inference-Compute Frontier for order book prediction. It explains why the trained robot stays small and fast.
- arXiv 2608.27734, What survives honest evaluation? It gives the leakage-safe splits, the planted cheat and the correction for the search.
- arXiv 2608.23808, The MinervaScore. It gives the robustness grade.
- arXiv 2609.27051, Propose, Don't Judge. It gives the betting referee.
- arXiv 2607.05076, Can Code Specify a System Precisely Enough to Formally Verify It? It shows what the model covers and what it does not.

## Licence

The code uses the MIT licence. See [LICENSE](LICENSE). The project uses Binance market data under the Binance terms of use. It does not share the raw recordings.
