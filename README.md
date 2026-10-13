# pons-research

Can a bot make money sniping new memecoins on a bonding-curve launchpad?

I wanted a bot that buys new tokens on Pons, a launchpad on Robinhood
Chain, and doubles its money. Before risking a cent, I recorded
every launch and every trade, rebuilt the curve's math from that data,
backtested hundreds of rules, and ran the best ones live as paper trades.

**Short answer: no.** After fees, every approach lost money on average.
The best one got to about zero. Over four days of data:

- 6,584 ETH-paired tokens that traded
- 240,000+ bonding-curve trades
- 6,767 live paper trades, plus a second live run on exact curve prices
- ~70,000 copied buys in the wallet test

![results](results.png)

No wallet, no keys, no transactions. Every script only reads public
chain data over JSON-RPC.

---

## How Pons actually works (measured, not taken from docs)

### 1. The curve is an exact constant-product curve

A launch starts from virtual reserves of **1.68 ETH and 1,000,000,000
tokens**, and every trade moves along `x * y = k`.

- Fitting the dev's first buy on 4,502 launches gives **R² = 1.0000**.
- Replaying reserves through 97,470 later buys predicts the tokens out with
  **0.00% median and 0.00% 90th-percentile error**.

Each launch event carries a 4.2 ETH graduation target. Once the curve has
taken in that much, the token moves to a Uniswap v4 pool. That caps the price on the curve at (5.88 / 1.68)² ≈ **12.25x the launch
price**. The biggest runners in the data peaked at 10.4x to 12.1x and then
graduated. A 100x can only happen after graduation.

### 2. Fees: 1% to the platform, plus a creator fee fixed per token

Every buy and every sell pays a flat 1%, plus a second fee. The second fee
never changes within a token (true for all 2,677 tokens with 3+ trades), so
it is visible from the first trade.

| second fee | share of tokens |
|---|---|
| 0% | 14% |
| 1% | 14% |
| 2% | 42% |
| 3% | 11% |
| 10% | 2% |

A round trip therefore costs between ~2% and ~21% before slippage and gas.
The 10% tokens averaged **-25% per trade** in paper trading.

### 3. Most tokens die within minutes

From the first 20 hours (1,427 tokens at least 2 hours old):

| | share |
|---|---|
| got even one buyer besides the dev | 65% |
| got 5+ outside buyers | 29% |
| still trading after 1 hour | 25% |
| ever reached 2x the launch price | 9.9% |
| ever reached 10x | 0.4% |
| graduated | 0.6% |

The median token's last trade came **4.5 minutes** after launch. Over the
full four days about 1% of tokens graduated.

### 4. After graduation it is a lottery with a bad average

Graduated tokens swing wildly from day to day. On one day, every token
whose graduation price I had was between 0.06x and 0.8x of it. The next
day, during a market-wide pump, a few were 5x to 15x up. By the last
snapshot, more than 20 of the 68 graduated tokens sat at 0.05x to 0.09x.

`hold_test.py` asked the plain question: put $1 into every token the first
time it reaches N times its launch price, and hold.

| entry | tokens | $1 became |
|---|---|---|
| 2x launch price (~$8.6k market cap) | 334 | $0.76 |
| 5x launch price (~$21k) | 85 | $0.62 |

---

## What was tested

All results are per trade, after the 1% fee, the token's own fee, and
slippage on both sides. Gas (~0.5% of a 0.002 ETH trade) is not included.

| approach | data | result per trade |
|---|---|---|
| Buy every launch 5 s in, sell after 10 min | 3,019 tokens, backtest | -7.1% |
| "2x before half": enter at 2 min, take 2x, stop at -50% | 3,271 tokens, backtest | -10.3% |
| 1,008 entry / filter / exit combinations | 43 hours, backtest | 0 profitable in both halves; best ≈ -0.5% |
| 6 exit strategies, live | 6,767 live paper trades | -3.5% to -5.3% |
| Best filter: creator fee ≤ 1%, no outside buyers yet, 1.2x or 10 min | backtest, period it was *not* found on (297 trades) | +0.3% to +1.3% |
| same rule, live at exact curve prices | 105 live paper trades | -1.2% (95% range -5.8 to +3.7) |
| Copy every buyer 2 s later | 39,042 copied buys | -6.0% |
| Copy the 25 best wallets (picked on the first half of the data) | 316 tokens, second half | -0.35% (95% range -3.1 to +2.4) |

**Speed was not the problem.** The live bot saw new launches about 0.5 to
0.9 seconds after they were mined.

**The one partial signal is wallets.** Copying the best wallets did 3 to 6
points better than copying everyone, so wallet choice carries some
information. It still did not beat the fees. Six of the first half's top 10
wallets lost money in the second half (one fell from +13% to -21%), which
looks more like luck than skill.

### Why it does not work

A bonding curve is close to zero-sum between traders. An early buyer only
profits if later buyers pay more, and on every trade the platform and the
creator take their cut. Most tokens never get later buyers. The rest of the
money goes to whoever is fastest and best informed, and a home bot reading
public data is neither.

---

## Five mistakes worth keeping

Every one was caught by the data, and each one had made a losing strategy
look profitable.

1. **A verdict that ranked on one half and checked only the other.** The
   first backtest called rules "profitable" that had lost money in the half
   used to pick them and won in the next. That was a market swing, not an
   edge. Fix: a rule must make money in both halves.
2. **Average trade price is not the price you get.** Right after the dev's
   buy, the last trade's average price sits about 1.3% below the curve's
   real price. Every later buy then looked like a gain. Fix: fit the curve
   and price every entry and exit from its reserves.
3. **Token-paired curves priced as if they were ETH.** Some launches pair
   with another token, not ETH. Pricing them with the ETH formula produced
   "72x in 68 seconds" trades. 16 such trades turned the baseline's -5.5% into a
   reported +5.5%. Fix: skip anything that is not an ETH curve, and reject
   any price the ETH curve cannot produce.
4. **One snapshot is not a result.** Graduated tokens were 0.06x to 0.8x
   on one day and 5x to 15x the next. Any single day can tell either story.
5. **One malformed RPC reply killed both bots for hours.** An endpoint
   returned its error as a plain string instead of an object. Fix: treat
   every reply as untrusted, retry, and skip bad logs instead of crashing.

---

## Scripts

| script | what it does |
|---|---|
| `probe_robinhood.py` | Checks RPCs, block time and launch rate, and decodes one launch event. |
| `watcher.py` | Records every launch, curve trade and graduation to CSV. Resumes after restarts. |
| `analyze_pons.py` | Fees, funnel, peaks, the "2x before half" bet, dev-sold signal. |
| `grad_tracker.py` | Looks up graduated tokens on DexScreener; appends snapshots over time. |
| `backtest_pons.py` | Replays 1,008 rules; a rule must win in both halves; fee analysis. |
| `check_rule.py` | Fits the curve, measures the pricing artifact, re-tests the best rule. |
| `hold_test.py` | Buy at N x launch price and hold until now. |
| `paper_trader.py` | Live paper trading at exact curve prices, several strategies at once (version 3.1; the first live run priced from the last trade, see mistake 2). |
| `paper_report.py` | Scores the paper trades, including small-wallet position limits. |
| `wallet_test.py` | Picks wallets on the first half, follows them on the second. |

## Running it

```bash
pip install requests
python probe_robinhood.py     # sanity check
python watcher.py             # leave running; collects data
python analyze_pons.py        # after a few hours
python backtest_pons.py
python check_rule.py
python hold_test.py           # needs grad_tracker.py snapshots
python wallet_test.py         # needs a few days of data
python paper_trader.py        # live paper trading
python paper_report.py paper_trades_v3.csv
```

Python 3.10+. The only dependency is `requests`; everything else is the
standard library. Data files (`*.csv`, `*.json`) are gitignored.

## Limits

This is four days of one launchpad in one market mood. Paper trading
cannot show competition for the same block, failed transactions or MEV,
and all of those would make real results worse, not better. The tests cover
rules a bot can run on public on-chain data. They do not cover a trader
with off-chain information.

Not financial advice. The point of this repo is the opposite.
