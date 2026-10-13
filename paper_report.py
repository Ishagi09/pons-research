"""
Score the paper trader so far, from paper_trades.csv.

  1. per strategy: closed trades, win%, average and median result after
     costs, total virtual profit
  2. the same split by the token's second fee (low = at most 1%), and by
     whether the dev had sold before entry - both are known at entry,
     so they work as filters
  3. by time of day, to see whether some hours are worse
  4. what a small wallet would really have done: only MAX_OPEN positions
     at once; signals that arrive while it is full are skipped

Only closed trades are counted. Trades still open are left out, and early
on that favours quick take-profits, so judge after a day or more.

Safe to run while paper_trader.py is running. Reads paper_trades.csv by
default; give another file to score it instead, e.g. version 3:

    python paper_report.py
    python paper_report.py paper_trades_v3.csv
"""

import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

TRADES_FILE = "paper_trades.csv"
LOW_FEE = 0.01
WALLET_LIMITS = [1, 3, 5]          # max open positions at once
HOUR_BLOCK = 3                     # time-of-day buckets, hours


def load() -> list[dict]:
    path = sys.argv[1] if len(sys.argv) > 1 else TRADES_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            raw = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"{path} not found - run this in the meme-bot folder")
    rows = []
    for r in raw:
        try:
            closed = datetime.strptime(r["closed_at"], "%Y-%m-%d %H:%M:%S")
            rows.append({
                "strategy": r["strategy"],
                "net": float(r["net_pct"]) / 100,
                "pnl": float(r["pnl_eth"]),
                "fee": float(r["fee_b"]),
                "dev_sold": r["dev_sold_at_entry"] == "True",
                "reason": r["reason"],
                "closed": closed,
                "opened": closed - timedelta(seconds=float(r["hold_s"])),
            })
        except (KeyError, TypeError, ValueError):
            continue                     # half-written line after a crash
    return rows


def stats(rows: list) -> str:
    if not rows:
        return f"{0:>6}       -          -"
    nets = [r["net"] for r in rows]
    win = sum(1 for x in nets if x > 0) / len(nets) * 100
    return (f"{len(rows):>6}  {win:>4.0f}%  {statistics.fmean(nets) * 100:>+7.1f}%")


def wallet(rows: list, limit: int) -> tuple[int, float, float]:
    """Take trades in the order they opened, never more than `limit` at once."""
    open_until, taken, nets, pnl = [], 0, [], 0.0
    for r in sorted(rows, key=lambda x: x["opened"]):
        open_until = [t for t in open_until if t > r["opened"]]
        if len(open_until) < limit:
            open_until.append(r["closed"])
            taken += 1
            nets.append(r["net"])
            pnl += r["pnl"]
    return taken, (statistics.fmean(nets) if nets else 0.0), pnl


def main() -> None:
    rows = load()
    if not rows:
        sys.exit("No closed paper trades yet.")
    by_strategy = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)
    names = sorted(by_strategy)
    first = min(r["opened"] for r in rows)
    last = max(r["closed"] for r in rows)
    hours = (last - first).total_seconds() / 3600
    print(f"\nPaper trades: {len(rows)} closed over {hours:.1f} hours\n")

    # ------------------------------------------------------------- 1
    print("1. PER STRATEGY (after fees, slippage and gas)\n")
    print("  strategy                   trades   win      avg    median   total pnl"
          "    exits (tp / sl / time / grad)")
    for name in names:
        rs = by_strategy[name]
        nets = [r["net"] for r in rs]
        reasons = defaultdict(int)
        for r in rs:
            reasons[r["reason"]] += 1
        print(f"  {name:<25} {stats(rs)}  {statistics.median(nets) * 100:>+7.1f}%"
              f"  {sum(r['pnl'] for r in rs):>+.5f} ETH   "
              f"{reasons['tp']} / {reasons['sl']} / {reasons['time']} / "
              f"{reasons['graduated']}")

    # ------------------------------------------------------------- 2
    print("\n2. FILTERS KNOWN AT ENTRY\n")
    print("  strategy                  | low fee: trades  win    avg  "
          "| high fee: trades  win    avg  | dev sold: trades  win    avg")
    for name in names:
        rs = by_strategy[name]
        low = [r for r in rs if r["fee"] <= LOW_FEE]
        high = [r for r in rs if r["fee"] > LOW_FEE]
        sold = [r for r in rs if r["dev_sold"]]
        print(f"  {name:<25} |       {stats(low)}  |        {stats(high)}  "
              f"|        {stats(sold)}")

    # ------------------------------------------------------------- 3
    print(f"\n3. BY TIME OF DAY (your local time, when the trade opened)\n")
    header = "  hours      " + "".join(f"{n[:16]:>18}" for n in names)
    print(header)
    for start in range(0, 24, HOUR_BLOCK):
        cells = []
        for name in names:
            rs = [r for r in by_strategy[name]
                  if start <= r["opened"].hour < start + HOUR_BLOCK]
            if rs:
                avg = statistics.fmean(r["net"] for r in rs) * 100
                cells.append(f"{avg:>+7.1f}% ({len(rs):>4})")
            else:
                cells.append(f"{'-':>15}")
        print(f"  {start:02d}-{start + HOUR_BLOCK:02d}     " +
              "".join(f"{c:>18}" for c in cells))

    # ------------------------------------------------------------- 4
    print("\n4. A SMALL WALLET - only a few positions open at once\n")
    print("  each cell: trades taken, average result, total virtual pnl (ETH)\n")
    print("  strategy                   filter   " + "".join(
        f"| max {k} open                  " for k in WALLET_LIMITS))
    for name in names:
        for label, rs in (("all", by_strategy[name]),
                          ("low fee", [r for r in by_strategy[name]
                                       if r["fee"] <= LOW_FEE])):
            cells = []
            for k in WALLET_LIMITS:
                taken, avg, pnl = wallet(rs, k)
                cells.append(f"| {taken:>5} {avg * 100:>+6.1f}% {pnl:>+.5f}  ")
            print(f"  {name:<25}  {label:<8} " + "".join(cells))

    print("\n  Trades still open are not counted. Judge after a day or more,")
    print("  and only on strategies with a few hundred closed trades.")


if __name__ == "__main__":
    main()
