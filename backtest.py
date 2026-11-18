"""Local backtest for the btc-race-24h-20261118-1444 dev window.

An exact integer replica of the kit harness's market model
(harness sim/bars/engine.ts: BarsPool constant-product hops, the buy/sell
fill formulas, the decision checks and the end-of-window flatten), driven
through the kit's own Python SDK state (vendor/makefun_bars.BarsState), so
the strategy code graded here is the strategy code shipped in main.py.

Also ports the kit's six baseline bots (baselines/*/main.ts) to Python
on_batch callables with the same rules, and grades them on the same window
for comparison.

Run:  python3 backtest.py [--params k=v,k=v] [--verbose]
"""

import gzip
import json
import math
import os
import sys
from math import gcd

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.expanduser(
    "~/workspace/makefun/kit-btc-race-24h-20261118-1444/btc-race-24h-20261118-1444-kit")
sys.path.insert(0, os.path.join(HERE, "vendor"))
import makefun_bars  # noqa: E402

BPS = 10_000
FEE_BPS = 4
SLIPPAGE_BPS = 2
QUOTE_SCALE = 6
PRICE_SCALE = 5
QTY_SCALE = 8
CAPITAL = 1_000_000_000  # 1,000 USDC in quote units
MIN_NOTIONAL = 1_000_000
MAX_ORDERS = 16
MAX_WEIGHT_BPS = 10_000
MAX_POSITIONS = 1
SYMBOL = "CBBTC"

HELLO = {
    "type": "hello",
    "protocol": "agent-fun/bars-replay/v1",
    "seed": "0",
    "bar_interval": "1m",
    "first_close_ms": 1794926700000,
    "capital": {"quote": "USDC", "quote_scale": QUOTE_SCALE, "quote_units": str(CAPITAL)},
    "symbols": [{"symbol": SYMBOL, "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
                 "price_scale": PRICE_SCALE, "qty_scale": QTY_SCALE}],
    "caps": {"max_open_positions": MAX_POSITIONS, "max_orders_per_bar": MAX_ORDERS,
             "max_weight_bps": MAX_WEIGHT_BPS, "shorts": False, "leverage": False},
    "fees": {"fee_bps": FEE_BPS, "slippage_bps": SLIPPAGE_BPS,
             "min_notional_quote_units": str(MIN_NOTIONAL)},
}


def value(qty, price):
    """barsValue: what qty is worth at price, in quote units (truncating)."""
    return qty * price * 10 ** QUOTE_SCALE // 10 ** (QTY_SCALE + PRICE_SCALE)


class Pool:
    """BarsPool replica: quote side y = yn/yd against invariant x*y = D^2 = kn/kd."""

    def __init__(self, yn, yd, kn, kd):
        g = gcd(yn, yd)
        self.yn, self.yd, self.kn, self.kd = yn // g, yd // g, kn, kd
        self.unit = 10 ** 6

    @classmethod
    def of(cls, depth):
        d = [x * 10 ** QUOTE_SCALE for x in depth]
        prod = 1
        for x in d:
            prod *= x
        total = 0
        for i in range(len(d)):
            other = 1
            for j, x in enumerate(d):
                if j != i:
                    other *= x
            total += other
        g = gcd(prod, total)
        p, s = prod // g, total // g
        return cls(p, s, p * p, s * s)

    def buy(self, spend):
        yn2 = self.yn + spend * self.unit * self.yd
        num = self.yn * yn2 * self.kd
        den = self.yd * self.yd * self.kn
        return num, den, self._spawn(yn2, self.yd)

    def _spawn(self, yn, yd):
        p = object.__new__(Pool)
        g = gcd(yn, yd)
        p.yn, p.yd, p.kn, p.kd, p.unit = yn // g, yd // g, self.kn, self.kd, self.unit
        return p

    def sell(self, val):
        t = self.kn * self.yd + val * self.unit * self.yn * self.kd
        num = t * self.yd
        den = self.kd * self.yn * self.yn
        return num, den, self._spawn(self.kn * self.yn, t)


def buy_fill(notional, open_price, pool):
    spend = notional * (BPS - FEE_BPS) // BPS
    num, den, after = pool.buy(spend)
    price = open_price * (BPS + SLIPPAGE_BPS) * num // (BPS * den)
    qty = spend * 10 ** (QTY_SCALE + PRICE_SCALE) // (price * 10 ** QUOTE_SCALE)
    return price, spend, notional - spend, qty, after


def sell_fill(qty, open_price, pool):
    num, den, after = pool.sell(value(qty, open_price))
    slipped = open_price * (BPS - SLIPPAGE_BPS) * den // (BPS * num)
    price = slipped if slipped >= 1 else 1
    gross = value(qty, price)
    proceeds = gross * (BPS - FEE_BPS) // BPS
    return price, gross, gross - proceeds, proceeds, after


def load_bars():
    path = os.path.join(KIT, "dataset", "bars", "CBBTC.ndjson.gz")
    bars = []
    with gzip.open(path, "rt") as f:
        for line in f:
            b = json.loads(line)
            bars.append({
                "symbol": SYMBOL, "t": b["t"],
                "o": int(b["o"]), "h": int(b["h"]), "l": int(b["l"]), "c": int(b["c"]),
                "v": b["v"],
                "o_depth": [int(x) for x in b["o_depth"]],
                "c_depth": [int(x) for x in b["c_depth"]],
            })
    return bars


class Replica:
    """The frozen replay: decide on batch k, fill at bar k+1's open, flatten at the end."""

    def __init__(self, bars):
        self.bars = bars
        self.cash = CAPITAL
        self.qty = 0
        self.cost = 0
        self.close = None
        self.trades = 0
        self.fees = 0
        self.round_trips = []
        self._trip_pnl = None
        self.equity_curve = [CAPITAL]
        self.rejects = {}

    def equity(self):
        e = self.cash
        if self.qty > 0:
            e += value(self.qty, self.close)
        return e

    def _reject(self, reason):
        self.rejects[reason] = self.rejects.get(reason, 0) + 1

    def decide(self, orders, seq):
        """The engine's decision checks, in order; returns pending order dicts."""
        is_last = seq == len(self.bars) - 1
        equity = self.equity()
        buys = 0
        bought = 0
        sold = 0
        open_symbols = {SYMBOL} if self.qty > 0 else set()
        pending = []
        for index, o in enumerate(orders):
            amount = int(o["notional_quote_units"]) if o["side"] == "buy" else int(o["qty"])
            reject = None
            if index >= MAX_ORDERS:
                reject = "max_orders_per_bar"
            elif amount == 0:
                reject = "zero_amount"
            elif is_last:
                reject = "after_end"
            elif self.close is None:
                reject = "no_bar"
            elif o["side"] == "buy":
                cash_left = self.cash - buys
                held_value = value(self.qty, self.close)
                if amount < MIN_NOTIONAL:
                    reject = "below_min_notional"
                elif amount > cash_left:
                    reject = "insufficient_cash"
                elif SYMBOL not in open_symbols and len(open_symbols) >= MAX_POSITIONS:
                    reject = "max_open_positions"
                elif (held_value + bought + amount) * BPS > MAX_WEIGHT_BPS * equity:
                    reject = "max_weight"
                if reject is None:
                    buys += amount
                    bought += amount
                    open_symbols.add(SYMBOL)
            else:
                available = self.qty - sold
                if amount > available:
                    reject = "insufficient_position"
                elif amount != available and value(amount, self.close) < MIN_NOTIONAL:
                    reject = "below_min_notional"
                if reject is None:
                    sold += amount
            pending.append({"order": o, "amount": amount, "reject": reject,
                            "close": self.close, "seq": seq})
        return pending

    def resolve(self, pending, bar):
        fills = []
        pool = Pool.of(bar["o_depth"])
        for p in pending:
            o = p["order"]
            if p["reject"] is not None:
                self._reject(p["reject"])
                fills.append({"client_id": o.get("client_id"), "status": "rejected",
                              "reason": p["reject"]})
                continue
            max_slip = int(o["max_slippage_bps"])
            if o["side"] == "buy":
                price, spend, fee, qty, after = buy_fill(p["amount"], bar["o"], pool)
                if price * BPS > p["close"] * (BPS + max_slip):
                    self._reject("max_slippage")
                    fills.append({"client_id": o.get("client_id"), "status": "rejected",
                                  "reason": "max_slippage"})
                    continue
                if qty == 0:
                    self._reject("zero_amount")
                    continue
                pool = after
                self.cash -= p["amount"]
                if self.qty == 0:
                    self._trip_pnl = 0
                self.qty += qty
                self.cost += p["amount"]
                self.trades += 1
                self.fees += fee
                self._trip_pnl -= p["amount"]
                fills.append({"client_id": o.get("client_id"), "status": "filled",
                              "price": str(price), "qty": str(qty)})
            else:
                price, gross, fee, proceeds, after = sell_fill(p["amount"], bar["o"], pool)
                if price * BPS < p["close"] * (BPS - max_slip):
                    self._reject("max_slippage")
                    fills.append({"client_id": o.get("client_id"), "status": "rejected",
                                  "reason": "max_slippage"})
                    continue
                pool = after
                self.cash += proceeds
                self.cost = self.cost * (self.qty - p["amount"]) // self.qty
                self.qty -= p["amount"]
                self.trades += 1
                self.fees += fee
                self._trip_pnl += proceeds
                if self.qty == 0:
                    self.round_trips.append(self._trip_pnl)
                    self._trip_pnl = None
                fills.append({"client_id": o.get("client_id"), "status": "filled",
                              "price": str(price), "qty": str(p["amount"])})
        return fills

    def run(self, on_batch):
        state = makefun_bars.BarsState(HELLO)
        pending = []
        for k, bar in enumerate(self.bars):
            fills = self.resolve(pending, bar) if pending else []
            self.close = bar["c"]
            account = {
                "cash_quote_units": str(self.cash),
                "positions": ([{"symbol": SYMBOL, "qty": str(self.qty),
                                 "cost_quote_units": str(self.cost)}] if self.qty > 0 else []),
                "equity_quote_units": str(self.equity()),
            }
            wire_bar = {"symbol": SYMBOL, "t": bar["t"], "o": str(bar["o"]), "h": str(bar["h"]),
                        "l": str(bar["l"]), "c": str(bar["c"]), "v": bar["v"],
                        "o_depth": [str(x) for x in bar["o_depth"]],
                        "c_depth": [str(x) for x in bar["c_depth"]]}
            batch = {"type": "batch", "seq": k, "t": bar["t"], "bars": [wire_bar],
                     "fills": fills, "account": account}
            state.apply_batch(batch)
            orders = state.to_orders(on_batch(state, batch))
            state.record_orders(orders)
            pending = self.decide(orders, k)
            self.equity_curve.append(self.equity())
        # Flatten at the last close against its close depth.
        if self.qty > 0:
            last = self.bars[-1]
            pool = Pool.of(last["c_depth"])
            price, gross, fee, proceeds, after = sell_fill(self.qty, last["c"], pool)
            self.cash += proceeds
            self.fees += fee
            self._trip_pnl += proceeds
            self.round_trips.append(self._trip_pnl)
            self._trip_pnl = None
            self.qty = 0
            self.cost = 0
        peak = CAPITAL
        max_dd = 0.0
        for e in self.equity_curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak)
        return {
            "net_pnl": self.cash - CAPITAL,
            "final_cash": self.cash,
            "trades": self.trades,
            "fees": self.fees,
            "max_dd": max_dd,
            "round_trips": self.round_trips,
            "rejects": self.rejects,
        }


# --------------------------------------------------------------------------
# Baseline ports (kit baselines/*/main.ts, same rules, single symbol CBBTC)
# --------------------------------------------------------------------------

def baseline_hold_cash():
    def on_batch(state, batch):
        return []
    return on_batch


def baseline_buy_and_hold():
    decided = {"done": False}

    def on_batch(state, batch):
        if decided["done"] or state.warming_up:
            return []
        if state.close(SYMBOL) is None:
            return []
        decided["done"] = True
        caps, fees = state.hello["caps"], state.hello["fees"]
        n = min(1, caps["max_open_positions"], caps["max_orders_per_bar"])
        even = state.cash // n
        by_weight = caps["max_weight_bps"] * state.equity // BPS
        slice_ = min(even, by_weight)
        if slice_ == 0 or slice_ < int(fees["min_notional_quote_units"]):
            return []
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": slice_,
                 "max_slippage_bps": 10_000}]
    return on_batch


def _stake_orders(state, entries, sells):
    caps, fees = state.hello["caps"], state.hello["fees"]
    orders = list(sells[:caps["max_orders_per_bar"]])
    slots = min(len(state.hello["symbols"]), caps["max_open_positions"])
    stake = state.equity // slots
    by_weight = caps["max_weight_bps"] * state.equity // BPS
    room = slots - len(state.positions)
    cash = state.cash
    for symbol in entries:
        if room <= 0 or len(orders) >= caps["max_orders_per_bar"]:
            break
        slice_ = min(stake, by_weight, cash)
        if slice_ == 0 or slice_ < int(fees["min_notional_quote_units"]):
            break
        orders.append({"side": "buy", "symbol": symbol, "notional_quote_units": slice_,
                       "max_slippage_bps": 100})
        cash -= slice_
        room -= 1
    return orders


def baseline_ema_cross():
    fast = {"span": 8, "warm": [], "value": None}
    slow = {"span": 21, "warm": [], "value": None}
    ctx = {"gap": None, "now": None, "down": False}

    def step(e, close):
        if e["value"] is not None:
            e["value"] += (2 / (e["span"] + 1)) * (close - e["value"])
            return
        e["warm"].append(close)
        if len(e["warm"]) == e["span"]:
            e["value"] = sum(e["warm"]) / e["span"]

    def on_batch(state, batch):
        cross_up = False
        fresh = False
        for bar in batch["bars"]:
            fresh = True
            c = float(int(bar["c"]))
            step(fast, c)
            step(slow, c)
            ctx["gap"] = ctx["now"]
            ctx["now"] = (fast["value"] - slow["value"]) if (fast["value"] is not None
                                                             and slow["value"] is not None) else None
            if ctx["now"] is None or ctx["now"] >= 0:
                ctx["down"] = False
            if ctx["gap"] is not None and ctx["now"] is not None and bar["v"] != "0":
                if ctx["now"] > 0 and ctx["gap"] <= 0:
                    cross_up = True
                if ctx["now"] < 0 and ctx["gap"] >= 0:
                    ctx["down"] = True
        if state.warming_up or not fresh:
            return []
        held = state.positions.get(SYMBOL)
        sells, entries = [], []
        if held is not None:
            val = state.value(SYMBOL, held.qty)
            if ctx["down"] or val * 2 >= held.cost * 3 or val * 5 <= held.cost * 4:
                sells.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                              "max_slippage_bps": 100})
        elif cross_up:
            entries.append(SYMBOL)
        return _stake_orders(state, entries, sells)
    return on_batch


def baseline_rsi_bollinger():
    s = {"typical": [], "last": None, "first": [], "gain": None, "loss": None}

    def push(bar):
        c = float(int(bar["c"]))
        s["typical"].append((float(int(bar["h"])) + float(int(bar["l"])) + c) / 3)
        if len(s["typical"]) > 20:
            s["typical"].pop(0)
        if s["last"] is not None:
            change = c - s["last"]
            up = change if change > 0 else 0.0
            down = -change if change < 0 else 0.0
            if s["gain"] is None:
                s["first"].append(change)
                if len(s["first"]) == 14:
                    s["gain"] = sum(x for x in s["first"] if x > 0) / 14
                    s["loss"] = sum(-x for x in s["first"] if x < 0) / 14
            else:
                s["gain"] = (s["gain"] * 13 + up) / 14
                s["loss"] = (s["loss"] * 13 + down) / 14
        s["last"] = c

    def on_batch(state, batch):
        fresh = False
        for bar in batch["bars"]:
            push(bar)
            fresh = True
        if not fresh:
            return []
        r = None
        if s["gain"] is not None:
            moved = s["gain"] + s["loss"]
            r = 50.0 if moved == 0 else 100.0 * s["gain"] / moved
        lower = None
        if len(s["typical"]) == 20:
            mean = sum(s["typical"]) / 20
            var = sum((x - mean) ** 2 for x in s["typical"]) / 20
            lower = mean - 2 * math.sqrt(var)
        held = state.positions.get(SYMBOL)
        sells, entries = [], []
        if held is not None:
            val = state.value(SYMBOL, held.qty)
            if (r is not None and r > 70) or val * 10 >= held.cost * 11 or val * 4 <= held.cost * 3:
                sells.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                              "max_slippage_bps": 100})
        elif r is not None and lower is not None and r < 30 and s["last"] < lower:
            entries.append(SYMBOL)
        return _stake_orders(state, entries, sells)
    return on_batch


def baseline_momentum():
    history = []
    ctx = {"next": 24, "entering": None}

    def buys(state, want):
        if not want:
            return []
        caps, fees = state.hello["caps"], state.hello["fees"]
        room = min(3, caps["max_open_positions"], caps["max_orders_per_bar"]) - len(state.positions)
        coming = [SYMBOL] if (SYMBOL in want and SYMBOL not in state.positions
                              and state.close(SYMBOL) is not None) else []
        coming = coming[:room if room > 0 else 0]
        if not coming:
            return []
        even = state.cash // len(coming)
        by_weight = caps["max_weight_bps"] * state.equity // BPS
        slice_ = min(even, by_weight)
        if slice_ == 0 or slice_ < int(fees["min_notional_quote_units"]):
            return []
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": slice_,
                 "max_slippage_bps": 10_000}]

    def on_batch(state, batch):
        history.append(state.close(SYMBOL))
        if ctx["entering"] is not None:
            orders = buys(state, ctx["entering"])
            ctx["entering"] = None
            return orders
        if batch["seq"] < ctx["next"]:
            return []
        ctx["next"] = batch["seq"] + 24
        seq = batch["seq"]
        now, then = history[seq], history[seq - 24]
        want = [SYMBOL] if (now is not None and then is not None) else []
        sells = []
        if SYMBOL in state.positions and SYMBOL not in want:
            sells.append({"side": "sell", "symbol": SYMBOL,
                          "qty": state.positions[SYMBOL].qty, "max_slippage_bps": 10_000})
        if not sells:
            return buys(state, want)
        ctx["entering"] = want
        return sells
    return on_batch


def baseline_adx_momentum():
    def wilder(span):
        return {"span": span, "moves": 0, "plus": 0.0, "minus": 0.0, "range": 0.0}

    s = {"prev": None, "closes": [], "short": wilder(14), "long": wilder(25),
         "dx_sum": 0.0, "dx_bars": 0, "adx": None}

    def smooth(w, plus, minus, rng):
        w["moves"] += 1
        if w["moves"] < w["span"]:
            w["plus"] += plus
            w["minus"] += minus
            w["range"] += rng
            return
        w["plus"] = w["plus"] - w["plus"] / w["span"] + plus
        w["minus"] = w["minus"] - w["minus"] / w["span"] + minus
        w["range"] = w["range"] - w["range"] / w["span"] + rng

    def dis(w):
        if w["moves"] < w["span"]:
            return None
        if w["range"] == 0:
            return (0.0, 0.0)
        return (100 * (w["plus"] / w["range"]), 100 * (w["minus"] / w["range"]))

    def push(bar):
        h, l, c = float(int(bar["h"])), float(int(bar["l"])), float(int(bar["c"]))
        if s["prev"] is not None:
            up = h - s["prev"]["h"]
            down = s["prev"]["l"] - l
            plus = up if (up > 0 and up > down) else 0.0
            minus = down if (down > 0 and down > up) else 0.0
            rng = max(h - l, abs(h - s["prev"]["c"]), abs(l - s["prev"]["c"]))
            smooth(s["short"], plus, minus, rng)
            smooth(s["long"], plus, minus, rng)
            di = dis(s["short"])
            if di is not None:
                total = di[0] + di[1]
                dx = None if total == 0 else 100 * (abs(di[0] - di[1]) / total)
                if s["adx"] is None:
                    if dx is not None:
                        s["dx_sum"] += dx
                    s["dx_bars"] += 1
                    if s["dx_bars"] == 14:
                        s["adx"] = s["dx_sum"] / 14
                elif dx is not None:
                    s["adx"] = (s["adx"] * 13 + dx) / 14
        s["prev"] = {"h": h, "l": l, "c": c}
        s["closes"].append(c)
        if len(s["closes"]) > 15:
            s["closes"].pop(0)
        di = dis(s["long"])
        if s["adx"] is None or di is None or len(s["closes"]) <= 14 or not (s["adx"] > 25):
            return None
        mom = c - s["closes"][0]
        plus_di, minus_di = di
        if mom > 0 and plus_di > 25 and plus_di > minus_di:
            return "up"
        if mom < 0 and minus_di > 25 and plus_di < minus_di:
            return "down"
        return None

    def on_batch(state, batch):
        signal = None
        fresh = False
        for bar in batch["bars"]:
            signal = push(bar)
            fresh = True
        if not fresh:
            return []
        held = state.positions.get(SYMBOL)
        sells, entries = [], []
        if held is not None:
            val = state.value(SYMBOL, held.qty)
            if signal == "down" or val * 100 >= held.cost * 101 or val * 4 <= held.cost * 3:
                sells.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                              "max_slippage_bps": 100})
        elif signal == "up":
            entries.append(SYMBOL)
        return _stake_orders(state, entries, sells)
    return on_batch


BASELINES = [
    ("hold-cash", baseline_hold_cash),
    ("buy-and-hold", baseline_buy_and_hold),
    ("ema-cross", baseline_ema_cross),
    ("momentum", baseline_momentum),
    ("rsi-bollinger", baseline_rsi_bollinger),
    ("adx-momentum", baseline_adx_momentum),
]


def fmt(res):
    return ("net PnL %+0.6f USDC | trades %d | fees %0.6f USDC | max DD %.3f%% | "
            "round trips %s | rejects %s" % (
                res["net_pnl"] / 1e6, res["trades"], res["fees"] / 1e6, res["max_dd"] * 100,
                ["%+.2f" % (x / 1e6) for x in res["round_trips"]], res["rejects"] or "-"))


def main():
    bars = load_bars()
    print("dev window: %d bars, close %.2f -> %.2f USDC" % (
        len(bars), bars[0]["c"] / 1e5, bars[-1]["c"] / 1e5))
    params = {}
    verbose = "--verbose" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--params"):
            for kv in arg.split("=", 1)[1].split(","):
                k, v = kv.split("=")
                params[k] = float(v) if "." in v else int(v)
    sys.path.insert(0, HERE)
    import main as bot_main
    res = Replica(bars).run(bot_main.make_on_batch(params or None))
    print("flush-bot %s: %s" % (params or "default", fmt(res)))
    if verbose:
        for name, factory in BASELINES:
            r = Replica(bars).run(factory())
            print("baseline %-14s: %s" % (name, fmt(r)))


if __name__ == "__main__":
    main()
