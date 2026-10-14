"""
Score the pump.fun paper trades in sol_paper_trades.csv.

The pass rule was fixed BEFORE the test started:
  a strategy passes only if its average result is at least +3% per trade
  in BOTH halves of the data (after fees, order delay and gas), with at
  least 100 closed trades in each half.
Anything less is no edge worth real money: live trading adds failed
transactions and competition for the same block, which paper trading
cannot show.

  1. per strategy: trades, win%, average with 95% range, without the best
     trade, median, how positions ended, total virtual profit
  2. first half vs second half, and the verdict
  3. filters known at entry: fee level, price already up 1.5x, outside buyers
  4. how far the price had already run when our order landed
  5. a small wallet: only 1 / 3 / 5 positions open at once

Trades that lived through a stream gap are left out (counted at the top).
Safe to run while sol_bot.py is running.

    python sol_report.py
"""

import csv
import statistics
import sys
import time
from collections import Counter, defaultdict

PAPER_FILE = "sol_paper_trades.csv"
GAP_FILE = "sol_gaps.csv"
PASS_AVG = 0.03            # +3% per trade
MIN_PER_HALF = 100
DEFAULT_FEE = 0.0125
WALLET_LIMITS = [1, 3, 5]


def load() -> tuple:
    path = sys.argv[1] if len(sys.argv) > 1 else PAPER_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            raw = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"{path} not found - run this in the folder where sol_bot.py runs")
    rows, flagged = [], 0
    for r in raw:
        try:
            row = {
                "strategy": r["strategy"],
                "net": float(r["net_pct"]) / 100,
                "pnl": float(r["pnl_sol"]),
                "gas": float(r["gas_sol"]),
                "fee": float(r["fee_b"]),
                "mayhem": r["mayhem"] == "True",
                "buyers": int(r["buyers_at_entry"]),
                "entry_mult": float(r["entry_mult"]),
                "reason": r["reason"],
                "opened": float(r["opened_at"]),
                "hold": float(r["hold_s"]),
            }
        except (KeyError, TypeError, ValueError):
            continue                     # half-written line after a crash
        if r.get("gap") == "True":
            flagged += 1
            continue
        row["closed"] = row["opened"] + row["hold"]
        rows.append(row)
    return rows, flagged


def gap_seconds(start: float, end: float) -> float:
    total = 0.0
    try:
        with open(GAP_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    a = time.mktime(time.strptime(r["start"], "%Y-%m-%d %H:%M:%S"))
                    b = time.mktime(time.strptime(r["end"], "%Y-%m-%d %H:%M:%S"))
                except (KeyError, ValueError):
                    continue
                total += max(0.0, min(b, end) - max(a, start))
    except FileNotFoundError:
        pass
    return total


def stats(nets: list) -> dict:
    n = len(nets)
    m = statistics.fmean(nets)
    se = statistics.stdev(nets) / n ** 0.5 if n > 1 else 0.0
    return {"n": n, "avg": m, "lo": m - 1.96 * se, "hi": m + 1.96 * se,
            "nobest": (sum(nets) - max(nets)) / (n - 1) if n > 1 else m,
            "median": statistics.median(nets),
            "win": sum(1 for x in nets if x > 0) / n}


def cell(rows: list) -> str:
    if not rows:
        return f"{0:>6}       -       -"
    s = stats([r["net"] for r in rows])
    return f"{s['n']:>6}  {s['win'] * 100:>4.0f}%  {s['avg'] * 100:>+6.1f}%"


def wallet(rows: list, limit: int) -> tuple:
    """Take trades in the order they opened, never more than `limit` at once."""
    open_until, nets, pnl = [], [], 0.0
    for r in sorted(rows, key=lambda x: x["opened"]):
        open_until = [t for t in open_until if t > r["opened"]]
        if len(open_until) < limit:
            open_until.append(r["closed"])
            nets.append(r["net"])
            pnl += r["pnl"]
    return len(nets), (statistics.fmean(nets) if nets else 0.0), pnl


def main() -> None:
    rows, flagged = load()
    if not rows:
        sys.exit("No closed paper trades yet.")
    by = defaultdict(list)
    for r in rows:
        by[r["strategy"]].append(r)
    names = list(dict.fromkeys(r["strategy"] for r in rows))    # first-seen order
    first = min(r["opened"] for r in rows)
    last = max(r["closed"] for r in rows)
    hours = (last - first) / 3600
    gaps = gap_seconds(first, last) / 3600
    print(f"\n{len(rows)} closed paper trades over {hours:.1f} hours "
          f"({time.strftime('%d %b %H:%M', time.localtime(first))} to "
          f"{time.strftime('%d %b %H:%M', time.localtime(last))})")
    print(f"stream down for {gaps:.1f} h of that; {flagged} trades that lived "
          f"through a gap are left out")

    # ------------------------------------------------------------------ 1
    print("\n1. PER STRATEGY (after fees, order delay and gas)\n")
    print("  strategy                  trades   win     avg   95% range        "
          "w/o best   median   tp / sl / time / grad      total pnl")
    for name in names:
        rs = by[name]
        s = stats([r["net"] for r in rs])
        ends = Counter(r["reason"] for r in rs)
        print(f"  {name:<24} {s['n']:>7}  {s['win'] * 100:>4.0f}%  "
              f"{s['avg'] * 100:>+6.1f}%  [{s['lo'] * 100:>+5.1f}, {s['hi'] * 100:>+5.1f}]"
              f"   {s['nobest'] * 100:>+6.1f}%  {s['median'] * 100:>+6.1f}%   "
              f"{ends['tp']:>5} /{ends['sl']:>5} /{ends['time']:>5} /{ends['graduated']:>4}"
              f"   {sum(r['pnl'] for r in rs):>+9.4f} SOL")

    # ------------------------------------------------------------------ 2
    split = statistics.median(r["opened"] for r in rows)
    print(f"\n2. BOTH HALVES - split at "
          f"{time.strftime('%d %b %H:%M', time.localtime(split))}\n")
    print("  strategy                  | 1st half: trades    avg | "
          "2nd half: trades    avg | verdict")
    verdicts = {}
    for name in names:
        halves = [[r["net"] for r in by[name] if r["opened"] < split],
                  [r["net"] for r in by[name] if r["opened"] >= split]]
        parts = []
        for h in halves:
            parts.append(f"{len(h):>14}  {statistics.fmean(h) * 100:>+6.1f}%"
                         if h else f"{0:>14}       -")
        if min(len(h) for h in halves) < MIN_PER_HALF:
            verdict = f"WAIT - needs {MIN_PER_HALF}+ trades in each half"
        elif all(statistics.fmean(h) >= PASS_AVG for h in halves):
            verdict = "PASS"
        else:
            verdict = "FAIL"
        verdicts[name] = verdict
        print(f"  {name:<24} | {parts[0]} | {parts[1]} | {verdict}")

    # ------------------------------------------------------------------ 3
    print("\n3. FILTERS KNOWN AT ENTRY  (each cell: trades, win%, average)\n")
    print(f"  strategy                  | fee <= {DEFAULT_FEE * 100:.2f}%"
          f"            | higher fee            | price ran 1.5x+ first "
          f"| 0 outside buyers      | 1+ outside buyers")
    for name in names:
        rs = by[name]
        print(f"  {name:<24} | {cell([r for r in rs if r['fee'] <= DEFAULT_FEE + 1e-9])} "
              f"| {cell([r for r in rs if r['fee'] > DEFAULT_FEE + 1e-9])} "
              f"| {cell([r for r in rs if r['entry_mult'] >= 1.5])} "
              f"| {cell([r for r in rs if r['buyers'] == 0])} "
              f"| {cell([r for r in rs if r['buyers'] > 0])}")

    # ------------------------------------------------------------------ 4
    print("\n4. WHERE OUR ORDER LANDED  (price when the order landed / launch price)\n")
    print("  strategy                  median   25% of trades above   10% above")
    for name in names:
        mults = sorted(r["entry_mult"] for r in by[name])
        q = lambda p: mults[min(len(mults) - 1, int(p * len(mults)))]
        print(f"  {name:<24}  {statistics.median(mults):>6.2f}x  {q(0.75):>17.2f}x"
              f"   {q(0.90):>8.2f}x")

    # ------------------------------------------------------------------ 5
    print("\n5. A SMALL WALLET - only a few positions open at once\n")
    print("  each cell: trades taken, average, total virtual pnl (SOL)\n")
    print("  strategy                 " + "".join(
        f"| max {k} open                  " for k in WALLET_LIMITS))
    for name in names:
        cells = []
        for k in WALLET_LIMITS:
            taken, avg, pnl = wallet(by[name], k)
            cells.append(f"| {taken:>5} {avg * 100:>+6.1f}% {pnl:>+8.4f}  ")
        print(f"  {name:<24} " + "".join(cells))

    sizes = [r["pnl"] / r["net"] for r in rows if abs(r["net"]) > 0.05]
    size = statistics.median(sizes) if sizes else 0.05
    gas_pct = statistics.fmean(r["gas"] for r in rows) / size * 100
    print(f"\n  Gas is included (~{gas_pct:.1f}% of a 0.05 SOL trade, round trip).")
    print(f"  Rule set before the test: PASS only with an average of "
          f"+{PASS_AVG * 100:.0f}% or more in BOTH halves,")
    print(f"  with {MIN_PER_HALF}+ closed trades in each half.")
    passed = [n for n, v in verdicts.items() if v == "PASS"]
    if passed:
        print(f"  Passed: {', '.join(passed)}. Next: check it on the recorded "
              f"data with a fresh split before any real money.")
    elif all(v.startswith("WAIT") for v in verdicts.values()):
        print("  Not enough trades yet - run it again later.")
    else:
        print("  No strategy passed.")


if __name__ == "__main__":
    main()
