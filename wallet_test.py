"""
Wallet test: do some wallets keep picking winners, and would following them pay?

The share cards suggest watching WHO buys, not just what is bought. If some
wallets are consistently early into tokens that then rise, a bot could buy
right after them.

How it is tested, on the watcher's data and the exact Pons curve
(constant product, 1.68 ETH x 1B tokens, fitted by check_rule.py):

  1. Launches are split by time. The first half chooses the wallets, the
     second half tests them. The test half is never used to choose.
  2. For each wallet's first buy of a token (devs buying their own launch
     and dust buys excluded), a copy is simulated: buy REACT seconds after
     that buy at the curve's real price, then sell either
       A) at 1.2x / -50% / 10 min, or
       B) when that wallet sells (at most 60 min later).
  3. On the first half, each wallet with at least MIN_TOKENS tokens is
     scored by the average result of copying it (minus one standard error,
     so a lucky wallet with few trades does not rank first).
  4. On the second half: follow the top 10 / 25 / 50 wallets and compare
     with following every wallet.

Results are after the 1% curve fee, the token's own fee and slippage on
both sides. Gas (~0.5% of a 0.002 ETH trade) is NOT included - subtract it.
ETH-paired tokens only.

Read-only analysis of local files.

    python wallet_test.py
"""

import bisect
import csv
import statistics
import sys
from collections import defaultdict

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"

X0 = 1.68e18                 # virtual ETH reserve at launch (wei)
Y0 = 1.0e27                  # virtual token reserve (1B tokens)
K = X0 * Y0
SIZE = 0.002e18              # our copy size, wei
FEE_A = 0.01
SLIP = 0.005                 # extra per side, beyond our modelled impact
REACT = 2                    # seconds between their buy and our copy
MIN_BUY = 10 ** 15           # their buy must be at least 0.001 ETH
MIN_TOKENS = 10              # wallet needs this many tokens in the first half
TP, SL, HOLD = 1.2, 0.5, 600
MIRROR_MAX = 3600
TOPS = [10, 25, 50]


def read(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"{path} not found - run this in the meme-bot folder")


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def buy_price(x):
    return x * x / K * (1 + SIZE / x)


def sell_price(x):
    x = max(x, 1.0)
    return x * x / K * (1 - SIZE / x)


# ------------------------------------------------------------------ load
def load():
    tokens = {}
    for row in read(LAUNCH_FILE):
        if row["data0"] not in ("0", ""):
            continue                                  # ETH-paired only
        dev = row["topic3"].lower() if row["topic3_is_contract"] == "False" else ""
        tokens[row["topic2"].lower()] = {
            "launch": int(row["est_time"]), "launch_tx": row["tx"], "dev": dev,
            "trades": []}
    seen = set()
    for row in read(TRADE_FILE):
        key = (row["tx"], row["log_index"])
        if key in seen:
            continue
        seen.add(key)
        info = tokens.get(row["curve"].lower())
        if info is None:
            continue
        d = [to_int(row[f"data{i}"]) for i in range(4)]
        if row["side"] == "buy":
            gross, flow = d[0], d[0] - d[2] - d[3]
        elif row["side"] == "sell":
            gross = d[1] + d[2] + d[3]
            flow = -gross
        else:
            continue
        if gross == 0:
            continue
        who = row["topic2"].lower()
        if not info["dev"] and row["tx"] == info["launch_tx"] and row["side"] == "buy":
            info["dev"] = who
        info["trades"].append((int(row["block"]), int(row["log_index"]),
                               int(row["est_time"]), row["side"], who, flow,
                               d[3] / gross))
    out = {}
    for curve, info in tokens.items():
        if not info["trades"]:
            continue
        info["trades"].sort()
        x, xs, ts = X0, [], []
        for tr in info["trades"]:
            x += tr[5]
            xs.append(x)
            ts.append(tr[2])
        if max(xs) > X0 + 4.2e18 * 1.05 or min(xs) < X0 * 0.5:
            continue                                  # not a normal ETH curve
        info["xs"], info["ts"] = xs, ts
        info["fee"] = statistics.median(tr[6] for tr in info["trades"])
        sells = defaultdict(list)
        for tr in info["trades"]:
            if tr[3] == "sell":
                sells[tr[4]].append(tr[2])
        info["sells"] = sells
        out[curve] = info
    return out


# -------------------------------------------------------------- simulate
def state_at(info, t):
    """Index of the last trade at or before time t."""
    return bisect.bisect_right(info["ts"], t) - 1


def copy_trade(info, i, who):
    """Copy the buy at trade index i. Returns (net A, net B, delay since launch)."""
    t_buy = info["trades"][i][2]
    f = max(state_at(info, t_buy + REACT), i)
    entry = buy_price(info["xs"][f])
    cost = FEE_A + info["fee"] + SLIP
    trades, xs, n = info["trades"], info["xs"], len(info["trades"])

    # A) take profit / stop loss / time limit
    deadline = t_buy + REACT + HOLD
    j, mult_a = f, None
    while j + 1 < n and trades[j + 1][2] <= deadline:
        j += 1
        r = sell_price(xs[j]) / entry
        if r >= TP or r <= SL:
            m = state_at(info, trades[j][2] + REACT)
            mult_a = sell_price(xs[m]) / entry
            break
    if mult_a is None:
        mult_a = sell_price(xs[j]) / entry

    # B) sell when they sell (at most MIRROR_MAX later)
    their = info["sells"].get(who, [])
    k = bisect.bisect_right(their, t_buy)
    t_out = their[k] if k < len(their) and their[k] <= t_buy + MIRROR_MAX \
        else t_buy + MIRROR_MAX
    m = max(state_at(info, t_out + REACT), f)
    mult_b = sell_price(xs[m]) / entry

    net = lambda mult: mult * (1 - cost) ** 2 - 1
    return net(mult_a), net(mult_b), t_buy - info["launch"]


def summary(xs):
    n = len(xs)
    if n == 0:
        return "     0        -                  -          -      -"
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / n ** 0.5 if n > 1 else 0.0
    nb = (sum(xs) - max(xs)) / (n - 1) if n > 1 else m
    win = sum(1 for x in xs if x > 0) / n * 100
    return (f"{n:>6}   {m * 100:>+6.2f}%   [{(m - 1.96 * se) * 100:>+5.1f}, "
            f"{(m + 1.96 * se) * 100:>+5.1f}]   {nb * 100:>+6.2f}%   {win:>3.0f}%")


# ------------------------------------------------------------------- run
def main():
    tokens = load()
    if not tokens:
        sys.exit("No ETH-paired traded launches found.")
    launches = sorted(i["launch"] for i in tokens.values())
    split = launches[len(launches) // 2]
    data_end = max(i["ts"][-1] for i in tokens.values())

    # copies of every wallet's first real buy of each token
    copies = []                  # (wallet, curve, launch, net_a, net_b, delay)
    for curve, info in tokens.items():
        if info["launch"] + REACT + MIRROR_MAX > data_end:
            continue                              # outcome not known yet
        done = set()
        for i, tr in enumerate(info["trades"]):
            _, _, _, side, who, flow, _ = tr
            if side != "buy" or flow < MIN_BUY or who == info["dev"] or who in done:
                continue
            done.add(who)
            a, b, delay = copy_trade(info, i, who)
            copies.append((who, curve, info["launch"], a, b, delay, tr[2]))

    train = [c for c in copies if c[2] < split]
    test = [c for c in copies if c[2] >= split]
    print(f"\n{len(tokens)} ETH-paired tokens, {len(copies)} copyable buys "
          f"({len(train)} in the first half, {len(test)} in the second)")

    by_wallet = defaultdict(list)
    for c in train:
        by_wallet[c[0]].append(c)
    scored = []
    for w, cs in by_wallet.items():
        if len(cs) < MIN_TOKENS:
            continue
        nets = [c[3] for c in cs]
        m = statistics.fmean(nets)
        se = statistics.stdev(nets) / len(nets) ** 0.5
        scored.append((m - se, m, len(cs), w))
    scored.sort(reverse=True)
    print(f"wallets with at least {MIN_TOKENS} tokens in the first half: {len(scored)}")
    if not scored:
        print("Not enough data yet - run again in a day or two.")
        return

    def follow(wallets, exit_b=False):
        """First buy per token among the chosen wallets, in the test half."""
        first = {}
        for c in sorted(test, key=lambda c: c[6]):
            if c[0] in wallets and c[1] not in first:
                first[c[1]] = c[4] if exit_b else c[3]
        return list(first.values())

    for label, exit_b in (("A) sell at 1.2x / -50% / 10 min", False),
                          ("B) sell when the wallet sells (max 60 min)", True)):
        print(f"\nCOPY RESULTS ON THE SECOND HALF - exit {label}\n")
        print("  follow                        tokens     avg     95% range       w/o best   win")
        print("  " + "-" * 82)
        for k in TOPS:
            top = {w for *_, w in scored[:k]}
            print(f"  top {k:<3} wallets of 1st half  {summary(follow(top, exit_b))}")
        everyone = {w for *_, w in scored}
        print(f"  all {len(everyone):<4} active wallets      {summary(follow(everyone, exit_b))}")
        print(f"  every buy, any wallet        "
              f"{summary([c[4] if exit_b else c[3] for c in test])}")

    print("\nDO THE BEST WALLETS STAY GOOD? (exit A)\n")
    print("  wallet           1st half: tokens   avg    |  2nd half: tokens   avg   "
          "| typical buy, s after launch")
    test_by = defaultdict(list)
    for c in test:
        test_by[c[0]].append(c)
    for _, m, n, w in scored[:10]:
        t_nets = [c[3] for c in test_by.get(w, [])]
        t_avg = f"{statistics.fmean(t_nets) * 100:>+6.1f}%" if t_nets else "      -"
        delays = [c[5] for c in by_wallet[w]]
        print(f"  {w[:8]}...{w[-4:]}  {n:>16}  {m * 100:>+6.1f}%   |  {len(t_nets):>15}  "
              f"{t_avg}  | {statistics.median(delays):>8.0f}")

    print("\n  A wallet signal is real only if the top wallets beat 'every buy'")
    print("  on the second half, by more than the 95% range, and stay above zero")
    print("  after subtracting ~0.5% gas.")


if __name__ == "__main__":
    main()
