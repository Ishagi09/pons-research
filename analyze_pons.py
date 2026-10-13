"""
What actually happens to a Pons token after launch?

Reads launches.csv and curve_trades.csv written by watcher.py and answers:

  1. Fees     - is the curve really charging ~1% + ~2% on every trade?
  2. Funnel   - how many tokens get any buyers, many buyers, graduate?
  3. Peaks    - how often does a token ever reach 2x / 10x / 100x
                of its launch price?
  4. The bet  - if you buy at a given moment, how often does price hit
                2x before it falls to half? (A fair coin gives ~33%.)
  5. A signal - does the deployer selling early predict the outcome,
                using only information you would have had at entry?

Field meanings, worked out from real rows (Oct 2026):
  TokenLaunched: topic1 = token, topic2 = bonding curve, topic3 = deployer
                 data0 = pair token (0 = native ETH), data2 = graduation target
  CurveBuy:      data0 = ETH in (gross), data1 = tokens out, data2/data3 = fees
  CurveSell:     data0 = tokens in, data1 = ETH out (net), data2/data3 = fees
  topic2 of a trade = the trader receiving tokens or ETH

Read-only analysis of local files.

    python analyze_pons.py
"""

import csv
import statistics
import sys
from collections import defaultdict

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"
GRAD_FILE = "graduations.csv"

WEI = 10 ** 18
MIN_ETH_FOR_PRICE = 10 ** 13       # ignore dust trades below 0.00001 ETH
MATURE_SECONDS = 2 * 3600          # only judge tokens at least 2 hours old
ENTRY_DELAYS = [0, 120, 300, 600]  # seconds after launch to simulate entry
SIGNAL_ENTRY = 300                 # dev-sell signal tested at a 5 min entry
SUPPLY = 1_000_000_000             # fixed Pons supply, for market cap


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


def load():
    tokens = {}                       # curve -> token info
    other_pair = 0
    for row in read(LAUNCH_FILE):
        curve = row["topic2"].lower()
        # Token-paired launches are kept: multiples are unit-free, so 2x is
        # 2x whatever the pair. Only the ETH market cap needs an ETH pair.
        eth_paired = row["data0"] in ("0", "")
        if not eth_paired:
            other_pair += 1
        dev = row["topic3"].lower() if row["topic3_is_contract"] == "False" else ""
        tokens[curve] = {
            "eth_paired": eth_paired,
            "symbol": row["symbol"] or "?",
            "token": row["token"].lower(),
            "launch": int(row["est_time"]),
            "launch_tx": row["tx"],
            "dev": dev,
            "target": to_int(row["data2"]),
            "trades": [],
        }

    fees = []
    total_trades = 0
    seen = set()          # a stopped watcher can replay one chunk; drop repeats
    for row in read(TRADE_FILE):
        key = (row["tx"], row["log_index"])
        if key in seen:
            continue
        seen.add(key)
        total_trades += 1
        info = tokens.get(row["curve"].lower())
        d = [to_int(row[f"data{i}"]) for i in range(4)]
        side = row["side"]
        if side == "buy":
            gross, tokens_moved = d[0], d[1]
            curve_eth = gross - d[2] - d[3]
        elif side == "sell":
            tokens_moved = d[0]
            gross = d[1] + d[2] + d[3]
            curve_eth = gross
        else:
            continue
        if gross >= MIN_ETH_FOR_PRICE:
            fees.append((d[2] / gross, d[3] / gross))
        if info is None or tokens_moved == 0 or curve_eth < MIN_ETH_FOR_PRICE:
            continue
        # router-launched tokens: the dev is whoever received the launch buy
        if not info["dev"] and row["tx"] == info["launch_tx"] and side == "buy":
            info["dev"] = row["topic2"].lower()
        info["trades"].append({
            "t": int(row["est_time"]),
            "block": int(row["block"]),
            "idx": int(row["log_index"]),
            "side": side,
            "price": curve_eth / tokens_moved,
            "eth": curve_eth / WEI,
            "who": row["topic2"].lower(),
            "caller": row["topic1"].lower(),
        })

    for info in tokens.values():
        info["trades"].sort(key=lambda x: (x["block"], x["idx"]))

    grad_events = []
    for row in read(GRAD_FILE) if _exists(GRAD_FILE) else []:
        # watcher.py already drops topic0 (the event signature) when saving
        grad_events.append({"0x" + t[-40:].lower()
                            for t in row["topics"].split()})
    known = set(tokens) | {i["token"] for i in tokens.values()}
    matched = sum(1 for g in grad_events if g & known)
    graduated = set().union(*grad_events) if grad_events else set()
    for curve, info in tokens.items():
        info["graduated"] = curve in graduated or info["token"] in graduated

    grad_summary = (len(grad_events), matched)
    return tokens, other_pair, total_trades, fees, grad_summary


def _exists(path: str) -> bool:
    try:
        open(path).close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------- helpers
def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "   -  "


def entry_point(trades: list, launch: int, delay: int):
    """First trade at or after launch + delay, and the trades after it."""
    for i, tr in enumerate(trades):
        if tr["t"] >= launch + delay:
            return tr, trades[i + 1:]
    return None, []


def double_or_half(entry_price: float, later: list) -> str:
    for tr in later:
        if tr["price"] >= entry_price * 2:
            return "win"
        if tr["price"] <= entry_price * 0.5:
            return "loss"
    return "stuck"


# ------------------------------------------------------------------- run
def main() -> None:
    tokens, other_pair, total_trades, fees, (grad_total, grad_matched) = load()
    if not tokens:
        sys.exit("No launches found yet.")

    data_end = max((tr["t"] for i in tokens.values() for tr in i["trades"]),
                   default=max(i["launch"] for i in tokens.values()))
    data_start = min(i["launch"] for i in tokens.values())
    hours = (data_end - data_start) / 3600

    print(f"\nData: {hours:.1f} hours, {len(tokens)} launches "
          f"({other_pair} token-paired, included), {total_trades} curve trades")
    print(f"Graduation events: {grad_total}, matched to a recorded launch: "
          f"{grad_matched} (unmatched ones launched before the watcher "
          f"started)\n")

    # ---------------------------------------------------------- 1. fees
    print("1. FEES - share of each trade's gross ETH\n")
    if fees:
        f1 = sorted(a for a, _ in fees)
        f2 = sorted(b for _, b in fees)
        q = lambda xs, p: xs[int(p * (len(xs) - 1))] * 100
        print(f"  fee A (data2): median {q(f1, .5):.2f}%   "
              f"10th-90th pct {q(f1, .1):.2f}% - {q(f1, .9):.2f}%")
        print(f"  fee B (data3): median {q(f2, .5):.2f}%   "
              f"10th-90th pct {q(f2, .1):.2f}% - {q(f2, .9):.2f}%")
        per_side = (statistics.median(f1) + statistics.median(f2)) * 100
        print(f"  -> about {per_side:.1f}% per side, ~{per_side * 2:.1f}% "
              f"for a buy and a sell, before any app fee")

    # -------------------------------------------------------- 2. funnel
    mature = {c: i for c, i in tokens.items()
              if data_end - i["launch"] >= MATURE_SECONDS}
    n = len(mature)
    print(f"\n2. FUNNEL - {n} tokens at least {MATURE_SECONDS // 3600}h old\n")
    if n == 0:
        print("  Not enough mature tokens yet. Let the watcher run longer.")
        return

    def buyers(info):
        return len({t["who"] for t in info["trades"]
                    if t["side"] == "buy" and t["who"] != info["dev"]})

    def lifespan(info):
        return (info["trades"][-1]["t"] - info["launch"]) if info["trades"] else 0

    any_outside = sum(1 for i in mature.values() if buyers(i) >= 1)
    five = sum(1 for i in mature.values() if buyers(i) >= 5)
    twenty = sum(1 for i in mature.values() if buyers(i) >= 20)
    grads = sum(1 for i in mature.values() if i["graduated"])
    over_hour = sum(1 for i in mature.values() if lifespan(i) >= 3600)
    spans = sorted(lifespan(i) for i in mature.values())

    print(f"  at least 1 buyer besides the dev   {pct(any_outside, n)}")
    print(f"  at least 5 outside buyers          {pct(five, n)}")
    print(f"  at least 20 outside buyers         {pct(twenty, n)}")
    print(f"  still trading after 1 hour         {pct(over_hour, n)}")
    print(f"  graduated                          {pct(grads, n)}")
    print(f"  median time from launch to last trade: "
          f"{spans[len(spans) // 2] / 60:.1f} min")

    # --------------------------------------------------------- 3. peaks
    print("\n3. PEAKS - highest price ever reached vs the launch price\n")
    peaks = []
    for c, info in mature.items():
        if len(info["trades"]) < 2:
            peaks.append((1.0, c))
            continue
        first = info["trades"][0]["price"]
        peaks.append((max(t["price"] for t in info["trades"]) / first, c))
    for mult in (2, 5, 10, 100):
        hit = sum(1 for p, _ in peaks if p >= mult)
        print(f"  ever reached {mult:>3}x               {pct(hit, n)}  ({hit})")

    # ----------------------------------------------------------- 4. bet
    print("\n4. THE BET - buy at a moment, does price hit 2x before half?\n")
    print("  fair-coin baseline is about 33% wins")
    print("  'stuck' = neither happened before trading stopped (often illiquid)\n")
    print("  entry           tokens   win     loss    stuck")
    print("  " + "-" * 48)
    for delay in ENTRY_DELAYS:
        counts = defaultdict(int)
        for info in mature.values():
            entry, later = entry_point(info["trades"], info["launch"], delay)
            if entry is None:
                continue
            counts[double_or_half(entry["price"], later)] += 1
        total = sum(counts.values())
        label = "first trade" if delay == 0 else f"+{delay // 60} min"
        print(f"  {label:<14} {total:>6}  {pct(counts['win'], total)}  "
              f"{pct(counts['loss'], total)}  {pct(counts['stuck'], total)}")
    print("\n  note: a 2x price is only ~1.88x money after ~3% fees each way")

    # -------------------------------------------------------- 5. signal
    print(f"\n5. SIGNAL - dev sold before a +{SIGNAL_ENTRY // 60} min entry?\n")
    groups = {"dev sold": defaultdict(int), "dev held": defaultdict(int)}
    for info in mature.values():
        if not info["dev"]:
            continue
        entry, later = entry_point(info["trades"], info["launch"], SIGNAL_ENTRY)
        if entry is None:
            continue
        sold_before = any(t["side"] == "sell" and t["who"] == info["dev"]
                          and t["t"] < entry["t"] for t in info["trades"])
        key = "dev sold" if sold_before else "dev held"
        groups[key][double_or_half(entry["price"], later)] += 1
    print("  group        tokens   win     loss    stuck")
    print("  " + "-" * 46)
    for key, counts in groups.items():
        total = sum(counts.values())
        print(f"  {key:<11} {total:>7}  {pct(counts['win'], total)}  "
              f"{pct(counts['loss'], total)}  {pct(counts['stuck'], total)}")
    print("\n  A real signal shows a clear win-rate gap between the groups,")
    print("  with enough tokens in each to trust it.")

    # ----------------------------------------------------------- 6. top
    print("\n6. TOP 10 BY PEAK\n")
    print("  symbol          peak     buyers  launch MC (ETH)  graduated")
    print("  " + "-" * 60)
    for peak, c in sorted(peaks, reverse=True)[:10]:
        info = mature[c]
        if info["eth_paired"] and info["trades"]:
            mc = f"{info['trades'][0]['price'] * SUPPLY:>14.2f}"
        else:
            mc = f"{'token pair':>14}"
        print(f"  {info['symbol'][:14]:<14} {peak:>7.1f}x  {buyers(info):>6}"
              f"  {mc}   {'yes' if info['graduated'] else 'no'}")

    # graduation split by pair type, to see where graduations come from
    for label, flag in (("ETH-paired", True), ("token-paired", False)):
        group = [i for i in mature.values() if i["eth_paired"] == flag]
        g = sum(1 for i in group if i["graduated"])
        print(f"\n  {label:<13} {len(group):>5} mature tokens, "
              f"{g} graduated ({pct(g, len(group)).strip()})", end="")
    print()


if __name__ == "__main__":
    main()
