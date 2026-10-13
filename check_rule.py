"""
Is the "low fee, no other buyers yet" result real, or a pricing artifact?

Paper trading found one group that made money: tokens with a low second
fee where nobody but the dev had bought when the bot entered. But in
exactly those tokens the "entry price" the bot records is the dev's own
average buy price. On a bonding curve, anyone buying after the dev pays
MORE than that: the dev's buy has already pushed the curve up. So the next
small buy looks like a gain even if a real bot would have broken even.

This script checks that, using the watcher's data:

  1. Fits the curve. If Pons uses a constant-product curve with fixed
     virtual reserves, the dev's first-buy price is a straight line in the
     buy size: price = floor + size / Y0. The fit (R^2) says whether that
     holds, and gives the reserves.
  2. Checks the fit on every later buy (predicted vs actual tokens out).
  3. Replays the rule twice: with the old pricing (last trade's average
     price, as paper_trader.py does) and with real curve prices (the spot
     price the curve is at, plus our own price impact).
  4. Splits by period: before paper trading started (data the rule was
     NOT found on) and after.

If the group still makes money with real curve prices on the earlier
period, it is the first real candidate. If not, it was an artifact.

Read-only analysis of local files.

    python check_rule.py
"""

import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"

PAPER_START = datetime(2026, 10, 10, 17, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
ENTRY_DELAY = 5            # seconds after launch, as in paper_trader.py
REACT = 2                  # decision -> fill
SIZE = 0.002 * 1e18        # our position, wei
FEE_A = 0.01
SLIPPAGES = [0.005, 0.01]  # extra cost per side beyond our modelled impact
LOW_FEE = 0.01
EXITS = [(1.2, 0.5, 600), (1.5, 0.5, 3600)]
MIN_AMOUNT = 10 ** 13
IST = timezone(timedelta(hours=5, minutes=30))


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


# ------------------------------------------------------------------ load
def load():
    tokens = {}
    for row in read(LAUNCH_FILE):
        if row["data0"] not in ("0", ""):
            continue                                   # ETH-paired only
        dev = row["topic3"].lower() if row["topic3_is_contract"] == "False" else ""
        tokens[row["topic2"].lower()] = {
            "launch": int(row["est_time"]), "launch_tx": row["tx"],
            "dev": dev, "trades": []}
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
            gross, moved, flow = d[0], d[1], d[0] - d[2] - d[3]   # ETH into curve
        elif row["side"] == "sell":
            moved, gross = d[0], d[1] + d[2] + d[3]
            flow = -gross                                        # ETH out of curve
        else:
            continue
        if moved == 0 or gross == 0:
            continue
        who = row["topic2"].lower()
        if not info["dev"] and row["tx"] == info["launch_tx"] and row["side"] == "buy":
            info["dev"] = who
        info["trades"].append({
            "block": int(row["block"]), "idx": int(row["log_index"]),
            "t": int(row["est_time"]), "side": row["side"], "flow": flow,
            "moved": moved, "who": who, "fee": d[3] / gross,
            "avg": abs(flow) / moved})
    for info in tokens.values():
        info["trades"].sort(key=lambda x: (x["block"], x["idx"]))
    return {c: i for c, i in tokens.items() if i["trades"]}


# --------------------------------------------------------------- curve fit
def fit_curve(tokens):
    """First-buy price = X0/Y0 + b/Y0 for a constant-product curve."""
    pts = [(i["trades"][0]["flow"], i["trades"][0]["avg"]) for i in tokens.values()
           if i["trades"][0]["side"] == "buy" and i["trades"][0]["flow"] >= MIN_AMOUNT]

    def ols(p):
        n = len(p)
        mx = sum(x for x, _ in p) / n
        my = sum(y for _, y in p) / n
        sxx = sum((x - mx) ** 2 for x, _ in p)
        sxy = sum((x - mx) * (y - my) for x, y in p)
        slope = sxy / sxx
        icpt = my - slope * mx
        ss_tot = sum((y - my) ** 2 for _, y in p)
        ss_res = sum((y - icpt - slope * x) ** 2 for x, y in p)
        return icpt, slope, 1 - ss_res / ss_tot if ss_tot else 0.0

    icpt, slope, r2 = ols(pts)
    # refit without points more than 3% off the line (odd launches)
    core = [(x, y) for x, y in pts if abs(y - icpt - slope * x) <= 0.03 * y]
    if len(core) >= 0.5 * len(pts) and len(core) > 10:
        icpt, slope, r2 = ols(core)
    y0 = 1 / slope
    x0 = icpt * y0
    return x0, y0, r2, len(pts), len(core)


def check_fit(tokens, x0, y0):
    """Predicted vs actual tokens out for every buy, replaying reserves."""
    k = x0 * y0
    errs = []
    for info in tokens.values():
        x = x0
        for tr in info["trades"]:
            if tr["side"] == "buy" and tr["flow"] >= MIN_AMOUNT:
                predicted = k / x - k / (x + tr["flow"])
                errs.append(abs(predicted / tr["moved"] - 1))
            x += tr["flow"]
            if x <= 0:
                break
    errs.sort()
    return errs[len(errs) // 2], errs[int(len(errs) * 0.9)], len(errs)


# ------------------------------------------------------------------ replay
def replay(info, x0, y0, tp, sl, hold):
    """One entry at launch + ENTRY_DELAY. Returns dict or None."""
    k = x0 * y0
    trades = info["trades"]
    decide = info["launch"] + ENTRY_DELAY
    buyers, dev_sold, fill, fees = set(), False, None, []
    x = x0
    xs = []                                   # reserves after each trade
    for tr in trades:
        x += tr["flow"]
        xs.append(x)
    for i, tr in enumerate(trades):
        if tr["t"] > decide + REACT:
            break
        if tr["t"] <= decide:
            if tr["side"] == "buy" and tr["who"] != info["dev"]:
                buyers.add(tr["who"])
            if tr["side"] == "sell" and tr["who"] == info["dev"]:
                dev_sold = True
        fees.append(tr["fee"])
        fill = i
    if fill is None or not info["dev"] or dev_sold:
        return None
    if xs[fill] <= 0:
        return None
    fee = statistics.median(fees)

    # entry: old = last trade's average price; spot = curve price + our impact
    old_entry = trades[fill]["avg"]
    spot_entry = xs[fill] ** 2 / k * (1 + SIZE / xs[fill])

    def run(price_of, entry):
        deadline = decide + REACT + hold
        j = fill
        while j + 1 < len(trades) and trades[j + 1]["t"] <= deadline:
            j += 1
            r = price_of(j) / entry
            if r >= tp or r <= sl:
                until, m = trades[j]["t"] + REACT, j
                while m + 1 < len(trades) and trades[m + 1]["t"] <= until:
                    m += 1
                return price_of(m) / entry, "tp" if r >= tp else "sl"
        return price_of(j) / entry, "time"

    old_mult, old_why = run(lambda j: trades[j]["avg"], old_entry)

    def spot_sell(j):                         # what selling into the curve pays
        xj = max(xs[j], 1)
        return xj ** 2 / k * (1 - SIZE / xj)
    spot_mult, spot_why = run(spot_sell, spot_entry)

    return {"fee": fee, "buyers": len(buyers), "launch": info["launch"],
            "old": old_mult, "spot": spot_mult, "old_why": old_why,
            "spot_why": spot_why, "bias": spot_entry / old_entry}


def net(mult, fee, slip):
    c = FEE_A + fee + slip
    return mult * (1 - c) ** 2 - 1


def stats(xs):
    if not xs:
        return "     0       -          -   "
    n = len(xs)
    m = statistics.fmean(xs)
    nb = (sum(xs) - max(xs)) / (n - 1) if n > 1 else m
    return f"{n:>6}  {m * 100:>+6.1f}%   {nb * 100:>+6.1f}%"


# --------------------------------------------------------------------- run
def main():
    tokens = load()
    print(f"\n{len(tokens)} ETH-paired tokens with trades")

    x0, y0, r2, n_all, n_core = fit_curve(tokens)
    print("\n1. CURVE FIT (dev's first buy: price = floor + size / Y0)\n")
    print(f"  points: {n_all}, used after dropping odd ones: {n_core}")
    print(f"  R^2 = {r2:.4f}   (close to 1 = constant-product curve confirmed)")
    print(f"  virtual reserves: {x0 / 1e18:.3f} ETH  and  {y0 / 1e18 / 1e9:.3f}B tokens")
    print(f"  floor market cap: {x0 / y0 * 1e9:.3f} ETH")

    med, p90, n = check_fit(tokens, x0, y0)
    print(f"\n2. FIT CHECK on {n} buys: predicted vs actual tokens out")
    print(f"  median error {med * 100:.2f}%   90th pct {p90 * 100:.2f}%")
    if med > 0.02:
        print("  WARNING: the model does not fit well - treat spot prices as rough")

    cutoff = PAPER_START.timestamp()
    data_end = max(i["trades"][-1]["t"] for i in tokens.values())
    for tp, sl, hold in EXITS:
        rows = [r for r in (replay(i, x0, y0, tp, sl, hold) for i in tokens.values())
                if r and r["launch"] + ENTRY_DELAY + REACT + hold <= data_end]
        print(f"\n3. RULE: enter +{ENTRY_DELAY} s, dev held, TP {tp}x, "
              f"SL -{round((1 - sl) * 100)}%, {hold // 60} min  (dev-held entries: {len(rows)})")
        zero = [r["bias"] for r in rows if r["buyers"] == 0]
        some = [r["bias"] for r in rows if r["buyers"] > 0]
        if zero:
            print(f"  real entry vs recorded entry: no other buyers "
                  f"{(statistics.median(zero) - 1) * 100:+.1f}% (median), "
                  f"with buyers {(statistics.median(some) - 1) * 100 if some else 0:+.1f}%")
        for slip in SLIPPAGES:
            print(f"\n  extra slippage {slip * 100:.1f}% per side      |"
                  f"   OLD pricing (last trade)     |   REAL curve pricing")
            print("  group                  period |     n     avg   w/o best    |"
                  "     n     avg   w/o best")
            for fee_label, fee_ok in (("low fee", lambda f: f <= LOW_FEE),
                                      ("high fee", lambda f: f > LOW_FEE)):
                for b_label, b_ok in (("0 buyers", lambda b: b == 0),
                                      ("1 buyer", lambda b: b == 1),
                                      ("2+ buyers", lambda b: b >= 2)):
                    for p_label, p_ok in (("before", lambda t: t < cutoff),
                                          ("after", lambda t: t >= cutoff)):
                        g = [r for r in rows if fee_ok(r["fee"]) and b_ok(r["buyers"])
                             and p_ok(r["launch"])]
                        old = [net(r["old"], r["fee"], slip) for r in g]
                        new = [net(r["spot"], r["fee"], slip) for r in g]
                        print(f"  {fee_label + ', ' + b_label:<22} {p_label:<6} |"
                              f" {stats(old)}  | {stats(new)}")

        # day by day for the group in question, real pricing
        key = [r for r in rows if r["fee"] <= LOW_FEE and r["buyers"] == 0]
        by_day = defaultdict(list)
        for r in key:
            day = datetime.fromtimestamp(r["launch"], IST).strftime("%b %d")
            by_day[day].append(net(r["spot"], r["fee"], SLIPPAGES[-1]))
        print("\n  low fee, 0 buyers - real pricing, 1% slippage, by day (IST):")
        for day, xs in sorted(by_day.items()):
            print(f"    {day}  {stats(xs)}")

    print("\n  'before' = launched before paper trading started (the group was")
    print("  NOT found on this data). Only a REAL-pricing 'before' result above")
    print("  zero, still above zero without the best trade, counts.")


if __name__ == "__main__":
    main()
