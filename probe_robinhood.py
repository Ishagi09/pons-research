"""
Probe Robinhood Chain and the Pons launchpad before building the bot.

Checks, in order:
  1. Which public Robinhood Chain RPC answers from this network.
  2. Real block time (the chain advertises ~100 ms blocks).
  3. How many tokens Pons actually launched in the last few minutes,
     extrapolated to launches per hour.
  4. The raw shape of a TokenLaunched event, and which of its address
     fields is the new token - so the bot decodes real data, not docs.
  5. Whether DexScreener and the Blockscout explorer are reachable.

Read-only JSON-RPC and HTTP GETs. No wallet, no keys, no transactions.

    python probe_robinhood.py
"""

import time

import requests

CHAIN_ID = 4663
RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://rpc.nodeflare.app/robinhood/public",
    "https://robinhood.drpc.org",
]

# Pons V2 launch factory and its TokenLaunched event (hash verified locally
# against the signature TokenLaunched(address,address,address,address,uint256,uint256))
PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOPIC_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"

LOOKBACK_SECONDS = 600         # measure launches over the last 10 minutes
SEL_SYMBOL = "0x95d89b41"      # symbol()
SEL_NAME = "0x06fdde03"        # name()


# ------------------------------------------------------------------- rpc
def rpc(url: str, method: str, params: list, tries: int = 3):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, json=body, timeout=20)
            data = r.json()
            if "result" in data:
                return data["result"], None
            last_error = data.get("error", {}).get("message", str(data)[:120])
            return None, last_error
        except (requests.RequestException, ValueError) as exc:
            last_error = type(exc).__name__
            time.sleep(2 * attempt)
    return None, last_error


def find_rpc() -> str | None:
    print("\n1. Robinhood Chain RPCs\n")
    working = None
    for url in RPCS:
        result, err = rpc(url, "eth_chainId", [], tries=2)
        if result and int(result, 16) == CHAIN_ID:
            print(f"  {url:<45} OK  (chain {CHAIN_ID})")
            working = working or url
        else:
            print(f"  {url:<45} FAILED  ({err})")
    return working


def block(url: str, number) -> dict | None:
    tag = number if isinstance(number, str) else hex(number)
    result, _ = rpc(url, "eth_getBlockByNumber", [tag, False])
    return result


# ------------------------------------------------------------- decoding
def word_to_address(word: str) -> str:
    return "0x" + word[-40:]


def decode_string(hex_result: str) -> str:
    """Decode an ABI-encoded string return value; tolerate bytes32 tokens."""
    raw = bytes.fromhex(hex_result[2:]) if hex_result else b""
    if len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        if 0 < length <= len(raw) - 64:
            return raw[64:64 + length].decode("utf-8", errors="replace")
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def erc20_label(url: str, address: str) -> str | None:
    symbol, _ = rpc(url, "eth_call", [{"to": address, "data": SEL_SYMBOL}, "latest"])
    if not symbol or symbol == "0x":
        return None
    name, _ = rpc(url, "eth_call", [{"to": address, "data": SEL_NAME}, "latest"])
    return f"{decode_string(symbol)} ({decode_string(name) if name else '?'})"


def get_logs(url: str, start: int, end: int) -> list:
    """Fetch logs in chunks, shrinking the chunk if the RPC complains."""
    logs, chunk, cursor = [], 2000, start
    while cursor <= end:
        stop = min(cursor + chunk - 1, end)
        params = [{"address": PONS_FACTORY, "topics": [TOPIC_LAUNCHED],
                   "fromBlock": hex(cursor), "toBlock": hex(stop)}]
        result, err = rpc(url, "eth_getLogs", params)
        if result is None:
            if chunk > 50:
                chunk //= 4
                continue
            print(f"    getLogs failed at {cursor}: {err}")
            return logs
        logs.extend(result)
        cursor = stop + 1
    return logs


# ------------------------------------------------------------------- run
def main() -> None:
    url = find_rpc()
    if not url:
        print("\n  No RPC answered. Send this output - we will try another route.")
        return

    print("\n2. Block time\n")
    latest = block(url, "latest")
    head = int(latest["number"], 16)
    past = block(url, max(head - 1000, 0))
    seconds = int(latest["timestamp"], 16) - int(past["timestamp"], 16)
    block_time = seconds / 1000 if seconds > 0 else 0.1
    print(f"  head block {head}, ~{block_time:.3f}s per block")

    print(f"\n3. Pons launches in the last {LOOKBACK_SECONDS // 60} minutes\n")
    span = int(LOOKBACK_SECONDS / block_time)
    logs = get_logs(url, head - span, head)
    per_hour = len(logs) * 3600 / LOOKBACK_SECONDS
    print(f"  {len(logs)} TokenLaunched events  ->  ~{per_hour:.0f} per hour")

    if not logs:
        print("\n  No launches found. The factory address may have changed.")
        print("  Send this output.")
    else:
        print("\n4. Shape of the newest launch event\n")
        log = logs[-1]
        topics = log["topics"]
        data = log["data"][2:]
        words = [data[i:i + 64] for i in range(0, len(data), 64)]
        print(f"  tx       : {log['transactionHash']}")
        print(f"  indexed  : {len(topics) - 1} topic(s)")
        for i, t in enumerate(topics[1:], 1):
            addr = word_to_address(t)
            print(f"   topic{i} : {addr}   {erc20_label(url, addr) or '(not a token)'}")
        print(f"  data     : {len(words)} word(s)")
        for i, w in enumerate(words):
            as_int = int(w, 16)
            if w.startswith("0" * 24) and as_int > 2 ** 64:
                addr = word_to_address(w)
                print(f"   word{i}  : {addr}   {erc20_label(url, addr) or '(not a token)'}")
            else:
                print(f"   word{i}  : {as_int}")

    print("\n5. Other data sources\n")
    for name, test in [
        ("DexScreener", "https://api.dexscreener.com/latest/dex/search?q=PONS"),
        ("Blockscout", "https://robinhoodchain.blockscout.com/api/v2/stats"),
    ]:
        try:
            r = requests.get(test, timeout=15)
            print(f"  {name:<12} HTTP {r.status_code}")
        except requests.RequestException as exc:
            print(f"  {name:<12} BLOCKED ({type(exc).__name__})")

    print("\nSend this whole output.")


if __name__ == "__main__":
    main()
