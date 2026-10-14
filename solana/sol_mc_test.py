"""
Market-cap test on the pump.fun data: buy when a coin reaches a market cap,
sell at a higher one. Does it pay, and can a filter pick the "potential"
coins in advance?

For every coin that sol_bot.py recorded:
  1. the bot watches the market cap (curve price x 1B tokens x SOL_USD)
  2. the first time it reaches ENTRY within the first hour, the bot decides
     to buy; the order lands REACT s later at the curve's price by then (it
     may already be higher)
  3. sell when the market cap reaches TARGET, or falls to STOP x the market
     cap our order landed at, or after HOLD minutes, or at graduation; the
     sell also lands REACT s after the bot sees the trigger
  4. priced exactly like the paper trader: the coin's fees on both sides,
     price impact, gas

A target is a market cap in USD (25000) or a multiple of the market cap our
order landed at (2x). Entry $3k is the launch itself: a coin starts at about
$3k when SOL is $107, so "3000" means buy right after the launch. The curve
never falls below its launch price, so there a stop only fires when our
order landed well above it.

"Potential" filters, all known when the order is sent:
  - speed:   reached the entry market cap in under 30 s (sniped / bundled)
             or slower
  - buyers:  fewer than 15 outside wallets had bought, or 15+
  - dev:     the dev still holds, or has sold
  - top 10:  the 10 biggest wallets hold under 30% of the supply, or 30%+
  - fill:    our order landed less than 20% above the market cap the bot saw,
             or more. A 20% slippage limit keeps only the first group: the
             other orders fail on chain and cost only gas.

The pass rule is the same as before: +3% or more per trade in BOTH halves,
with 100+ trades in each half. With many rule and filter combinations a pass
is only a candidate until it passes again on new data.

Read-only analysis of local files. Needs sol_backtest.py in the same folder.

    python sol_mc_test.py                        # SOL $107, buy at $8k/$10k, sell at $25k/$30k
    python sol_mc_test.py 120                    # another SOL price in USD
    python sol_mc_test.py 107 3000 2x,1.5x,7000,6000,4500 10,1,5,30
        # SOL price, entries, targets, time limits in minutes. 2x = twice the
        # market cap our order landed at. The first entry, target and time
        # limit are "the idea as stated" in sections 2 and 3.
"""

import bisect
import statistics
import sys
import time
from itertools import product

import sol_backtest as bt

SOL_USD = 107.0
REACT = 1.0
ENTRIES = [8_000, 10_000]          # USD market cap that triggers the buy
TARGETS = [25_000, 30_000]         # sell at this USD market cap, or "2x" = 2 x our buy
STOPS = [0.5, 0.7]                 # sell if market cap falls to this x our buy
HOLDS = [3600, 1800]               # time limits in seconds; the first is "the idea as stated"
SLIPPAGE = 0.20                    # the "fill" filter
ENTRY_WINDOW = 3600                # the entry must come in the first hour
SUPPLY = 1_000_000_000             # tokens (6 decimals on chain)
INF = float("inf")
FILTERS = {
    "speed": ("under 30 s", "30 s or more"),
    "buyers": ("under 15", "15+"),
    "dev": ("holds", "sold"),
    "top 10": ("under 30%", "30%+"),
    "fill": (f"under +{SLIPPAGE:.0%}", f"+{SLIPPAGE:.0%} or more"),
}


def mcap(vq: float, vt: float) -> float:
    """USD market cap from the curve reserves (lamports, raw token units)."""
    return vq / vt * 1e6 * SOL_USD     # price in SOL per token x 1B tokens


def top10_share(balances: dict) -> float:
    held = sorted((b for b in balances.values() if b > 0), reverse=True)[:10]
    return sum(held) / (SUPPLY * 1e6)


def scan(c: bt.Coin, mc: list, entry_usd: float):
    """First crossing of entry_usd in the first hour: (index, features) or None."""
    balances, limit = {}, c.t0 + ENTRY_WINDOW
    prev_vt = c.vt0
    for k, t in enumerate(c.ts):
        if t > limit:
            return None
        # tokens moved by this trade = change in the curve's token reserve
        # (every normal-coin trade starts where the previous one ended)
        moved = abs(prev_vt - c.vt[k])
        prev_vt = c.vt[k]
        user = c.users[k]
        balances[user] = balances.get(user, 0) + (moved if c.is_buy[k] else -moved)
        if mc[k] >= entry_usd:
            return k, {
                "speed": "under 30 s" if t - c.t0 < 30 else "30 s or more",
                "buyers": "under 15" if c.buyers[k] < 15 else "15+",
                "dev": "sold" if c.devsold[k] else "holds",
                "top 10": "under 30%" if top10_share(balances) < 0.30 else "30%+",
            }
    return None


def buy(c: bt.Coin, k: int):
    """Our order, REACT s after seeing trade k: (fill time, index of the last
    trade before it, tokens bought, market cap it landed at), or None."""
    fill = c.ts[k] + REACT
    if c.grad <= fill:
        return None                    # graduated before the order landed
    j = bisect.bisect_left(c.ts, fill) - 1
    vq, vt, fee = c.state(j)
    net_in = bt.SIZE / (1 + fee)
    return fill, j, vt * net_in / (vq + net_in), mcap(vq, vt)


def first_time(c: bt.Coin, mc: list, j: int, until: float, test) -> float:
    """Time of the first trade after index j, up to `until`, whose market cap
    passes `test`; INF if none."""
    for i in range(j + 1, len(c.ts)):
        if c.ts[i] > until:
            break
        if test(mc[i]):
            return c.ts[i]
    return INF


def usd_label(value: float) -> str:
    return f"${value / 1000:g}k"


def target_label(target) -> str:
    return target if isinstance(target, str) else usd_label(target)


def usd_list(text: str) -> list:
    return [int(float(x)) for x in text.split(",") if x.strip()]


def target_list(text: str) -> list:
    """'2x,1.5x,6000' -> ['2x', '1.5x', 6000]: x = multiple of our buy price."""
    out = []
    for x in text.split(","):
        x = x.strip().lower()
        if x:
            out.append(f"{float(x[:-1]):g}x" if x.endswith("x") else int(float(x)))
    return out


def main() -> None:
    global SOL_USD, ENTRIES, TARGETS, HOLDS
    if len(sys.argv) > 1:
        SOL_USD = float(sys.argv[1])
    if len(sys.argv) > 2:
        ENTRIES = usd_list(sys.argv[2])
    if len(sys.argv) > 3:
        TARGETS = target_list(sys.argv[3])
    if len(sys.argv) > 4:
        HOLDS = [int(float(m) * 60) for m in sys.argv[4].split(",") if m.strip()]
    horizon = ENTRY_WINDOW + max(HOLDS) + 2 * REACT + 10
    fixed = sorted(t for t in TARGETS if not isinstance(t, str))
    started = time.time()
    coins, total, n_trades, data_end = bt.load(horizon=horizon, details=True)
    if len(coins) < 200:
        sys.exit(f"Only {len(coins)} usable coins - let sol_bot.py collect more data.")
    split = coins[len(coins) // 2].t0
    launch_usd = mcap(coins[0].vq0, coins[0].vt0)
    print(f"\n{len(coins)} coins with a full history; SOL at ${SOL_USD:g}, so a coin "
          f"launches at about ${launch_usd:,.0f} market cap")

    # ------------------------------------------------------------- funnel
    levels = sorted(set(ENTRIES + fixed))
    peak = []
    for c in coins:
        limit = c.t0 + horizon
        peak.append(max((mcap(q, v) for q, v, t in zip(c.vq, c.vt, c.ts) if t <= limit),
                        default=launch_usd))
    print("\n1. HOW FAR COINS GET (peak market cap in their recorded history)\n")
    for lv in levels:
        n = sum(1 for p in peak if p >= lv)
        print(f"  reached ${lv:>6,}: {n:>6} coins  ({n / len(coins) * 100:>5.1f}%)")
    for lo, hi in product(ENTRIES, fixed):
        a = sum(1 for p in peak if p >= lo)
        b = sum(1 for p in peak if p >= hi)
        if a and hi > lo:
            print(f"  of the coins that reached ${lo:,}, {b / a * 100:.1f}% went on to ${hi:,}")

    results, landed, outcomes = simulate(coins, split)
    report(results, landed, outcomes, started)


def simulate(coins: list, split: float) -> tuple:
    """Run every entry x target x stop x hold on every coin.

    results:  (entry, target, stop, hold, filter, value) -> (1st half, 2nd half) nets
    landed:   entry -> [(market cap the bot saw, market cap our order got)]
    outcomes: target -> [(net, why we sold)] for "the idea as stated"
    """
    results, outcomes = {}, {}
    landed = {e: [] for e in ENTRIES}
    for c in coins:
        half = 0 if c.t0 < split else 1
        mc = [mcap(q, t) for q, t in zip(c.vq, c.vt)]
        for entry_usd in ENTRIES:
            hit = scan(c, mc, entry_usd)
            if hit is None:
                continue
            k, feats = hit
            order = buy(c, k)
            if order is None:
                continue
            fill, j, tokens, entry_mc = order
            landed[entry_usd].append((mc[k], entry_mc))
            jumped = entry_mc >= (1 + SLIPPAGE) * mc[k]
            feats["fill"] = FILTERS["fill"][1] if jumped else FILTERS["fill"][0]
            groups = [("all coins", "")] + [(name, feats[name]) for name in FILTERS]
            # first time each target and each stop is reached, up to the longest hold
            until = fill + max(HOLDS)
            t_goal = {}
            for target in TARGETS:
                # "2x" = twice the market cap our order landed at; 6000 = $6k market cap
                goal = float(target[:-1]) * entry_mc if isinstance(target, str) else target
                t_goal[target] = first_time(c, mc, j, until, lambda m, g=goal: m >= g)
            t_stop = {s: first_time(c, mc, j, until, lambda m, f=s * entry_mc: m <= f)
                      for s in STOPS}
            for target, stop, hold in product(TARGETS, STOPS, HOLDS):
                end = fill + hold
                exit_at, why = end, "time"
                if t_goal[target] <= end or t_stop[stop] <= end:
                    if t_goal[target] <= t_stop[stop]:
                        exit_at, why = t_goal[target], "target"
                    else:
                        exit_at, why = t_stop[stop], "stop"
                net = bt.sell(c, exit_at + REACT, tokens)
                if entry_usd == ENTRIES[0] and stop == STOPS[0] and hold == HOLDS[0]:
                    outcomes.setdefault(target, []).append((net, why))
                for name, value in groups:
                    key = (entry_usd, target, stop, hold, name, value)
                    results.setdefault(key, ([], []))[half].append(net)
    return results, landed, outcomes


def report(results: dict, landed: dict, outcomes: dict, started: float) -> None:
    def avg(xs):
        return statistics.fmean(xs) * 100 if xs else float("nan")

    def pct(xs) -> str:
        return f"{avg(xs):>+6.1f}%" if xs else "      -"

    print("\n   Where our order landed (market cap 1 s after the trigger):")
    for e in ENTRIES:
        if not landed[e]:
            continue
        xs = sorted(got for _, got in landed[e])
        q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]  # noqa: E731
        jumped = sum(1 for saw, got in landed[e] if got >= (1 + SLIPPAGE) * saw)
        print(f"   trigger ${e:,}: median ${statistics.median(xs):,.0f}, a quarter of the "
              f"time above ${q(0.75):,.0f}, a tenth above ${q(0.90):,.0f}")
        print(f"   {jumped / len(xs) * 100:.0f}% of the orders landed {SLIPPAGE:.0%}+ above the "
              f"market cap the bot saw")

    stop_text = f"stop at -{(1 - STOPS[0]) * 100:.0f}%"
    idea = (f"buy at {usd_label(ENTRIES[0])}, sell at {target_label(TARGETS[0])}, "
            f"{stop_text}, max {HOLDS[0] / 60:g} min")
    base = (ENTRIES[0], TARGETS[0], STOPS[0], HOLDS[0])
    print(f"\n2. THE IDEA AS STATED: {idea}\n")
    print("  filter                     | 1st half: trades    avg | 2nd half: trades    avg | all")
    for name, value in [("all coins", "")] + [(n, v) for n, vs in FILTERS.items() for v in vs]:
        h0, h1 = results.get(base + (name, value), ([], []))
        label = name if not value else f"{name}: {value}"
        print(f"  {label:<26} | {len(h0):>15} {pct(h0)} | {len(h1):>15} {pct(h1)} "
              f"| {pct(h0 + h1)}")

    print(f"\n3. HOW OFTEN THE TARGET WAS HIT (buy at {usd_label(ENTRIES[0])}, {stop_text}, "
          f"max {HOLDS[0] / 60:g} min, all coins)\n")
    print("  sell at    trades   hit the target   avg when hit   avg otherwise   hit rate needed")
    for target in TARGETS:
        rows = outcomes.get(target, [])
        if not rows:
            continue
        won = [n * 100 for n, why in rows if why == "target"]
        lost = [n * 100 for n, why in rows if why != "target"]
        w = statistics.fmean(won) if won else None
        l = statistics.fmean(lost) if lost else None
        if w is None or l is None:
            need = "-"
        elif l >= 0:
            need = "0%"                 # even the misses made money
        elif w <= 0:
            need = "never"              # hitting the target loses money too
        else:
            need = f"{-l / (w - l) * 100:.1f}%"
        show = lambda x: "-" if x is None else f"{x:+.1f}%"  # noqa: E731
        print(f"  {target_label(target):<8} {len(rows):>8}   {len(won) / len(rows) * 100:>13.1f}%"
              f"   {show(w):>12}   {show(l):>13}   {need:>15}")
    print("\n  A target makes money only when its hit rate beats the last column:"
          "\n  hit rate x gain when hit has to pay for every miss.")

    ranked = sorted((k for k, (h0, h1) in results.items()
                     if len(h0) >= bt.MIN_PER_HALF and len(h1) >= bt.MIN_PER_HALF),
                    key=lambda k: -min(avg(results[k][0]), avg(results[k][1])))
    print(f"\n4. ALL {len(results)} RULE x FILTER COMBINATIONS - the 12 best by their WORSE half "
          f"({len(ranked)} have {bt.MIN_PER_HALF}+ trades in each half;\n"
          f"   combinations that made exactly the same trades are shown once)\n")
    print("  buy at   sell at  stop  hold  filter                     | 1st half       | 2nd half       | win")
    shown, seen = [], set()
    for key in ranked:
        h0, h1 = results[key]
        same = (len(h0), len(h1), round(sum(h0), 6), round(sum(h1), 6))
        if same not in seen:
            seen.add(same)
            shown.append(key)
        if len(shown) == 12:
            break
    for key in shown:
        entry_usd, target, stop, hold, name, value = key
        h0, h1 = results[key]
        win = sum(1 for x in h0 + h1 if x > 0) / len(h0 + h1) * 100
        label = name if not value else f"{name}: {value}"
        print(f"  {usd_label(entry_usd):<7}  {target_label(target):<7}  {stop:.1f}x  {hold // 60:>2}m   "
              f"{label:<26} | {len(h0):>5} {avg(h0):>+6.1f}% | {len(h1):>5} {avg(h1):>+6.1f}% | {win:>3.0f}%")
    passed = [k for k in ranked if min(avg(results[k][0]), avg(results[k][1])) >= bt.PASS_AVG * 100]
    print(f"\n  Combinations at +{bt.PASS_AVG * 100:.0f}% or more in both halves: {len(passed)}")
    print("  All results after fees on both sides, price impact, a 1 s order delay and gas.")
    print(f"  ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
