"""Flush-and-recover bot for the btc-race-24h bars-replay/v1 harness.

Written for a tape like the kit's dev window: cbBTC grinds down in a sharp
flush, bases for hours a couple of percent under its recent high, then
recovers most of the drop. One-minute RSI dip-buying was backtested first
and rejected: on this window oversold 1-minute RSI has negative forward
returns (it catches the flush, not the bottom) and the round trips bleed
fees. What the data supports is slower and pickier:

  - ARM: the close sits at least ARM_DD under the highest close of the
    last HIGH_BARS bars -- a real flush, not noise -- and the price has
    lifted LIFT off the lowest close of the last BASE_BARS bars, so the
    bot is not buying mid-crash.
  - BUY once, with SIZE_FRAC of the cash. One position at a time.
  - SELL the whole position when the first of these fires:
      * take profit: worth TAKE_PROFIT over cost (fees included);
      * recovery: the close is back within EXIT_DD of the highest close
        of the last EXIT_HIGH_BARS bars and the position is at least
        MIN_PROFIT over cost (the flush has been retraced);
      * stop loss: worth STOP_LOSS under cost (the flush was a downtrend
        leg, not a base);
      * time stop: MAX_HOLD bars held (never ride a stale position into
        the end-of-window flatten, which sells on worse terms).
  - After a sell, sit out COOLDOWN bars; a second entry needs a deeper
    flush (REARM_DD), so late-window noise cannot re-arm the bot.

Every order carries max_slippage_bps = SLIP_BPS: an order decided on a
close is refused rather than filled if the next open (plus modelled
slippage and the order's own price impact) lands more than that past the
close. Deterministic: a pure function of the bars seen so far. No clock,
no environment, no randomness, no network, standard library only.

Revision note (after the first full backtest of this design): the first
bounce off a flush low is sharp and brief, and the first parameter set
(lift 0.10%, take profit +1.20%) rode it straight back down, banking one
round trip where the tape offered two. The lift was trimmed to 0.06% so
the entry is not a bar late, and the take profit to +1.08% so that first
bounce is banked; the re-dip then re-arms the bot for the slower recovery
leg. Both points sit in the middle of a flat region of the parameter
plane (every neighbouring lift/take-profit pair grades within a dollar
of the chosen one on the dev window), not on a spike.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (tuned on the kit's frozen dev window; see README) ---
HIGH_BARS = 240          # trailing window for the flush anchor (4 hours)
ARM_DD_BPS = -180        # arm when close <= high * (1 - 1.80%)
BASE_BARS = 30           # the base window for the lift-off check
LIFT_BPS = 6             # buy only once close >= 30-bar low * (1 + 0.06%)
EXIT_HIGH_BARS = 720     # trailing window for the recovery anchor (12 hours)
EXIT_DD_BPS = -40        # recovered when close >= high720 * (1 - 0.40%)
MIN_PROFIT_BPS = 30      # ... and the position is at least +0.30% over cost
TAKE_PROFIT_BPS = 108    # sell at +1.08% over cost
STOP_LOSS_BPS = 200      # sell at -2.00% under cost
MAX_HOLD = 600           # sell after this many bars in the position
REARM_DD_BPS = -230      # a second entry needs a deeper flush: -2.30%
COOLDOWN = 30            # bars to sit out after a sell
SIZE_NUM = 19            # buy with 19/20 of the cash on hand
SIZE_DEN = 20
SLIP_BPS = 200           # max_slippage_bps on every order
BPS = 10_000


class FlushBot:
    """The strategy as a stateful on_batch callable (one instance per run)."""

    def __init__(self, params=None):
        p = params or {}
        self.high_bars = p.get("high_bars", HIGH_BARS)
        self.arm_dd = p.get("arm_dd_bps", ARM_DD_BPS)
        self.base_bars = p.get("base_bars", BASE_BARS)
        self.lift = p.get("lift_bps", LIFT_BPS)
        self.exit_high_bars = p.get("exit_high_bars", EXIT_HIGH_BARS)
        self.exit_dd = p.get("exit_dd_bps", EXIT_DD_BPS)
        self.min_profit = p.get("min_profit_bps", MIN_PROFIT_BPS)
        self.take_profit = p.get("take_profit_bps", TAKE_PROFIT_BPS)
        self.stop_loss = p.get("stop_loss_bps", STOP_LOSS_BPS)
        self.max_hold = p.get("max_hold", MAX_HOLD)
        self.rearm_dd = p.get("rearm_dd_bps", REARM_DD_BPS)
        self.cooldown = p.get("cooldown", COOLDOWN)
        self.size_num = p.get("size_num", SIZE_NUM)
        self.size_den = p.get("size_den", SIZE_DEN)
        self.slip_bps = p.get("slip_bps", SLIP_BPS)
        self.closes = []
        self._entry_seq = None
        self._flat_since = None
        self._trips = 0

    def on_batch(self, state, batch):
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)
        if len(self.closes) > self.exit_high_bars:
            self.closes.pop(0)
        if state.warming_up:
            return []
        held = state.positions.get(SYMBOL)

        if held is not None:
            if self._entry_seq is None:
                self._entry_seq = state.seq
            value = state.value(SYMBOL, held.qty)
            high_exit = max(self.closes)
            exit_now = (
                value * BPS >= held.cost * (BPS + self.take_profit)
                or value * BPS <= held.cost * (BPS - self.stop_loss)
                or (close * BPS >= high_exit * (BPS + self.exit_dd)
                    and value * BPS >= held.cost * (BPS + self.min_profit))
                or state.seq - self._entry_seq >= self.max_hold
            )
            if exit_now:
                self._entry_seq = None
                self._flat_since = state.seq
                self._trips += 1
                return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                         "max_slippage_bps": self.slip_bps}]
            return []

        # Flat: cooldown first, then the flush-and-lift entry.
        if self._flat_since is not None and state.seq - self._flat_since < self.cooldown:
            return []
        if len(self.closes) < 2:
            return []
        high = max(self.closes[-self.high_bars:])
        arm = self.arm_dd if self._trips == 0 else min(self.arm_dd, self.rearm_dd)
        if close * BPS > high * (BPS + arm):
            return []
        low = min(self.closes[-self.base_bars:])
        if close * BPS < low * (BPS + self.lift):
            return []
        notional = state.cash * self.size_num // self.size_den
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < int(state.hello["fees"]["min_notional_quote_units"]):
            return []
        self._entry_seq = state.seq + 1  # fills at the next bar's open
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional,
                 "max_slippage_bps": self.slip_bps}]


def make_on_batch(params=None):
    return FlushBot(params).on_batch


if __name__ == "__main__":
    makefun_bars.run(FlushBot().on_batch)
