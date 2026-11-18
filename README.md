# btc-race-24h-20261118-1444-entry

[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/raraanzaii/btc-race-24h-20261118-1444-entry/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/raraanzaii/btc-race-24h-20261118-1444-entry/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

**Flush-and-Recover** — a cbBTC trading bot for make.fun's 24-hour BTC
race (bars-replay/v1: one JSON line per 1-minute bar in, orders out;
fills at the next minute's open with 4 bps fee + 2 bps slippage + impact).

The strategy buys sharp flushes, not dips in general. On this competition's
frozen dev window, 1-minute oversold signals keep falling (the classic
dip-buying design lost money), while a close at least 1.8% under the
trailing 4-hour high average reliably bounces over the following hours.
So the bot:

- arms when the close is ≥1.8% under the trailing 4h high average and has
  lifted slightly off its 30-bar low,
- buys once with 95% of cash,
- exits on a +1.08% take-profit, recovery toward the 12h high, a −2% stop,
  or a 600-bar time stop,
- re-enters only on a deeper (−2.3%) flush.

On the kit's frozen dev window (BTC −2.80% over the window) the local
grading replica scores it **+$18.98 net (+1.90%), 4 trades, max drawdown
0.62%** — profitable after all fees, ahead of every kit baseline
(buy-and-hold −$31.63, ema-cross −$148.17), and it stays profitable at
double fees. `backtest.py` in this repo is the replica used to tune it.

Python 3, standard library only, deterministic: two runs over the wire
protocol produce byte-identical replies.

## Run it

```sh
python3 -P -s main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=kagami-tanaka&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
