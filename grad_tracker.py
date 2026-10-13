"""
What happens to a Pons token after it graduates?

On the bonding curve a token can rise roughly 12x at most; at graduation
it moves to a Uniswap v4 pool that watcher.py does not see. Any 100x story
has to happen there. This looks every graduated token up on DexScreener
and records where it is now.

  - reads graduations.csv and launches.csv from the watcher
  - asks DexScreener for current price, market cap, liquidity, volume
  - estimates the market cap at graduation from the last curve trade
    (ETH-paired tokens only) and prints the multiple since graduation
  - appends every lookup to grad_history.csv, so running it daily builds
    a time series

Read-only public API, no key. Run any time:

    python grad_tracker.py
"""

import csv
import os
import time
from collections import defaultdict

import requests

LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"
GRAD_FILE = "graduations.csv"
HISTORY_FILE = "grad_history.csv"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens/"
BATCH = 30                       # DexScreener accepts up to 30 addresses
SUPPLY = 1_000_000_000

HISTORY_FIELDS = ["checked_at", "token", "symbol", "chain", "dex", "pair",
                  "price_usd", "market_cap_usd", "liquidity_usd",
                  "volume_24h_usd", "change_24h_pct", "grad_mc_eth",
                  "now_mc_eth", "multiple_since_grad"]


def read(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def graduated_tokens() -> tuple[dict, int, int]:
    """({token_address: info}, graduation events, events mapped to a launch).

    Graduation events name the token or its curve among their topics. When
    the launch was recorded, map back to the token; otherwise try each
    topic address as a token directly (DexScreener ignores the ones that
    are not tokens).
    """
    by_token, by_curve = {}, {}
    for row in read(LAUNCH_FILE):
        info = {"symbol": row["symbol"] or "?", "token": row["token"].lower(),
                "curve": row["topic2"].lower(),
                "eth_paired": row["data0"] in ("0", "")}
        by_token[info["token"]] = info
        by_curve[info["curve"]] = info

    found, events, mapped = {}, 0, 0
    for row in read(GRAD_FILE):
        events += 1
        # watcher.py already drops topic0 (the event signature) when saving
        addrs = ["0x" + t[-40:].lower() for t in row["topics"].split()
                 if len(t) >= 42]
        match = next((by_token.get(a) or by_curve.get(a) for a in addrs
                      if a in by_token or a in by_curve), None)
        if match:
            mapped += 1
            found[match["token"]] = match
        else:
            for a in addrs:
                found.setdefault(a, {"symbol": "?", "token": a, "curve": "",
                                     "eth_paired": False})
    return found, events, mapped


def last_curve_price() -> dict:
    """{curve: price in pair units per token} from the final curve trade."""
    last = {}
    for row in read(TRADE_FILE):
        d = [int(row[f"data{i}"] or 0) for i in range(4)]
        if row["side"] == "buy":
            pair_amt, toks = d[0] - d[2] - d[3], d[1]
        elif row["side"] == "sell":
            pair_amt, toks = d[1] + d[2] + d[3], d[0]
        else:
            continue
        if toks <= 0 or pair_amt <= 0:
            continue
        key = row["curve"].lower()
        block = int(row["block"])
        if key not in last or block >= last[key][0]:
            last[key] = (block, pair_amt / toks)
    return {k: v[1] for k, v in last.items()}


def dexscreener(addresses: list[str]) -> dict:
    """{token_address: best pair by liquidity}."""
    best = {}
    for i in range(0, len(addresses), BATCH):
        chunk = addresses[i:i + BATCH]
        try:
            r = requests.get(DEX_API + ",".join(chunk), timeout=20)
            pairs = (r.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError) as exc:
            print(f"  DexScreener batch failed: {type(exc).__name__}")
            continue
        for p in pairs:
            addr = (p.get("baseToken") or {}).get("address", "").lower()
            if addr not in chunk:
                continue
            liq = num((p.get("liquidity") or {}).get("usd"))
            if addr not in best or liq > num((best[addr].get("liquidity") or {}).get("usd")):
                best[addr] = p
        time.sleep(1)
    return best


def main() -> None:
    grads, events, mapped = graduated_tokens()
    if not grads:
        print("No graduations recorded yet.")
        return
    curve_price = last_curve_price()
    print(f"\nGraduation events: {events}, mapped to a recorded launch: {mapped}")
    print(f"Looking up {len(grads)} candidate addresses on DexScreener...\n")
    pairs = dexscreener(list(grads))

    new_file = not os.path.exists(HISTORY_FILE)
    out = open(HISTORY_FILE, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=HISTORY_FIELDS)
    if new_file:
        writer.writeheader()
    now = int(time.time())

    rows = []
    chains = defaultdict(int)
    for token, info in grads.items():
        p = pairs.get(token)
        if not p:
            continue
        chains[p.get("chainId", "?")] += 1
        price_usd = num(p.get("priceUsd"))
        price_native = num(p.get("priceNative"))
        quote = (p.get("quoteToken") or {}).get("symbol", "").upper()
        mc = num(p.get("marketCap")) or num(p.get("fdv"))

        grad_mc_eth = now_mc_eth = multiple = None
        if info["eth_paired"] and info["curve"] in curve_price:
            grad_mc_eth = curve_price[info["curve"]] * SUPPLY
        if quote in ("WETH", "ETH") and price_native > 0 and mc > 0:
            eth_usd = price_usd / price_native
            now_mc_eth = mc / eth_usd
        if grad_mc_eth and now_mc_eth:
            multiple = now_mc_eth / grad_mc_eth

        row = {
            "checked_at": now, "token": token,
            "symbol": (p.get("baseToken") or {}).get("symbol") or info["symbol"],
            "chain": p.get("chainId", ""), "dex": p.get("dexId", ""),
            "pair": p.get("pairAddress", ""), "price_usd": price_usd,
            "market_cap_usd": mc,
            "liquidity_usd": num((p.get("liquidity") or {}).get("usd")),
            "volume_24h_usd": num((p.get("volume") or {}).get("h24")),
            "change_24h_pct": num((p.get("priceChange") or {}).get("h24")),
            "grad_mc_eth": round(grad_mc_eth, 3) if grad_mc_eth else "",
            "now_mc_eth": round(now_mc_eth, 3) if now_mc_eth else "",
            "multiple_since_grad": round(multiple, 2) if multiple else "",
        }
        writer.writerow(row)
        rows.append(row)
    out.close()

    print(f"  graduated tokens found on DexScreener: {len(rows)} of {events}"
          f"   chains: {dict(chains)}\n")
    if not rows:
        print("  None found. The tokens may not be indexed, or the addresses")
        print("  in graduations.csv are not token addresses. Send this output.")
        return

    print("  symbol        market cap     liquidity     vol 24h   "
          "24h     since grad")
    print("  " + "-" * 72)
    for r in sorted(rows, key=lambda x: -x["market_cap_usd"]):
        mult = f"{r['multiple_since_grad']}x" if r["multiple_since_grad"] else "-"
        print(f"  {str(r['symbol'])[:12]:<12} ${r['market_cap_usd']:>11,.0f}"
              f"  ${r['liquidity_usd']:>10,.0f}  ${r['volume_24h_usd']:>9,.0f}"
              f"  {r['change_24h_pct']:>+5.0f}%   {mult:>8}")

    alive = sum(1 for r in rows if r["liquidity_usd"] >= 5000)
    print(f"\n  with at least $5k liquidity today: {alive} of {len(rows)}")
    print(f"  saved to {HISTORY_FILE} - run again tomorrow to see the trend")


if __name__ == "__main__":
    main()
