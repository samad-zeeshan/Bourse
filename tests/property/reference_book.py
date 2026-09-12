"""
A deliberately slow and plain order book that the Rust engine is tested against.

It sorts the whole book on every match, so it is easy to read and hard to get wrong. `--serve` reads one JSON case per line.
"""

import json
import sys

U64_MAX = 2**64 - 1


class Book:
    def __init__(self, min_price, levels, max_orders, stp):
        self.min_price, self.levels, self.max_orders, self.stp = min_price, levels, max_orders, stp
        self.orders = {}  # id -> dict(side, price, qty, owner, t)
        self.clock = 0
        self.seq = 0
        self.out = []

    def emit(self, *fields):
        self.seq += 1
        kind, rest = fields[0], fields[1:]
        self.out.append(" ".join([kind, str(self.seq)] + [str(f) for f in rest]))

    def in_range(self, p):
        return self.min_price <= p < self.min_price + self.levels

    def rest(self, oid, owner, side, price, qty):
        if len(self.orders) >= self.max_orders:
            self.emit("X", oid, qty, "full")
            return
        self.clock += 1
        self.orders[oid] = dict(side=side, price=price, qty=qty, owner=owner, t=self.clock)
        self.emit("A", oid, side, price, qty)

    def queue(self, side):
        # Best price first, then oldest first. Buyers want the highest price, sellers the lowest.
        mine = [(oid, o) for oid, o in self.orders.items() if o["side"] == side]
        sign = -1 if side == "B" else 1
        return sorted(mine, key=lambda x: (sign * x[1]["price"], x[1]["t"]))

    def take(self, oid, owner, side, limit, qty):
        opp = "S" if side == "B" else "B"
        while qty > 0:
            q = self.queue(opp)
            if not q:
                break
            mid, m = q[0]
            if limit is not None and ((side == "B" and m["price"] > limit) or (side == "S" and m["price"] < limit)):
                break
            if self.stp != "off" and owner != 0 and m["owner"] == owner:
                if self.stp == "incoming":
                    self.emit("X", oid, qty, "stp")
                    return 0
                del self.orders[mid]
                self.emit("X", mid, m["qty"], "stp")
                continue
            fill = min(qty, m["qty"])
            qty -= fill
            m["qty"] -= fill
            self.emit("T", oid, mid, side, m["price"], fill)
            if m["qty"] == 0:
                del self.orders[mid]
        return qty

    def command(self, line):
        f = line.split(",")
        kind = f[0]
        if kind in ("L", "M"):
            oid, owner, side = int(f[1]), int(f[2]), f[3]
            if kind == "L":
                price, qty, tif = int(f[4]), int(f[5]), f[6]
            else:
                price, qty, tif = None, int(f[4]), None
            if oid == U64_MAX:
                return self.emit("J", oid, "invalid")
            if qty == 0:
                return self.emit("J", oid, "zero")
            if price is not None and not self.in_range(price):
                return self.emit("J", oid, "range")
            if oid in self.orders:
                return self.emit("J", oid, "dup")
            left = self.take(oid, owner, side, price, qty)
            if left > 0:
                if kind == "M":
                    self.emit("X", oid, left, "noliq")
                elif tif == "IOC":
                    self.emit("X", oid, left, "ioc")
                else:
                    self.rest(oid, owner, side, price, left)
        elif kind == "C":
            oid = int(f[1])
            if oid not in self.orders:
                return self.emit("J", oid, "unknown")
            o = self.orders.pop(oid)
            self.emit("X", oid, o["qty"], "user")
        elif kind == "R":
            oid, price, qty = int(f[1]), int(f[2]), int(f[3])
            if oid not in self.orders:
                return self.emit("J", oid, "unknown")
            if qty == 0:
                return self.emit("J", oid, "zero")
            if not self.in_range(price):
                return self.emit("J", oid, "range")
            o = self.orders[oid]
            if price == o["price"] and qty <= o["qty"]:
                o["qty"] = qty
                return self.emit("R", oid, price, qty)
            del self.orders[oid]
            self.emit("R", oid, price, qty)
            left = self.take(oid, o["owner"], o["side"], price, qty)
            if left > 0:
                self.rest(oid, o["owner"], o["side"], price, left)
        else:
            raise ValueError(f"bad command {line!r}")

    def run(self, line):
        self.out = []
        self.command(line)
        return self.out


def run_case(case):
    c = case["cfg"]
    book = Book(c["min_price"], c["levels"], c["max_orders"], c["stp"])
    return [book.run(line) for line in case["cmds"]]


def main():
    if "--serve" in sys.argv:
        for line in sys.stdin:
            if line.strip():
                sys.stdout.write(json.dumps(run_case(json.loads(line))) + "\n")
                sys.stdout.flush()
        return
    case = json.load(sys.stdin)
    json.dump(run_case(case), sys.stdout)


if __name__ == "__main__":
    main()
