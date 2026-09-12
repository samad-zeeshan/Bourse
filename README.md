# Bourse

Bourse is a small stock exchange and a trading robot, built to be checked rather than trusted. The exchange part matches buyers with sellers in strict order of price and arrival time. The robot quotes prices on both sides and tries to earn the gap between them.

## Invariants

The matching engine promises five things after every order it handles.

1. The book is never crossed. The highest price a buyer is waiting at is always below the lowest price a seller is waiting at.
2. Quantity is conserved. For every order, what traded plus what was cancelled plus what is still waiting adds up to what was sent.
3. Price comes first, then arrival time. A trade always happens at the best waiting price, and at that price the order that has waited longest trades first.
4. A cancel removes exactly the quantity that was waiting, no more and no less.
5. Every event the engine sends out carries a number one higher than the event before it, so a gap or a repeat is visible.

## Licence

MIT, see [LICENSE](LICENSE).
