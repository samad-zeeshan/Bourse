------------------------------ MODULE Bourse ------------------------------
(* The order lifecycle and the ring handoff between the reader thread and the matcher.

   The reader pushes commands into a bounded ring in two steps, write the slot then publish the tail,
   exactly as spsc.rs does with its release store. The matcher pops and applies each command in one
   step, because it is the only thread that touches the book. Two flags switch in known bugs so the
   checker has to find them: PublishFirst publishes the tail before the slot is written, and LifoBug
   puts a new order at the front of its price level instead of the back.

   The invariants are the ones in the README:
     NoCrossed        the best bid is always below the best ask
     Conserved        for every processed order, filled + cancelled + resting = sent quantity.
                      A cancel that removed the wrong amount breaks this too, so it also covers
                      "cancel removes exactly the resting quantity".
     FifoWithinLevel  inside a price level, orders sit in arrival order, and trades take the head
     SeqIncreasing    event i carries sequence number i
     HandoffExact     the matcher has consumed exactly a prefix of what the reader sent, in order,
                      with nothing lost, repeated or read before it was written

   What this does not cover: market orders, IOC, replace and self-trade prevention, which the
   property tests and the fuzzer check against the Python reference instead. Quantities, ids and
   prices are tiny sets, and the model says nothing about the Rust code except through the reading
   of it that produced this file. *)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Ids, Prices, MaxQty, Cap, MaxSent, PublishFirst, LifoBug

Sides == {"B", "S"}
Opp(s) == IF s = "B" THEN "S" ELSE "B"
Min(S) == CHOOSE x \in S : \A y \in S : x <= y
Max(S) == CHOOSE x \in S : \A y \in S : x >= y
Blank == [kind |-> "none", id |-> 0, side |-> "B", price |-> 0, qty |-> 0]

VARIABLES
    sent,      \* every command the reader has started to push, in order (ghost)
    ring,      \* slot index -> command
    ptail,     \* the reader's private count of slots written
    tail,      \* the count the reader has published to the matcher
    head,      \* the count the matcher has consumed
    stage,     \* "idle", or "mid" between the two steps of a push
    taken,     \* every command the matcher has consumed, in order (ghost)
    book,      \* side -> price -> sequence of <<id, qty, arrival>>
    out,       \* the event stream
    qtyOf, filled, cancelled, done  \* ghost accounting per order

vars == <<sent, ring, ptail, tail, head, stage, taken, book, out, qtyOf, filled, cancelled, done>>

Init ==
    /\ sent = <<>>
    /\ ring = [i \in 0..(Cap - 1) |-> Blank]
    /\ ptail = 0 /\ tail = 0 /\ head = 0
    /\ stage = "idle"
    /\ taken = <<>>
    /\ book = [s \in Sides |-> [p \in Prices |-> <<>>]]
    /\ out = <<>>
    /\ qtyOf = [i \in Ids |-> 0]
    /\ filled = [i \in Ids |-> 0]
    /\ cancelled = [i \in Ids |-> 0]
    /\ done = {}

Used == {sent[i].id : i \in 1..Len(sent)}

NewCommands ==
    {[kind |-> "limit", id |-> i, side |-> s, price |-> p, qty |-> q] :
        i \in Ids \ Used, s \in Sides, p \in Prices, q \in 1..MaxQty}
    \cup {[kind |-> "cancel", id |-> i, side |-> "B", price |-> 0, qty |-> 0] : i \in Used}

(* The first step of a push. With the right order it writes the slot and leaves the tail alone.
   With PublishFirst it bumps the published tail before anything is in the slot. *)
PushStart ==
    /\ stage = "idle"
    /\ Len(sent) < MaxSent
    /\ ptail - head < Cap
    /\ \E c \in NewCommands :
        /\ sent' = Append(sent, c)
        /\ IF PublishFirst
              THEN /\ tail' = ptail + 1 /\ ring' = ring
              ELSE /\ ring' = [ring EXCEPT ![ptail % Cap] = c] /\ tail' = tail
    /\ stage' = "mid"
    /\ UNCHANGED <<ptail, head, taken, book, out, qtyOf, filled, cancelled, done>>

PushFinish ==
    /\ stage = "mid"
    /\ IF PublishFirst
          THEN /\ ring' = [ring EXCEPT ![ptail % Cap] = sent[Len(sent)]] /\ tail' = tail
          ELSE /\ tail' = ptail + 1 /\ ring' = ring
    /\ ptail' = ptail + 1
    /\ stage' = "idle"
    /\ UNCHANGED <<sent, head, taken, book, out, qtyOf, filled, cancelled, done>>

Crosses(side, px, limit) == IF side = "B" THEN px <= limit ELSE px >= limit

Trade(taker, maker, px, q) == [k |-> "trade", seq |-> 0, id |-> taker, maker |-> maker, price |-> px, qty |-> q]
Ev(k, i, q) == [k |-> k, seq |-> 0, id |-> i, maker |-> 0, price |-> 0, qty |-> q]

(* Walk the opposite side from the best price, always taking the head of the level. *)
RECURSIVE Match(_, _, _, _, _, _, _)
Match(bk, fl, taker, side, limit, rem, evs) ==
    LET opp == Opp(side)
        live == {x \in Prices : bk[opp][x] # <<>> /\ Crosses(side, x, limit)}
    IN IF rem = 0 \/ live = {}
       THEN [bk |-> bk, fl |-> fl, rem |-> rem, evs |-> evs]
       ELSE LET bp == IF side = "B" THEN Min(live) ELSE Max(live)
                m == Head(bk[opp][bp])
                f == IF rem < m[2] THEN rem ELSE m[2]
                rest == IF f = m[2] THEN Tail(bk[opp][bp])
                        ELSE <<<<m[1], m[2] - f, m[3]>>>> \o Tail(bk[opp][bp])
            IN Match([bk EXCEPT ![opp][bp] = rest],
                     [fl EXCEPT ![taker] = @ + f, ![m[1]] = @ + f],
                     taker, side, limit, rem - f, Append(evs, Trade(taker, m[1], bp, f)))

Resting(i) == UNION {{<<s, p, k>> : k \in {j \in 1..Len(book[s][p]) : book[s][p][j][1] = i}} : s \in Sides, p \in Prices}

Numbered(evs) == [k \in 1..Len(evs) |-> [evs[k] EXCEPT !.seq = Len(out) + k]]

Consume ==
    /\ head < tail
    /\ LET c == ring[head % Cap]
           t == Len(taken) + 1
       IN /\ taken' = Append(taken, c)
          /\ head' = head + 1
          /\ CASE c.kind = "limit" ->
                    LET r == Match(book, filled, c.id, c.side, c.price, c.qty, <<>>)
                        entry == <<c.id, r.rem, t>>
                        lvl == r.bk[c.side][c.price]
                        placed == IF r.rem = 0 THEN r.bk
                                  ELSE [r.bk EXCEPT ![c.side][c.price] =
                                          IF LifoBug THEN <<entry>> \o lvl ELSE Append(lvl, entry)]
                        evs == IF r.rem = 0 THEN r.evs ELSE Append(r.evs, Ev("rested", c.id, r.rem))
                    IN /\ book' = placed
                       /\ filled' = r.fl
                       /\ out' = out \o Numbered(evs)
                       /\ qtyOf' = [qtyOf EXCEPT ![c.id] = c.qty]
                       /\ done' = done \cup {c.id}
                       /\ UNCHANGED cancelled
              [] c.kind = "cancel" ->
                    IF Resting(c.id) = {}
                    THEN /\ out' = out \o Numbered(<<Ev("rejected", c.id, 0)>>)
                         /\ UNCHANGED <<book, filled, cancelled, qtyOf, done>>
                    ELSE LET loc == CHOOSE x \in Resting(c.id) : TRUE
                             s == loc[1]
                             p == loc[2]
                             k == loc[3]
                             q == book[s][p][k][2]
                         IN /\ book' = [book EXCEPT ![s][p] =
                                          SubSeq(@, 1, k - 1) \o SubSeq(@, k + 1, Len(@))]
                            /\ cancelled' = [cancelled EXCEPT ![c.id] = @ + q]
                            /\ out' = out \o Numbered(<<Ev("cancelled", c.id, q)>>)
                            /\ UNCHANGED <<filled, qtyOf, done>>
              [] OTHER ->
                    \* A slot read before it was written. The matcher sees nothing to apply.
                    /\ out' = out \o Numbered(<<Ev("rejected", 0, 0)>>)
                    /\ UNCHANGED <<book, filled, cancelled, qtyOf, done>>
    /\ UNCHANGED <<sent, ring, ptail, tail, stage>>

(* The reader stops after MaxSent commands. Once everything is consumed the system idles here,
   so TLC does not report the end of the bounded run as a deadlock. *)
Finished == Len(sent) = MaxSent /\ stage = "idle" /\ head = tail /\ UNCHANGED vars

Next == PushStart \/ PushFinish \/ Consume \/ Finished

Spec == Init /\ [][Next]_vars /\ WF_vars(PushFinish) /\ WF_vars(Consume)

-----------------------------------------------------------------------------

BestBid == LET l == {p \in Prices : book["B"][p] # <<>>} IN IF l = {} THEN 0 ELSE Max(l)
BestAsk == LET l == {p \in Prices : book["S"][p] # <<>>} IN IF l = {} THEN 1000 ELSE Min(l)

NoCrossed == BestBid < BestAsk

RestingQty(i) == LET r == Resting(i) IN IF r = {} THEN 0
                 ELSE LET x == CHOOSE y \in r : TRUE IN book[x[1]][x[2]][x[3]][2]

Conserved == \A i \in done : filled[i] + cancelled[i] + RestingQty(i) = qtyOf[i]

FifoWithinLevel == \A s \in Sides, p \in Prices :
    \A a, b \in 1..Len(book[s][p]) : a < b => book[s][p][a][3] < book[s][p][b][3]

SeqIncreasing == \A i \in 1..Len(out) : out[i].seq = i

(* Liveness: whatever the reader has started to push is eventually consumed by the matcher. *)
Drains == [](Len(sent) > Len(taken) => <>(Len(taken) = Len(sent)))

HandoffExact == /\ Len(taken) <= Len(sent)
                /\ \A i \in 1..Len(taken) : taken[i] = sent[i]

=============================================================================
