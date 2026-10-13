"""
Hold test: what if you buy early and simply hold?

The share cards people post (88,000%, 259,000%) come from buying a token
early on its curve and holding it for days. backtest_pons.py never tests
that: its longest hold is 60 minutes and its biggest take-profit is 3x,
so a 100x could never show up there.

This asks the plain question for every recorded ETH-paired token:
  - enter the first time its price reaches N x the launch price
    (only information available at that moment)
  - hold until now. If it graduated, its value now comes from the latest
    grad_tracker.py lookup (grad_history.csv). If it never graduated, it
    is sold back to the curve at its last price
  - after fees and slippage on the buy and on the sell

Only tokens launched at least MIN_AGE_HOURS ago are counted, so each one
had time to either die or run. "Now" is still a single snapshot, so run
this again on later days and compare.

Read-only analysis of local files.

    python hold_test.py
"""

import csv
import statistics
import sys
from collections import defaultdict

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"
GRAD_FILE = "graduations.csv"
HISTORY_FILE = "grad_history.csv"

ENTRY_MULTIPLES = [1, 2, 5, 10]   # 1 = the very first trade (a perfect snipe)
MIN_AGE_HOURS = 24
SUPPLY = 1_000_000_000
MIN_AMOUNT = 10 ** 13             # ignore dust trades for pricing
FEE_A = 0.01                      # curve fee on every trade
SLIPPAGE = 0.01                   # per side
DEX_FEE = 0.01                    # assumed pool fee when selling after graduation


def read(path: str, required: bool = True) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        if required:
            sys.exit(f"{path} not found - run this in the meme-bot folder")
        return []


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load():
    tokens = {}                                   # curve -> info
    for row in read(LAUNCH_FILE):
        if row["data0"] not in ("0", ""):
            continue                              # token-paired: no ETH value
        curve = row["topic2"].lower()
        tokens[curve] = {
            "token": row["token"].lower(), "curve": curve,
            "symbol": row["symbol"] or "?",
            "launch": int(row["est_time"]), "trades": [],
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
        if row["side"] == "buy":
            gross, moved = d[0], d[1]
            amount = gross - d[2] - d[3]
        elif row["side"] == "sell":
            moved, gross = d[0], d[1] + d[2] + d[3]
            amount = gross
        else:
            continue
        if moved == 0 or amount < MIN_AMOUNT:
            continue
        info["trades"].append((int(row["block"]), int(row["log_index"]),
                               int(row["est_time"]), amount / moved,
                               d[3] / gross if gross else 0.0))
    for info in tokens.values():
        info["trades"].sort()

    graduated = set()                             # watcher saves topics without topic0
    for row in read(GRAD_FILE, required=False):
        graduated |= {"0x" + t[-40:].lower() for t in row["topics"].split()}

    latest = {}                                   # token -> newest grad_history row
    for row in read(HISTORY_FILE, required=False):
        tok = row["token"].lower()
        if tok not in latest or to_int(row["checked_at"]) >= to_int(latest[tok]["checked_at"]):
            latest[tok] = row

    rates = [to_float(r["market_cap_usd"]) / to_float(r["now_mc_eth"])
             for r in latest.values()
             if (to_float(r["now_mc_eth"]) or 0) > 0 and (to_float(r["market_cap_usd"]) or 0) > 0]
    eth_usd = statistics.median(rates) if rates else None
    return tokens, graduated, latest, eth_usd


def usd(mc_eth: float, eth_usd) -> str:
    if not eth_usd:
        return f"{mc_eth:.1f} ETH"
    v = mc_eth * eth_usd
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}k"


def main() -> None:
    tokens, graduated, latest, eth_usd = load()
    traded = [i for i in tokens.values() if i["trades"]]
    if not traded:
        sys.exit("No traded ETH-paired launches yet.")
    data_end = max(i["trades"][-1][2] for i in traded)
    cutoff = data_end - MIN_AGE_HOURS * 3600
    old = [i for i in traded if i["launch"] <= cutoff]
    if not old:
        sys.exit(f"No token is {MIN_AGE_HOURS} h old yet - run again later.")
    old.sort(key=lambda i: i["launch"])
    middle = old[len(old) // 2]["launch"]

    results = defaultdict(list)      # multiple -> [(net multiple, info, entry_mc, exit_mc)]
    grads = missing = 0
    launch_mcs = []
    for info in old:
        trades = info["trades"]
        first = trades[0][3]
        launch_mcs.append(first * SUPPLY)
        fee_b = statistics.median(t[4] for t in trades)
        if info["token"] in graduated or info["curve"] in graduated:
            grads += 1
            row = latest.get(info["token"])
            exit_mc = to_float(row["now_mc_eth"]) if row else None
            if not exit_mc:
                missing += 1          # graduated, but no ETH value known now
                continue
            sell_cost = DEX_FEE + SLIPPAGE
        else:
            exit_mc = trades[-1][3] * SUPPLY
            sell_cost = FEE_A + fee_b + SLIPPAGE
        buy_cost = FEE_A + fee_b + SLIPPAGE
        for m in ENTRY_MULTIPLES:
            entry = next((t for t in trades if t[3] >= first * m), None)
            if entry is None:
                continue              # never got there, so never bought
            entry_mc = entry[3] * SUPPLY
            net = exit_mc / entry_mc * (1 - buy_cost) * (1 - sell_cost)
            results[m].append((net, info, entry_mc, exit_mc))

    span_h = (old[-1]["launch"] - old[0]["launch"]) / 3600
    launch_mc = statistics.median(launch_mcs)
    print(f"\nHOLD TEST - buy when a token first reaches N x its launch price,"
          f" hold until now\n")
    print(f"  {len(old)} ETH-paired tokens launched at least {MIN_AGE_HOURS} h ago"
          f" (launches spread over {span_h:.0f} h)")
    print(f"  graduated: {grads}  (left out, no value found now: {missing})")
    if eth_usd:
        print(f"  ETH ~ ${eth_usd:,.0f} (from DexScreener); typical launch "
              f"market cap {usd(launch_mc, eth_usd)}")
    print("  costs: 1% + the token's own fee + 1% slippage per side on the curve,"
          f" {DEX_FEE * 100:.0f}% + 1% when selling after graduation\n")

    print("  entry                       tokens   $1 in each    without   median"
          "    >1x    >10x   >100x")
    print("  when price first reaches              -> now      best one")
    print("  " + "-" * 88)
    for m in ENTRY_MULTIPLES:
        rs = results.get(m, [])
        if not rs:
            print(f"  {m}x launch: no tokens")
            continue
        nets = sorted(r[0] for r in rs)
        n = len(nets)
        mean = statistics.fmean(nets)
        no_best = (sum(nets) - nets[-1]) / (n - 1) if n > 1 else mean
        label = ("launch (first trade)" if m == 1 else f"{m}x launch") + \
            f" ~{usd(launch_mc * m, eth_usd)}"
        print(f"  {label:<28}{n:>6}   ${mean:>8.2f}   ${no_best:>7.2f}"
              f"   {statistics.median(nets):>5.2f}x"
              f"  {sum(1 for x in nets if x > 1) / n * 100:>5.1f}%"
              f"  {sum(1 for x in nets if x > 10) / n * 100:>5.1f}%"
              f"  {sum(1 for x in nets if x > 100) / n * 100:>5.1f}%")

    print("\n  Same test on each half of the launches (a real edge holds in both):\n")
    print("  entry            first half: tokens  $1 ->   |  second half: tokens  $1 ->")
    for m in ENTRY_MULTIPLES:
        rs = results.get(m, [])
        a = [r[0] for r in rs if r[1]["launch"] < middle]
        b = [r[0] for r in rs if r[1]["launch"] >= middle]
        fa = f"{len(a):>6}  ${statistics.fmean(a):>6.2f}" if a else f"{0:>6}       -"
        fb = f"{len(b):>6}  ${statistics.fmean(b):>6.2f}" if b else f"{0:>6}       -"
        print(f"  {m:>2}x launch                {fa}   |              {fb}")

    best = sorted(results.get(2, []), key=lambda r: -r[0])[:10]
    if best:
        print("\n  Best 10 for an entry at 2x launch:\n")
        print("  symbol          entry MC     now MC     result")
        for net, info, emc, xmc in best:
            print(f"  {info['symbol'][:14]:<14} {usd(emc, eth_usd):>9}  "
                  f"{usd(xmc, eth_usd):>9}   {net:>7.1f}x")

    print("\n  Read it like this: '$1 in each -> now' is what $1 put into EVERY")
    print("  such token is worth today, losers included. Above $1 = the strategy")
    print("  made money. If it is only above $1 because of the best one, it is luck.")


if __name__ == "__main__":
    main()
