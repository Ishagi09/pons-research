"""
Backtest simple Pons trading rules on the watcher's data, with real fees.

analyze_pons.py showed that ~90% of entries neither double nor halve:
the token simply stops trading. So win% says little. What matters is the
money left after fees. This replays simple rules on every recorded token:

  enter   at a fixed time after launch (5 s, 30 s, 2 min, 5 min), filled
          at the curve price a few seconds later (reaction time)
  filter  optional conditions known at that moment: outside buyers so
          far, whether the dev has already sold, the token's own fee
  exit    take profit (1.2x ... 3x / none), stop loss (-30% / -50% /
          none), or a time limit (10 / 60 min). If trading has stopped,
          the sell goes to the curve at its last price - the curve
          always buys back, so there is no "cannot exit" case
  costs   1% + the token's second fee as seen on its trades so far,
          plus slippage, on both the buy and the sell

Version 2 - stricter verdict. Version 1 ranked rules on the first half
and checked the second half only, so rules that lost on the first half
and won on the second were called profitable. Now a rule must make
money in BOTH halves, and the best ones are also shown per quarter and
under different slippage. Anything picked here still has to prove
itself in paper trading on fresh data.

Read-only analysis of local files.

    python backtest_pons.py
"""

import csv
import statistics
import sys
from collections import Counter, defaultdict

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"

MIN_AMOUNT = 10 ** 13          # ignore dust trades for pricing
FEE_A = 0.01                   # first fee, constant 1% in the data
SLIPPAGE = 0.01                # own price impact + bad timing, per side
REACT_SECONDS = 2              # delay between a decision and the fill
MIN_N = 30                     # ignore rules with fewer trades in a half
LOW_FEE = 0.01                 # "low fee" filter: second fee at most 1%

DELAYS = [5, 30, 120, 300]                 # entry, seconds after launch
TAKE_PROFITS = [1.2, 1.3, 1.5, 2.0, 3.0, None]
STOP_LOSSES = [0.7, 0.5, None]             # exit price / entry price
HOLDS = [600, 3600]                        # time limit, seconds
SLIPPAGE_CHECK = [0.0, 0.005, 0.01, 0.02]

AGE_EDGES = [10, 60, 300, 1800]            # fee table buckets, seconds
AGE_LABELS = ["0-10 s", "10-60 s", "1-5 min", "5-30 min", "30+ min"]

FILTERS = [
    ("all", lambda s: True),
    ("dev held", lambda s: s["dev_known"] and not s["dev_sold"]),
    ("3+ buyers", lambda s: s["buyers"] >= 3),
    ("10+ buyers", lambda s: s["buyers"] >= 10),
    ("10+ buyers, dev held", lambda s: (s["buyers"] >= 10 and s["dev_known"]
                                        and not s["dev_sold"])),
    ("low fee", lambda s: s["fee"] <= LOW_FEE),
    ("low fee, dev held", lambda s: (s["fee"] <= LOW_FEE and s["dev_known"]
                                     and not s["dev_sold"])),
]
FILTER_INDEX = {name: i for i, (name, _) in enumerate(FILTERS)}

# rules fixed in advance, shown regardless of how they rank
REFERENCE = [
    (5, "all", None, None, 600),        # buy everything at launch, hold 10 min
    (120, "all", 2.0, 0.5, 3600),       # the 2x-or-half bet from analyze_pons
    (5, "dev held", 1.5, 0.5, 3600),    # best family from the first run
]


# ------------------------------------------------------------------ load
def read(path: str) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"{path} not found - run this in the meme-bot folder")


def to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load() -> dict:
    tokens = {}
    for row in read(LAUNCH_FILE):
        dev = row["topic3"].lower() if row["topic3_is_contract"] == "False" else ""
        tokens[row["topic2"].lower()] = {
            "symbol": row["symbol"] or "?",
            "launch": int(row["est_time"]),
            "launch_tx": row["tx"],
            "dev": dev,
            "data1": row.get("data1", ""),
            "trades": [],
        }

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
        side = row["side"]
        if side == "buy":
            gross, moved = d[0], d[1]
            curve_amt = gross - d[2] - d[3]
        elif side == "sell":
            moved = d[0]
            gross = d[1] + d[2] + d[3]
            curve_amt = gross
        else:
            continue
        if moved == 0 or curve_amt < MIN_AMOUNT:
            continue
        who = row["topic2"].lower()
        # router-launched tokens: the dev is whoever received the launch buy
        if not info["dev"] and row["tx"] == info["launch_tx"] and side == "buy":
            info["dev"] = who
        info["trades"].append({
            "t": int(row["est_time"]),
            "block": int(row["block"]),
            "idx": int(row["log_index"]),
            "side": side,
            "price": curve_amt / moved,
            "who": who,
            "caller": row["topic1"].lower(),
            "gross": gross,
            "fee_b": d[3] / gross if gross else 0.0,
        })

    for info in tokens.values():
        info["trades"].sort(key=lambda x: (x["block"], x["idx"]))
    return {c: i for c, i in tokens.items() if i["trades"]}


# ------------------------------------------------------------------ fees
def age_bucket(age: float) -> int:
    for i, edge in enumerate(AGE_EDGES):
        if age < edge:
            return i
    return len(AGE_EDGES)


def med_pct(xs: list) -> str:
    return f"{statistics.median(xs) * 100:5.2f}%" if xs else "   -  "


def zero_share(xs: list) -> str:
    return f"{sum(1 for x in xs if x < 0.0005) / len(xs) * 100:4.0f}%" if xs else "  - "


def fee_report(tokens: dict) -> None:
    """Find out what decides the second fee (0-3%)."""
    print("\n1. WHAT DECIDES THE SECOND FEE? (the first fee is a flat 1%)\n")

    by_age = defaultdict(list)
    for info in tokens.values():
        for tr in info["trades"]:
            by_age[age_bucket(tr["t"] - info["launch"])].append(tr["fee_b"])
    print("  a) by token age      trades   median   share at 0%")
    for b, name in enumerate(AGE_LABELS):
        xs = by_age.get(b, [])
        print(f"     {name:<16} {len(xs):>8}   {med_pct(xs)}   {zero_share(xs)}")

    # b) is the fee fixed per token?
    fixed = several = 0
    levels = Counter()
    for info in tokens.values():
        fs = [round(tr["fee_b"] * 1000) for tr in info["trades"]]   # 0.1% units
        if len(fs) >= 3:
            several += 1
            if max(fs) - min(fs) <= 1:
                fixed += 1
        levels[round(statistics.median(fs) / 10 * 2) / 2] += 1   # nearest 0.5%
    print(f"\n  b) tokens with 3+ trades whose fee never changes: "
          f"{fixed} of {several} ({fixed / several * 100 if several else 0:.0f}%)")
    print("     tokens by their usual fee: " + ",  ".join(
        f"{k:g}%: {v}" for k, v in sorted(levels.items())))

    # c) does it depend on how the trade is sent?
    direct, routed = [], []
    callers = defaultdict(list)
    for info in tokens.values():
        for tr in info["trades"]:
            if tr["caller"] == tr["who"]:
                direct.append(tr["fee_b"])
            else:
                routed.append(tr["fee_b"])
                callers[tr["caller"]].append(tr["fee_b"])
    print("\n  c) by how the trade was sent        trades   median   share at 0%")
    print(f"     wallet calls the curve itself {len(direct):>9}   "
          f"{med_pct(direct)}   {zero_share(direct)}")
    print(f"     through another contract     {len(routed):>9}   "
          f"{med_pct(routed)}   {zero_share(routed)}")
    for caller, xs in sorted(callers.items(), key=lambda kv: -len(kv[1]))[:5]:
        print(f"       {caller[:12]}...{caller[-4:]}       {len(xs):>9}   "
              f"{med_pct(xs)}   {zero_share(xs)}")

    # d) by trade size
    size_edges = [(0, 10 ** 15, "< 0.001"), (10 ** 15, 10 ** 16, "0.001-0.01"),
                  (10 ** 16, 10 ** 17, "0.01-0.1"), (10 ** 17, 10 ** 40, "> 0.1")]
    print("\n  d) by trade size (ETH)        trades   median   share at 0%")
    for lo, hi, name in size_edges:
        xs = [tr["fee_b"] for i in tokens.values() for tr in i["trades"]
              if lo <= tr["gross"] < hi]
        print(f"     {name:<20} {len(xs):>9}   {med_pct(xs)}   {zero_share(xs)}")

    # e) is it set at launch? compare with the launch event's data1
    groups = defaultdict(list)
    for info in tokens.values():
        groups[info["data1"]].append(statistics.median(tr["fee_b"] for tr in info["trades"]))
    print("\n  e) by launch field data1      tokens   median fee")
    for value, xs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
        shown = value if len(value) <= 20 else value[:17] + "..."
        print(f"     {shown:<24} {len(xs):>7}   {med_pct(xs)}")


# -------------------------------------------------------------- simulate
def entry_state(info: dict, delay: int):
    """What a bot knows at launch + delay, and where its buy would fill."""
    decide = info["launch"] + delay
    buyers, dev_sold, fill, fees = set(), False, None, []
    for i, tr in enumerate(info["trades"]):
        if tr["t"] > decide + REACT_SECONDS:
            break
        if tr["t"] <= decide:
            if tr["side"] == "buy" and tr["who"] != info["dev"]:
                buyers.add(tr["who"])
            if tr["side"] == "sell" and tr["who"] == info["dev"]:
                dev_sold = True
        fees.append(tr["fee_b"])
        fill = i
    if fill is None:
        return None
    return {"fill": fill, "fill_t": decide + REACT_SECONDS,
            "buyers": len(buyers), "dev_sold": dev_sold,
            "dev_known": bool(info["dev"]), "fee": statistics.median(fees)}


def simulate(trades: list, fill: int, fill_t: int, tp, sl, hold: int):
    """Walk the trades after entry. Returns (price multiple, reason)."""
    entry = trades[fill]["price"]
    deadline = fill_t + hold
    n, k = len(trades), fill
    while k + 1 < n and trades[k + 1]["t"] <= deadline:
        k += 1
        r = trades[k]["price"] / entry
        hit_tp = tp is not None and r >= tp
        hit_sl = sl is not None and r <= sl
        if hit_tp or hit_sl:
            # the sell lands a few seconds after the trigger is seen
            until, m = trades[k]["t"] + REACT_SECONDS, k
            while m + 1 < n and trades[m + 1]["t"] <= until:
                m += 1
            return trades[m]["price"] / entry, ("tp" if hit_tp else "sl")
    return trades[k]["price"] / entry, "time"


def summary(items: list, slip: float = SLIPPAGE) -> dict:
    """items: (multiple, reason, token fee, quarter, filter mask)."""
    nets = []
    tp_hits = 0
    for mult, why, fee, _, _ in items:
        cost = FEE_A + fee + slip
        nets.append(mult * (1 - cost) ** 2 - 1)
        tp_hits += why == "tp"
    n = len(nets)
    if n == 0:
        return {"n": 0, "mean": 0.0, "se": 0.0, "no_best": 0.0,
                "median": 0.0, "win": 0.0, "tp": 0.0}
    mean = statistics.fmean(nets)
    se = statistics.stdev(nets) / n ** 0.5 if n > 1 else 0.0
    # one lucky 12x can carry a whole rule; show the average without it
    no_best = (sum(nets) - max(nets)) / (n - 1) if n > 1 else mean
    return {"n": n, "mean": mean, "se": se, "no_best": no_best,
            "median": statistics.median(nets),
            "win": sum(1 for x in nets if x > 0) / n, "tp": tp_hits / n}


# ------------------------------------------------------------------- run
def label(rule) -> str:
    delay, filt, tp, sl, hold = rule
    entry = f"+{delay} s" if delay < 60 else f"+{delay // 60} min"
    tp_s = f"{tp:g}x" if tp else "-"
    sl_s = f"-{round((1 - sl) * 100)}%" if sl else "-"
    return f"{entry:<8}{filt:<22}{tp_s:<6}{sl_s:<6}{hold // 60:>3}m"


def pct(x: float) -> str:
    return f"{x * 100:>+6.1f}%"


def main() -> None:
    tokens = load()
    if not tokens:
        sys.exit("No traded launches found yet.")
    data_end = max(tr["t"] for i in tokens.values() for tr in i["trades"])
    launches = sorted(i["launch"] for i in tokens.values())
    cuts = [launches[len(launches) * q // 4] for q in (1, 2, 3)]
    hours = (data_end - launches[0]) / 3600
    print(f"\nData: {hours:.1f} hours, {len(tokens)} tokens with trades. "
          f"Halves and quarters are split by launch time.")

    fee_report(tokens)

    # ------------------------------------------------------- replay
    exits = [(tp, sl, hold) for tp in TAKE_PROFITS for sl in STOP_LOSSES
             for hold in HOLDS]
    cells = defaultdict(list)       # (delay, tp, sl, hold) -> results
    n_rules = len(DELAYS) * len(FILTERS) * len(exits)
    print(f"\n  replaying {n_rules} rules on {len(tokens)} tokens...")
    for info in tokens.values():
        quarter = sum(1 for c in cuts if info["launch"] >= c)
        for delay in DELAYS:
            state = entry_state(info, delay)
            if state is None:
                continue
            mask = sum(1 << i for i, (_, rule) in enumerate(FILTERS) if rule(state))
            if not mask:
                continue
            for tp, sl, hold in exits:
                if state["fill_t"] + hold > data_end:
                    continue                  # outcome not known yet
                mult, why = simulate(info["trades"], state["fill"],
                                     state["fill_t"], tp, sl, hold)
                cells[(delay, tp, sl, hold)].append(
                    (mult, why, state["fee"], quarter, mask))

    def items(rule, quarters=(0, 1, 2, 3)):
        delay, filt, tp, sl, hold = rule
        bit = 1 << FILTER_INDEX[filt]
        return [x for x in cells.get((delay, tp, sl, hold), [])
                if x[4] & bit and x[3] in quarters]

    rules = [(d, f, tp, sl, h) for d in DELAYS for f, _ in FILTERS
             for tp, sl, h in exits]
    stats = {}
    for rule in rules:
        first, second = summary(items(rule, (0, 1))), summary(items(rule, (2, 3)))
        if first["n"] >= MIN_N and second["n"] >= MIN_N:
            stats[rule] = {"first": first, "second": second,
                           "all": summary(items(rule))}

    # ---------------------------------------------- 2. first vs second
    header = ("  entry   filter                TP    SL    hold |"
              "    n  1st half |    n  2nd half |    n  all     w/o best"
              "   win  TP hit")
    line = "  " + "-" * (len(header) - 2)

    def row(rule) -> str:
        if rule not in stats:
            return f"  {label(rule)} |  too few trades in one of the halves"
        a, b, c = (stats[rule][h] for h in ("first", "second", "all"))
        return (f"  {label(rule)} | {a['n']:>4} {pct(a['mean'])}  |"
                f" {b['n']:>4} {pct(b['mean'])}  | {c['n']:>4} {pct(c['mean'])}"
                f"  {pct(c['no_best'])}  {c['win'] * 100:>4.0f}%  "
                f"{c['tp'] * 100:>4.0f}%")

    print(f"\n2. BEST 15 RULES BY THEIR WORSE HALF\n")
    print("  avg profit per trade after all costs. A rule is only as good as")
    print("  its worse half: a rule that wins one half and loses the other is luck.\n")
    print(header)
    print(line)
    robust = sorted(stats, key=lambda r: -min(stats[r]["first"]["mean"],
                                              stats[r]["second"]["mean"]))
    for rule in robust[:15]:
        print(row(rule))

    print("\n3. REFERENCE RULES (fixed in advance, not picked by the data)\n")
    print(header)
    print(line)
    for rule in REFERENCE:
        print(row(rule))

    if not robust:
        print("\n  Not enough trades yet for any rule.")
        return

    # -------------------------------------------- 4. quarters, slippage
    print("\n4. THE TOP 5 BY QUARTER (each quarter = a quarter of the launches)\n")
    print("  entry   filter                TP    SL    hold |"
          "     Q1       Q2       Q3       Q4    quarters > 0")
    print("  " + "-" * 98)
    for rule in robust[:5]:
        qs = [summary(items(rule, (q,))) for q in range(4)]
        cols = "  ".join(f"{pct(s['mean'])}" if s["n"] else "     -  " for s in qs)
        good = sum(1 for s in qs if s["n"] and s["mean"] > 0)
        print(f"  {label(rule)} |  {cols}    {good} of 4")

    best = robust[0]
    print(f"\n5. THE TOP RULE UNDER DIFFERENT SLIPPAGE (all data)\n")
    print(f"  {label(best)}")
    for slip in SLIPPAGE_CHECK:
        s = summary(items(best), slip)
        print(f"    slippage {slip * 100:>3.1f}% per side   avg {pct(s['mean'])}"
              f"   +-{s['se'] * 200:.1f}% (2 SE)")

    # ------------------------------------------------------ 6. verdict
    both = [r for r in stats if stats[r]["first"]["mean"] > 0
            and stats[r]["second"]["mean"] > 0]
    solid = [r for r in both
             if stats[r]["all"]["mean"] > 2 * stats[r]["all"]["se"]
             and stats[r]["all"]["no_best"] > 0]
    print("\n6. VERDICT\n")
    print(f"  rules with enough trades in both halves:  {len(stats)}")
    print(f"  profitable in the first half:            "
          f"{sum(1 for r in stats if stats[r]['first']['mean'] > 0)}")
    print(f"  profitable in the second half:           "
          f"{sum(1 for r in stats if stats[r]['second']['mean'] > 0)}")
    print(f"  profitable in BOTH halves:               {len(both)}")
    print(f"  ...and clearly above zero overall (avg > 2 SE, still positive")
    print(f"     without the single best trade):       {len(solid)}")
    for r in sorted(solid, key=lambda r: -stats[r]["all"]["mean"])[:10]:
        print(f"    {label(r)}   all {pct(stats[r]['all']['mean'])}")
    print("\n  With hundreds of rules tried, a few pass by luck. A rule that")
    print("  passes here is a candidate for paper trading, not proof.")
    print(f"\n  costs used: 1% + the token's own second fee + "
          f"{SLIPPAGE * 100:.0f}% slippage per side, {REACT_SECONDS} s reaction time")


if __name__ == "__main__":
    main()
