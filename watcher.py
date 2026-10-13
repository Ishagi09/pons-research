"""
Pons watcher - step 1 of the meme bot.

Records every Pons launch and every bonding-curve trade on Robinhood Chain,
raw, to two CSV files. Nothing is decided here and nothing is traded. The
point is to collect real data first and work out what the numbers mean
from that data, instead of guessing from docs.

  launches.csv   one row per TokenLaunched event
  curve_trades.csv  one row per CurveBuy / CurveSell event

What the probe established (Oct 2026):
  - topic1 of TokenLaunched is the new token.
  - topic2 / topic3 are not tokens; one is the deployer (a wallet), one is
    most likely the token's bonding-curve contract. The watcher records
    whether each one has contract code, which tells them apart.
  - ~258 launches per hour, ~0.103 s blocks.

Read-only JSON-RPC. No wallet, no keys, no transactions.
Run it and leave it open. Ctrl+C to stop; it resumes where it left off.

    python watcher.py
"""

import csv
import json
import os
import time

import threading
from concurrent.futures import ThreadPoolExecutor

import requests

CHAIN_ID = 4663
WORKERS = 8                      # parallel metadata lookups per chunk
RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://rpc.nodeflare.app/robinhood/public",
    "https://robinhood.drpc.org",
]

PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOPIC_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
TOPIC_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
TOPIC_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
TOPIC_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"

SEL_SYMBOL = "0x95d89b41"
SEL_NAME = "0x06fdde03"

POLL_SECONDS = 10
MAX_RANGE = 1500                 # blocks per getLogs call
START_BACK_BLOCKS = 600          # first run starts ~1 minute in the past

STATE_FILE = "watcher_state.json"
LAUNCH_FILE = "launches.csv"
TRADE_FILE = "curve_trades.csv"
GRAD_FILE = "graduations.csv"

LAUNCH_FIELDS = ["block", "est_time", "tx", "token", "symbol", "name",
                 "topic2", "topic2_is_contract", "topic3", "topic3_is_contract",
                 "data0", "data1", "data2", "data_extra"]
TRADE_FIELDS = ["block", "est_time", "tx", "log_index", "curve", "side",
                "topic1", "topic2", "data0", "data1", "data2", "data3",
                "data_extra"]
GRAD_FIELDS = ["block", "est_time", "tx", "topics", "data"]

BAD_LOG = (KeyError, IndexError, TypeError, ValueError, AttributeError)


# ------------------------------------------------------------------- rpc
class Rpc:
    """JSON-RPC client that fails over between public endpoints.

    Each thread keeps its own HTTP session, so connections are reused
    instead of paying a fresh TLS handshake on every call.
    """

    def __init__(self, urls: list[str]):
        self.urls = urls
        self.index = 0
        self.local = threading.local()

    def session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        return self.local.session

    @property
    def url(self) -> str:
        return self.urls[self.index]

    def rotate(self) -> None:
        self.index = (self.index + 1) % len(self.urls)
        print(f"  switching RPC -> {self.url}")

    def call(self, method: str, params: list, tries: int = 6):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = None
        for attempt in range(tries):
            try:
                data = self.session().post(self.url, json=body, timeout=20).json()
            except (requests.RequestException, ValueError) as exc:
                # network trouble, or a reply that is not JSON (an HTML error
                # page, an empty body) - try again, then another endpoint
                data, last = None, type(exc).__name__
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            if data is not None:
                last = error_text(data)
                if method == "eth_getLogs" and is_range_error(last):
                    raise RangeTooBig(last)       # caller shrinks the range
            if attempt % 2 == 1:
                self.rotate()
            time.sleep(min(2 * (attempt + 1), 10))
        raise ConnectionError(f"{method} failed: {last}")


class RangeTooBig(Exception):
    pass


def error_text(data) -> str:
    """Endpoints report errors as a dict, a plain string, or something else."""
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        err = err.get("message", err)
    return str(err)[:160]


def is_range_error(message: str) -> bool:
    text = message.lower()
    return "rate" not in text and any(
        w in text for w in ("range", "too many", "more than", "exceed", "response size"))


# ------------------------------------------------------------ decoding
def words(data_hex: str) -> list[str]:
    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return [raw[i:i + 64] for i in range(0, len(raw), 64)]


def as_address(word: str) -> str:
    return "0x" + word[-40:]


def as_number(word: str) -> str:
    return str(int(word, 16)) if word else ""


def decode_string(result: str | None) -> str:
    if not result or result == "0x":
        return ""
    raw = bytes.fromhex(result[2:])
    if len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        if 0 < length <= len(raw) - 64:
            return raw[64:64 + length].decode("utf-8", errors="replace")
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def clean(text: str) -> str:
    """Token names are attacker-controlled; keep the CSV readable."""
    return "".join(ch for ch in text if ch.isprintable())[:64]


# ------------------------------------------------------------- storage
class Sink:
    def __init__(self, path: str, fields: list[str]):
        new = not os.path.exists(path)
        self.file = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=fields)
        if new:
            self.writer.writeheader()
        self.count = 0

    def write(self, row: dict) -> None:
        self.writer.writerow(row)
        self.count += 1

    def flush(self) -> None:
        self.file.flush()


def load_state() -> int | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f).get("last_block")
    return None


def save_state(block: int) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_block": block}, f)
    os.replace(tmp, STATE_FILE)        # atomic, survives a crash mid-write


# ------------------------------------------------------------- watcher
class Watcher:
    def __init__(self):
        self.rpc = Rpc(RPCS)
        self.launches = Sink(LAUNCH_FILE, LAUNCH_FIELDS)
        self.trades = Sink(TRADE_FILE, TRADE_FIELDS)
        self.grads = Sink(GRAD_FILE, GRAD_FIELDS)
        self.code_cache: dict[str, bool] = {}
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)
        self.head_block = 0
        self.head_time = 0
        self.block_time = 0.103
        self.bad_logs = 0

    def est_time(self, block: int) -> int:
        return int(self.head_time - (self.head_block - block) * self.block_time)

    def is_contract(self, address: str) -> bool:
        if address not in self.code_cache:
            code = self.rpc.call("eth_getCode", [address, "latest"])
            self.code_cache[address] = bool(code and code != "0x")
        return self.code_cache[address]

    def token_text(self, token: str) -> tuple[str, str]:
        try:
            sym = self.rpc.call("eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"], tries=2)
            name = self.rpc.call("eth_call", [{"to": token, "data": SEL_NAME}, "latest"], tries=2)
            return clean(decode_string(sym)), clean(decode_string(name))
        except (ConnectionError, RangeTooBig, ValueError):
            return "", ""

    def logs(self, start: int, end: int, address, topics: list) -> list:
        params = {"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}
        if address:
            params["address"] = address
        return self.rpc.call("eth_getLogs", [params])

    def update_head(self) -> None:
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        self.head_block = int(latest["number"], 16)
        self.head_time = int(latest["timestamp"], 16)

    def process_range(self, start: int, end: int) -> None:
        # Fetch everything first and write only at the end. If any call
        # fails (or the range is too large), nothing from this chunk has
        # been written yet, so the retry cannot create duplicate rows.
        launch_logs = self.logs(start, end, PONS_FACTORY, [TOPIC_LAUNCHED])
        trade_logs = self.logs(start, end, None,
                               [[TOPIC_CURVE_BUY, TOPIC_CURVE_SELL]])
        grad_logs = self.logs(start, end, PONS_FACTORY, [TOPIC_GRADUATED])

        # A malformed log is skipped and counted. Raising instead would make
        # the same chunk fail forever and stop the watcher.
        parsed = []
        for log in launch_logs:
            try:
                t = log["topics"]
                parsed.append((log, t,
                               as_address(t[1]) if len(t) > 1 else "",
                               as_address(t[2]) if len(t) > 2 else "",
                               as_address(t[3]) if len(t) > 3 else ""))
            except BAD_LOG:
                self.bad_logs += 1

        # metadata for all launches in this chunk, fetched in parallel
        tokens = [p[2] for p in parsed if p[2]]
        addrs = list({a for p in parsed for a in (p[3], p[4]) if a})
        texts = dict(zip(tokens, self.pool.map(self.token_text, tokens)))
        codes = dict(zip(addrs, self.pool.map(self.is_contract, addrs)))

        for log, t, token, t2, t3 in parsed:
            symbol, name = texts.get(token, ("", ""))
            try:
                w = words(log["data"])
                block = int(log["blockNumber"], 16)
                row = {
                    "block": block, "est_time": self.est_time(block),
                    "tx": log["transactionHash"], "token": token,
                    "symbol": symbol, "name": name,
                    "topic2": t2, "topic2_is_contract": codes.get(t2, "") if t2 else "",
                    "topic3": t3, "topic3_is_contract": codes.get(t3, "") if t3 else "",
                    "data0": as_number(w[0]) if len(w) > 0 else "",
                    "data1": as_number(w[1]) if len(w) > 1 else "",
                    "data2": as_number(w[2]) if len(w) > 2 else "",
                    "data_extra": " ".join(w[3:]),
                }
            except BAD_LOG:
                self.bad_logs += 1
                continue
            self.launches.write(row)

        side_of = {TOPIC_CURVE_BUY: "buy", TOPIC_CURVE_SELL: "sell"}
        for log in trade_logs:
            try:
                t = log["topics"]
                w = words(log["data"])
                block = int(log["blockNumber"], 16)
                row = {
                    "block": block, "est_time": self.est_time(block),
                    "tx": log["transactionHash"],
                    "log_index": int(log["logIndex"], 16),
                    "curve": log["address"].lower(),
                    "side": side_of.get(t[0], "?"),
                    "topic1": as_address(t[1]) if len(t) > 1 else "",
                    "topic2": as_address(t[2]) if len(t) > 2 else "",
                    "data0": as_number(w[0]) if len(w) > 0 else "",
                    "data1": as_number(w[1]) if len(w) > 1 else "",
                    "data2": as_number(w[2]) if len(w) > 2 else "",
                    "data3": as_number(w[3]) if len(w) > 3 else "",
                    "data_extra": " ".join(w[4:]),
                }
            except BAD_LOG:
                self.bad_logs += 1
                continue
            self.trades.write(row)

        for log in grad_logs:
            try:
                block = int(log["blockNumber"], 16)
                row = {
                    "block": block, "est_time": self.est_time(block),
                    "tx": log["transactionHash"],
                    "topics": " ".join(log["topics"][1:]),
                    "data": log["data"],
                }
            except BAD_LOG:
                self.bad_logs += 1
                continue
            self.grads.write(row)

    def step(self, cursor: int) -> int:
        self.update_head()
        end_target = self.head_block - 2          # small margin for reorgs
        chunk = MAX_RANGE
        start_cursor, started, last_note = cursor, time.time(), time.time()
        while cursor <= end_target:
            # while catching up on a large gap, show progress every 30s
            if time.time() - last_note >= 30 and end_target - start_cursor > 5000:
                done = cursor - start_cursor
                left = end_target - cursor
                rate = done / max(time.time() - started, 1)
                eta = left / rate / 60 if rate > 0 else 0
                print(f"  catching up: {done * 100 / (done + left):5.1f}%   "
                      f"{left} blocks left   ~{eta:.0f} min   "
                      f"launches {self.launches.count}   "
                      f"trades {self.trades.count}")
                last_note = time.time()
            end = min(cursor + chunk - 1, end_target)
            try:
                self.process_range(cursor, end)
            except RangeTooBig:                   # range too large for this RPC
                if chunk <= 20:
                    raise ConnectionError("getLogs refuses even small ranges")
                chunk = max(chunk // 4, 20)
                continue
            for sink in (self.launches, self.trades, self.grads):
                sink.flush()
            save_state(end)
            cursor = end + 1
        return cursor

    def run(self) -> None:
        while True:                       # at boot the network may not be up yet
            try:
                self.update_head()
                break
            except Exception as exc:      # noqa: BLE001 - keep retrying
                print(f"  cannot reach the chain yet ({type(exc).__name__}) "
                      f"- retrying in 30s")
                time.sleep(30)
        saved = load_state()
        cursor = saved + 1 if saved else self.head_block - START_BACK_BLOCKS
        behind = self.head_block - cursor
        print(f"Watching Pons on Robinhood Chain via {self.rpc.url}")
        print(f"Starting at block {cursor} ({behind} blocks behind head)")
        print("Leave this window open. Ctrl+C to stop.\n")

        last_report = time.time()
        while True:
            try:
                cursor = self.step(cursor)
            except ConnectionError as exc:
                print(f"  {exc} - waiting 30s")
                time.sleep(30)
                continue
            except Exception as exc:      # noqa: BLE001 - never die on one bad reply
                print(f"  unexpected {type(exc).__name__}: {str(exc)[:120]} "
                      f"- waiting 30s")
                time.sleep(30)
                continue
            if time.time() - last_report >= 60:
                bad = f"   skipped bad logs {self.bad_logs}" if self.bad_logs else ""
                print(f"  {time.strftime('%H:%M:%S')}  block {cursor - 1}   "
                      f"launches {self.launches.count}   "
                      f"curve trades {self.trades.count}   "
                      f"graduations {self.grads.count}{bad}")
                last_report = time.time()
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        Watcher().run()
    except KeyboardInterrupt:
        print("\nStopped. Run again any time; it resumes from the last block.")
