"""
Wallet test on the pump.fun data: do some wallets keep picking winners, and
would copying them pay?

The same test as on Pons (wallet_test.py), on far more data:
  1. Coins are split by launch time. The first half chooses the wallets,
     the second half tests them. The test half is never used to choose.
  2. For each wallet's first buy of a coin (dev buys, buys under
     MIN_COPY_SOL and buys later than COPY_WINDOW after launch left out), a
     copy is simulated: our order lands REACT seconds after the bot SAW
     their buy, at the curve's price by then, priced exactly like
     sol_bot.py (the coin's fees on both sides, price impact, gas). Exits:
       A) 1.2x / -50% / 10 min
       B) when that wallet sells (at most 60 min later)
  3. On the first half, every wallet with MIN_TOKENS+ coins is scored by the
     average result of copying it, minus one standard error (so a lucky
     wallet with few coins does not come first).
  4. On the second half: follow the top 10 / 25 / 50 / 100 wallets (one copy
     per coin, the first of them to buy) and compare with following
     everyone.

A wallet signal counts only if the top wallets beat "every buy" on the
second half by more than the 95% range, and stay above the +3% bar.

Read-only analysis of local files. Needs sol_backtest.py in the same folder.

    python sol_wallet_test.py
"""

import bisect
import statistics
import sys
import time
from collections import defaultdict

import sol_backtest as bt

REACT = 1.0                 # seconds from seeing their buy to our order landing
MIN_COPY_SOL = 0.02         # their buy must be at least this big
MIN_TOKENS = 10             # wallet needs this many coins in the first half
TP, SL, HOLD = 1.2, 0.5, 600
MIRROR_MAX = 3600
COPY_WINDOW = 3000          # only buys in the first 50 min (trades are recorded for 2 h)
TOPS = [10, 25, 50, 100]
HORIZON = COPY_WINDOW + MIRROR_MAX + 2 * REACT + 10


def copy_trade(c: bt.Coin, i: int) -> tuple:
    """Copy the buy at trade i. Returns (net A, net B) or None."""
    t_buy = c.ts[i]
    fill = t_buy + REACT
    if c.grad <= fill:
        return None                          # graduated before our copy landed
    j = bisect.bisect_left(c.ts, fill) - 1
    vq, vt, fee = c.state(j)
    net_in = bt.SIZE / (1 + fee)
    tokens = vt * net_in / (vq + net_in)
    hit_tp, hit_sl = bt.crossings(c, fill, j, net_in, tokens, fill + HOLD, [TP], [SL])
    net_a = bt.sell(c, bt.exit_time(fill, hit_tp, hit_sl, TP, SL, HOLD) + REACT, tokens)
    sells = c.sells.get(c.users[i], ())
    k = bisect.bisect_right(sells, t_buy)
    out = sells[k] if k < len(sells) and sells[k] <= t_buy + MIRROR_MAX else t_buy + MIRROR_MAX
    net_b = bt.sell(c, out + REACT, tokens)
    return net_a, net_b


def summary(xs: list) -> str:
    n = len(xs)
    if n == 0:
        return f"{0:>6}        -                 -          -      -"
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / n ** 0.5 if n > 1 else 0.0
    nb = (sum(xs) - max(xs)) / (n - 1) if n > 1 else m
    win = sum(1 for x in xs if x > 0) / n * 100
    return (f"{n:>6}   {m * 100:>+6.2f}%   [{(m - 1.96 * se) * 100:>+5.1f}, "
            f"{(m + 1.96 * se) * 100:>+5.1f}]   {nb * 100:>+6.2f}%   {win:>3.0f}%")


def main() -> None:
    started = time.time()
    coins, total, n_trades, data_end = bt.load(horizon=HORIZON, details=True)
    if len(coins) < 200:
        sys.exit(f"Only {len(coins)} usable coins - let sol_bot.py collect more data.")
    split = coins[len(coins) // 2].t0

    # copies of every wallet's first real buy of each coin
    copies = []        # (wallet, mint, launch, net A, net B, seconds after launch, buy time)
    for c in coins:
        done = set()
        limit = c.t0 + COPY_WINDOW
        for i, t in enumerate(c.ts):
            if t > limit:
                break
            who = c.users[i]
            if (not c.is_buy[i] or c.sol[i] < MIN_COPY_SOL * 1e9 or who == c.dev
                    or who in done):
                continue
            done.add(who)
            r = copy_trade(c, i)
            if r is not None:
                copies.append((who, c.mint, c.t0, r[0], r[1], t - c.t0, t))

    train = [x for x in copies if x[2] < split]
    test = [x for x in copies if x[2] >= split]
    fmt = lambda t: time.strftime("%d %b %H:%M", time.localtime(t))  # noqa: E731
    print(f"\n{len(coins)} coins with a full history ({fmt(coins[0].t0)} to "
          f"{fmt(coins[-1].t0)}), {len(copies)} copyable buys "
          f"({len(train)} in the first half, {len(test)} in the second)")

    by_wallet = defaultdict(list)
    for x in train:
        by_wallet[x[0]].append(x)
    scored = []
    for w, xs in by_wallet.items():
        if len(xs) < MIN_TOKENS:
            continue
        nets = [x[3] for x in xs]
        m = statistics.fmean(nets)
        scored.append((m - statistics.stdev(nets) / len(nets) ** 0.5, m, len(xs), w))
    scored.sort(reverse=True)
    print(f"wallets with at least {MIN_TOKENS} coins in the first half: {len(scored)}")
    if not scored:
        print("Not enough data yet - run again later.")
        return

    def follow(wallets: set, exit_b: bool) -> list:
        """First buy per coin among the chosen wallets, in the test half."""
        first = {}
        for x in sorted(test, key=lambda x: x[6]):
            if x[0] in wallets and x[1] not in first:
                first[x[1]] = x[4] if exit_b else x[3]
        return list(first.values())

    for label, exit_b in (("A) sell at 1.2x / -50% / 10 min", False),
                          ("B) sell when the wallet sells (max 60 min)", True)):
        print(f"\nCOPY RESULTS ON THE SECOND HALF - exit {label}\n")
        print("  follow                          coins     avg     95% range       w/o best   win")
        print("  " + "-" * 84)
        for k in TOPS:
            top = {w for *_, w in scored[:k]}
            print(f"  top {k:<3} wallets of 1st half    {summary(follow(top, exit_b))}")
        everyone = {w for *_, w in scored}
        print(f"  all {len(everyone):<5} scored wallets      {summary(follow(everyone, exit_b))}")
        print(f"  every buy, any wallet          "
              f"{summary([x[4] if exit_b else x[3] for x in test])}")

    print("\nDO THE BEST WALLETS STAY GOOD? (exit A)\n")
    print("  wallet           1st half: coins    avg   |  2nd half: coins    avg   "
          "| buys, s after launch")
    test_by = defaultdict(list)
    for x in test:
        test_by[x[0]].append(x)
    for _, m, n, w in scored[:10]:
        t_nets = [x[3] for x in test_by.get(w, [])]
        t_avg = f"{statistics.fmean(t_nets) * 100:>+6.1f}%" if t_nets else "      -"
        delay = statistics.median(x[5] for x in by_wallet[w])
        print(f"  {w[:6]}...{w[-4:]}  {n:>15}  {m * 100:>+6.1f}%   |  {len(t_nets):>14}  "
              f"{t_avg}  | {delay:>8.0f}")

    print(f"\n  Copies land {REACT:g} s after the bot saw the wallet's buy, after fees,")
    print("  price impact and gas. A wallet signal is real only if the top wallets")
    print("  beat 'every buy' on the second half by more than the 95% range, and")
    print("  stay above +3%.")
    print(f"  ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
