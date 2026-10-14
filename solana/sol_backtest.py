"""
Backtest a grid of rules on the pump.fun data that sol_bot.py recorded.

The live paper trader tested 6 rules. This replays the recorded launches and
trades (exact curve reserves from the chain) to test 2,400 rules at once:

  entry:  0 / 5 / 15 / 60 / 180 s after the bot saw the launch; the order
          lands 1 s after the decision
  filter: outside buyers by then (any / 0 / 1-4 / 5+)
          x dev (any / has not sold)
  exit:   take profit 1.2 / 1.5 / 2 / 3x or none, stop loss 0.5 / 0.7x or
          none, time limit 1 / 5 / 10 / 60 min, or graduation

Every result is priced exactly like the paper trader: the coin's own fees on
both sides, our own price impact, the order delay and gas. The first table
replays the paper trader's six rules; its numbers should match
sol_report.py, which shows the replay is faithful.

The pass rule is the one set before the test: an average of +3% or more per
trade in BOTH halves (coins split by launch time), with 100+ trades in each
half. With 2,400 rules a pass is only a candidate - it has to pass again on
data recorded later.

Coins launched too close to a stream gap or to the end of the data are left
out, because part of their price history is missing.

Read-only analysis of local files. Safe to run while sol_bot.py is running.

    python sol_backtest.py
"""

import bisect
import csv
import math
import statistics
import sys
import time

LAUNCH_FILE = "sol_launches.csv"
TRADE_FILE = "sol_trades.csv"
GRAD_FILE = "sol_graduations.csv"
GAP_FILE = "sol_gaps.csv"
PAPER_FILE = "sol_paper_trades.csv"

# the same costs as sol_bot.py
SIZE = 0.05e9               # lamports per trade, fees included
GAS = 0.0002e9              # per transaction
MIN_BUY = 0.001e9           # smaller buys do not count as a buyer
DEFAULT_FEE = 0.0125

REACT = 1.0                 # seconds from decision to the order landing
ENTRIES = [0, 5, 15, 60, 180]
BUYERS = [("any buyers", 0, None), ("0 buyers", 0, 0),
          ("1-4 buyers", 1, 4), ("5+ buyers", 5, None)]
DEVS = ["dev any", "dev held"]
TPS = [1.2, 1.5, 2.0, 3.0, None]
SLS = [0.5, 0.7, None]
HOLDS = [60, 300, 600, 3600]
PASS_AVG = 0.03
MIN_PER_HALF = 100
TOP = 15
INF = float("inf")
HORIZON = max(ENTRIES) + max(HOLDS) + 10      # seconds of history a coin needs

# sol_bot.py's own rules, replayed as a check:
#   name, entry after, react, min buyers, max buyers, tp, sl, hold
LIVE = [
    ("launch   1.2x 10m fast", 0, 0.5, 0, None, 1.2, 0.5, 600),
    ("launch   1.2x 10m slow", 0, 2.0, 0, None, 1.2, 0.5, 600),
    ("5s 0 buy 1.2x 10m", 5, 1.0, 0, 0, 1.2, 0.5, 600),
    ("5s 0 buy 1.5x 60m", 5, 1.0, 0, 0, 1.5, 0.5, 3600),
    ("5s any   2x/half 60m", 5, 1.0, 0, None, 2.0, 0.5, 3600),
    ("60s 10+  1.3x 10m", 60, 1.0, 10, None, 1.3, 0.5, 600),
]


# ------------------------------------------------------------------- data
class Coin:
    __slots__ = ("mint", "t0", "dev", "vq0", "vt0", "fee0", "grad", "ts", "vq",
                 "vt", "fee", "buyers", "devsold", "seen", "sold",
                 "users", "is_buy", "sol", "sells")

    def __init__(self, mint, t0, dev, vq0, vt0, fee0):
        self.mint, self.t0, self.dev = mint, t0, dev
        self.vq0, self.vt0, self.fee0 = vq0, vt0, fee0
        self.grad = INF
        self.ts, self.vq, self.vt, self.fee = [], [], [], []
        self.buyers, self.devsold = [], []
        self.seen, self.sold = set(), False
        self.users, self.is_buy, self.sol = [], [], []    # kept only with details
        self.sells = {}                                  # wallet -> sell times

    def state(self, k: int) -> tuple:
        """Reserves and fee rate after trade k (k = -1: before any trade)."""
        if k < 0:
            return self.vq0, self.vt0, self.fee0
        return self.vq[k], self.vt[k], self.fee[k]


def rows(path: str, required: bool = True):
    """(column index, row) for each complete row of a CSV file."""
    try:
        f = open(path, newline="", encoding="utf-8")
    except FileNotFoundError:
        if required:
            sys.exit(f"{path} not found - run this in the folder where sol_bot.py runs")
        return
    with f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return
        ix = {name: i for i, name in enumerate(header)}
        for row in reader:
            if len(row) == len(header):     # a half-written last line is skipped
                yield ix, row


def stamp_to_epoch(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def load(horizon: float = HORIZON, details: bool = False) -> tuple:
    """(usable coins sorted by launch, launches, trades, end of data).

    A coin is usable when `horizon` seconds after its launch were recorded
    without a stream gap. details=True also keeps who traded (wallet test).
    """
    coins = {}
    for ix, r in rows(LAUNCH_FILE):
        try:
            if r[ix["mayhem"]] == "True":
                continue                     # mayhem coins are not traded
            cf = int(r[ix["creator_fee_bps"]] or 0)
            c = Coin(r[ix["mint"]], float(r[ix["seen_at"]]), r[ix["dev"]],
                     int(r[ix["virtual_sol"]]), int(r[ix["virtual_tokens"]]),
                     (95 + cf) / 10_000 if cf else DEFAULT_FEE)
        except (KeyError, ValueError):
            continue
        coins[c.mint] = c
    if not coins:
        sys.exit("No launches recorded yet.")
    data_end = max(c.t0 for c in coins.values())
    n = 0
    for ix, r in rows(TRADE_FILE):
        c = coins.get(r[ix["mint"]])
        if c is None:
            continue
        try:
            t = float(r[ix["seen_at"]])
            sol = int(r[ix["sol"]])
            buy = r[ix["side"]] == "buy"
            user = r[ix["user"]]
            vq, vt = int(r[ix["virtual_sol"]]), int(r[ix["virtual_tokens"]])
            creator = int(r[ix["creator_fee_bps"]]) if int(r[ix["creator_fee"]] or 0) > 0 else 0
            fee = (int(r[ix["fee_bps"]]) + creator) / 10_000
        except (KeyError, ValueError):
            continue
        if vq <= 0 or vt <= 0:
            continue
        if sol >= MIN_BUY:                   # the same bookkeeping as the bot
            if buy and user != c.dev:
                c.seen.add(user)
            elif not buy and user == c.dev:
                c.sold = True
        c.ts.append(t)
        c.vq.append(vq)
        c.vt.append(vt)
        c.fee.append(fee)
        c.buyers.append(len(c.seen))
        c.devsold.append(c.sold)
        if details:
            user = sys.intern(user)
            c.users.append(user)
            c.is_buy.append(buy)
            c.sol.append(sol)
            if not buy:
                c.sells.setdefault(user, []).append(t)
        data_end = max(data_end, t)
        n += 1
    for ix, r in rows(GRAD_FILE, required=False):
        c = coins.get(r[ix["mint"]])
        if c is not None:
            try:
                c.grad = min(c.grad, float(r[ix["seen_at"]]))
            except ValueError:
                pass
    gaps = []
    for ix, r in rows(GAP_FILE, required=False):
        try:
            gaps.append((stamp_to_epoch(r[ix["start"]]), stamp_to_epoch(r[ix["end"]])))
        except (KeyError, ValueError):
            continue
    for c in coins.values():
        c.seen = None                        # free memory
    usable = [c for c in coins.values()
              if c.t0 + horizon <= data_end
              and not any(a < c.t0 + horizon and b > c.t0 for a, b in gaps)]
    usable.sort(key=lambda c: c.t0)
    return usable, len(coins), n, data_end


# -------------------------------------------------------------- simulation
def entry(c: Coin, after: float, react: float):
    """Our buy: (fill time, state index, buyers, dev sold, quote in, tokens)."""
    decide = c.t0 + after
    fill = decide + react
    if c.grad <= fill:
        return None                          # graduated before the order landed
    i = bisect.bisect_left(c.ts, decide) - 1
    buyers = c.buyers[i] if i >= 0 else 0
    dev_sold = c.devsold[i] if i >= 0 else False
    j = bisect.bisect_left(c.ts, fill) - 1
    vq, vt, fee = c.state(j)
    net_in = SIZE / (1 + fee)
    return fill, j, buyers, dev_sold, net_in, vt * net_in / (vq + net_in)


def crossings(c: Coin, fill: float, j: int, net_in: float, tokens: float,
              until: float, tps: list, sls: list) -> tuple:
    """First time the position is worth each multiple in tps (rising) or sls
    (falling), counting trades after our fill, up to `until`."""
    tps = sorted(tps)
    sls = sorted(sls, reverse=True)
    hit_tp, hit_sl = {}, {}
    a = b = 0
    ts, vq, vt = c.ts, c.vq, c.vt
    k, n = j + 1, len(ts)
    while k < n and ts[k] <= until and (a < len(tps) or b < len(sls)):
        m = vq[k] * tokens / (vt[k] + tokens) / net_in
        while a < len(tps) and m >= tps[a]:
            hit_tp[tps[a]] = ts[k]
            a += 1
        while b < len(sls) and m <= sls[b]:
            hit_sl[sls[b]] = ts[k]
            b += 1
        k += 1
    return hit_tp, hit_sl


def sell(c: Coin, at: float, tokens: float) -> float:
    """Net result (fraction of SIZE) when our sell lands at time `at`, or at
    graduation if the curve completes first."""
    if c.grad <= at:
        k = bisect.bisect_right(c.ts, c.grad) - 1
    else:
        k = bisect.bisect_left(c.ts, at) - 1
    vq, vt, fee = c.state(k)
    gross = vq * tokens / (vt + tokens)
    return (gross * (1 - fee) - SIZE - 2 * GAS) / SIZE


def exit_time(fill, hit_tp, hit_sl, tp, sl, hold) -> float:
    return min(hit_tp.get(tp, INF) if tp else INF,
               hit_sl.get(sl, INF) if sl else INF,
               fill + hold)


def run_rule(coins, after, react, lo, hi, dev_held, tp, sl, hold) -> list:
    """(launch time, net) for one rule over all coins."""
    out = []
    for c in coins:
        e = entry(c, after, react)
        if e is None:
            continue
        fill, j, buyers, dev_sold, net_in, tokens = e
        if buyers < lo or (hi is not None and buyers > hi) or (dev_held and dev_sold):
            continue
        hit_tp, hit_sl = crossings(c, fill, j, net_in, tokens, fill + hold,
                                   [tp] if tp else [], [sl] if sl else [])
        t = exit_time(fill, hit_tp, hit_sl, tp, sl, hold)
        out.append((c.t0, sell(c, t + react, tokens)))
    return out


# ------------------------------------------------------------------ stats
class Acc:
    """Running sums for one rule, split by half."""
    __slots__ = ("n", "s", "q", "w", "best")

    def __init__(self):
        self.n, self.s, self.q, self.w = [0, 0], [0.0, 0.0], [0.0, 0.0], [0, 0]
        self.best = -INF

    def add(self, half: int, x: float) -> None:
        self.n[half] += 1
        self.s[half] += x
        self.q[half] += x * x
        self.w[half] += x > 0
        if x > self.best:
            self.best = x

    def avg(self, half=None) -> float:
        if half is None:
            n = sum(self.n)
            return sum(self.s) / n if n else 0.0
        return self.s[half] / self.n[half] if self.n[half] else 0.0

    def ci(self) -> tuple:
        n = sum(self.n)
        m = self.avg()
        var = (sum(self.q) - n * m * m) / (n - 1) if n > 1 else 0.0
        se = math.sqrt(max(var, 0.0) / n) if n else 0.0
        return m - 1.96 * se, m + 1.96 * se


def describe(key) -> str:
    after, buyers, dev, tp, sl, hold = key
    exit_ = (f"{tp:g}x" if tp else "no tp") + " / " + (f"{sl:g}x" if sl else "no sl")
    return (f"{after:>3}s  {buyers:<10} {dev:<8}  {exit_:<15} "
            f"{hold // 60 if hold >= 60 else hold:>2}{'m' if hold >= 60 else 's'}")


def paper_averages(mints: set) -> dict:
    out = {}
    for ix, r in rows(PAPER_FILE, required=False):
        try:
            if r[ix["gap"]] == "True" or r[ix["mint"]] not in mints:
                continue
            out.setdefault(r[ix["strategy"]], []).append(float(r[ix["net_pct"]]) / 100)
        except (KeyError, ValueError):
            continue
    return {k: (len(v), statistics.fmean(v)) for k, v in out.items()}


# -------------------------------------------------------------------- run
def main() -> None:
    started = time.time()
    coins, total, n_trades, data_end = load()
    if len(coins) < 200:
        sys.exit(f"Only {len(coins)} usable coins - let sol_bot.py collect more data.")
    split = coins[len(coins) // 2].t0
    fmt = lambda t: time.strftime("%d %b %H:%M", time.localtime(t))  # noqa: E731
    print(f"\n{total} launches and {n_trades} trades loaded; {len(coins)} coins have a "
          f"full price history ({fmt(coins[0].t0)} to {fmt(coins[-1].t0)}), "
          f"split at {fmt(split)}")

    # ---------------------------------------------------------------- 1
    print("\n1. CHECK: THE PAPER TRADER'S OWN RULES, REPLAYED\n")
    print("  rule                      replay: trades     avg  |  paper trades: trades     avg")
    paper = paper_averages({c.mint for c in coins})
    for name, after, react, lo, hi, tp, sl, hold in LIVE:
        res = run_rule(coins, after, react, lo, hi, after > 0, tp, sl, hold)
        nets = [x for _, x in res]
        p = paper.get(name)
        print(f"  {name:<24} {len(nets):>14}  {statistics.fmean(nets) * 100 if nets else 0:>+6.2f}%"
              f"  |  {p[0] if p else 0:>20}  "
              + (f"{p[1] * 100:>+6.2f}%" if p else "     -"))
    print("  (small differences: the paper trader only counts trades closed so far)")

    # ---------------------------------------------------------------- 2
    accs = {}
    tps_all = [tp for tp in TPS if tp]
    sls_all = [sl for sl in SLS if sl]
    for done, c in enumerate(coins):
        half = 0 if c.t0 < split else 1
        for after in ENTRIES:
            e = entry(c, after, REACT)
            if e is None:
                continue
            fill, j, buyers, dev_sold, net_in, tokens = e
            groups = [(name, dev) for name, lo, hi in BUYERS
                      if buyers >= lo and (hi is None or buyers <= hi)
                      for dev in DEVS if dev == "dev any" or not dev_sold]
            hit_tp, hit_sl = crossings(c, fill, j, net_in, tokens,
                                       fill + max(HOLDS), tps_all, sls_all)
            for tp in TPS:
                for sl in SLS:
                    for hold in HOLDS:
                        t = exit_time(fill, hit_tp, hit_sl, tp, sl, hold)
                        net = sell(c, t + REACT, tokens)
                        for name, dev in groups:
                            key = (after, name, dev, tp, sl, hold)
                            acc = accs.get(key)
                            if acc is None:
                                acc = accs[key] = Acc()
                            acc.add(half, net)
        if done and done % 5000 == 0:
            print(f"  ... {done}/{len(coins)} coins")
    ranked = sorted((k for k, a in accs.items() if min(a.n) >= MIN_PER_HALF),
                    key=lambda k: -min(accs[k].avg(0), accs[k].avg(1)))
    grid = len(ENTRIES) * len(BUYERS) * len(DEVS) * len(TPS) * len(SLS) * len(HOLDS)
    print(f"\n2. ALL {grid} RULES - the {TOP} best, ranked by their WORSE half\n"
          f"   ({len(ranked)} rules have {MIN_PER_HALF}+ trades in each half; rules that "
          f"made exactly the same trades are shown once)\n")
    print("  entry buyers     dev       tp / sl         hold |  1st half    2nd half  |"
          "   all    95% range       w/o best  win")
    shown, seen = [], set()
    for key in ranked:
        a = accs[key]
        same = (tuple(a.n), round(a.s[0], 6), round(a.s[1], 6))
        if same in seen:
            continue
        seen.add(same)
        shown.append(key)
        if len(shown) == TOP:
            break
    for key in shown:
        a = accs[key]
        lo, hi = a.ci()
        n = sum(a.n)
        nobest = (sum(a.s) - a.best) / (n - 1) if n > 1 else a.avg()
        print(f"  {describe(key)} | {a.n[0]:>5} {a.avg(0) * 100:>+5.1f}%  {a.n[1]:>5} "
              f"{a.avg(1) * 100:>+5.1f}% | {a.avg() * 100:>+5.1f}%  [{lo * 100:>+5.1f}, "
              f"{hi * 100:>+5.1f}]   {nobest * 100:>+5.1f}%  {sum(a.w) / n * 100:>3.0f}%")

    both_pos = [k for k in ranked if accs[k].avg(0) > 0 and accs[k].avg(1) > 0]
    passed = [k for k in ranked if min(accs[k].avg(0), accs[k].avg(1)) >= PASS_AVG]
    print(f"\n  Rules above zero in both halves: {len(both_pos)} of {len(ranked)}")
    print(f"  Rules at +{PASS_AVG * 100:.0f}% or more in both halves (the pass rule): {len(passed)}")

    # ---------------------------------------------------------------- 3
    print("\n3. BEST RULE FOR EACH ENTRY TIME (worse half)\n")
    for after in ENTRIES:
        keys = [k for k in ranked if k[0] == after]
        if keys:
            a = accs[keys[0]]
            print(f"  {describe(keys[0])}   worse half {min(a.avg(0), a.avg(1)) * 100:>+5.1f}%"
                  f"   ({sum(a.n)} trades)")

    # ---------------------------------------------------------------- 4
    print("\n4. SPEED CHECK - the 3 best rules with a faster and a slower order\n")
    print("  rule                                             0.5 s     1 s       2 s")
    for key in shown[:3]:
        after, bname, dev, tp, sl, hold = key
        lo, hi = next((lo, hi) for name, lo, hi in BUYERS if name == bname)
        cells = []
        for react in (0.5, 1.0, 2.0):
            res = run_rule(coins, after, react, lo, hi, dev == "dev held", tp, sl, hold)
            cells.append(f"{statistics.fmean(x for _, x in res) * 100:>+6.1f}%" if res else "   -")
        print(f"  {describe(key)}  " + "  ".join(cells))

    print(f"\n  All results after fees on both sides, price impact, a {REACT:g} s order "
          f"delay and gas.")
    if passed:
        print("  A rule passed on this data. With this many rules that can be luck:")
        print("  it only counts if it passes again on data recorded later.")
    else:
        print("  No rule passed.")
    print(f"  ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
