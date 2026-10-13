"""
Paper-trade Pons rules live - no wallet, no real money.  (version 3)

What changed in version 3:
  - Real curve prices. check_rule.py showed Pons is an exact constant-
    product curve (virtual reserves 1.68 ETH and 1B tokens, 0.00% error on
    97,470 buys). The bot now tracks each curve's reserves from launch and
    prices every buy and sell at the curve's spot price plus our own price
    impact - the price a real bot would get. Version 2 used the last trade's
    average price, which flattered entries right after the dev's buy.
  - Strategies built on what the data showed: only tokens whose second fee
    is at most 1%, dev has not sold, and few or no outside buyers yet.
  - Its own files (paper_trades_v3.csv, paper_state_v3.json), so version 2
    results stay separate.
  - A live self-check: every buy on a tracked curve is compared with the
    curve formula; the report shows the error.
  - 3.1: token-paired launches are skipped. Their curves hold another token,
    not ETH, and treating them as ETH produced fake 20-70x trades.
    Any curve whose reserve leaves the possible ETH range is skipped too.

For every new launch:
  - wait until ENTRY_DELAY seconds after it
  - skip it if the bot saw the launch too late, or nobody has traded yet
  - each strategy whose filters pass makes a virtual buy of SIZE_ETH
  - each sells at its take profit, its stop loss, at graduation, or after
    its time limit

Read-only JSON-RPC. Nothing is signed or sent. Ctrl+C to stop; it resumes
open positions when restarted.

    python paper_trader.py
"""

import csv
import json
import os
import statistics
import time
from collections import defaultdict, deque

import requests

# ---------------------------------------------------------------- settings
ENTRY_DELAY = 5            # seconds after launch
SIZE_ETH = 0.002           # virtual position size
FEE_A = 0.01               # first curve fee
EXTRA_SLIPPAGE = 0.005     # per side, beyond our own modelled price impact
GAS_UNITS = 250_000        # rough gas for one curve trade
LATE_SECONDS = 15          # launch seen later than this -> skipped

# Pons bonding curve, fitted by check_rule.py on the watcher's data
X0 = 1.68e18               # virtual ETH reserve at launch (wei)
Y0 = 1.0e27                # virtual token reserve at launch (1B tokens)
K = X0 * Y0
GRAD_ETH = 4.2e18          # ETH raised at graduation (launch data2)
SIZE = SIZE_ETH * 1e18

STRATEGIES = [
    # name                       take profit, stop loss, hold, max fee, max buyers
    ("low fee 0 buy  1.2x 10m",  1.2, 0.5, 600,  0.01, 0),    # the candidate
    ("low fee 0 buy  1.3x 10m",  1.3, 0.5, 600,  0.01, 0),
    ("low fee 0 buy  1.5x 60m",  1.5, 0.5, 3600, 0.01, 0),
    ("low fee 1 buy  1.2x 10m",  1.2, 0.5, 600,  0.01, 1),
    ("low fee any    1.2x 10m",  1.2, 0.5, 600,  0.01, 999),
    ("any fee any    1.2x 10m",  1.2, 0.5, 600,  1.00, 999),  # old baseline
]

POLL_SECONDS = 1.0
REPORT_SECONDS = 60
GAS_REFRESH_SECONDS = 60
MAX_RANGE = 100            # blocks per getLogs call (~10 s)
MAX_RESUME_BLOCKS = 420_000  # ~12 h; older state is dropped on restart
BAD_LOG = (KeyError, IndexError, TypeError, ValueError, AttributeError,
           ZeroDivisionError)

RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://rpc.nodeflare.app/robinhood/public",
    "https://robinhood.drpc.org",
]
PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOPIC_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
TOPIC_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
TOPIC_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
TOPIC_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
SEL_SYMBOL = "0x95d89b41"
MIN_AMOUNT = 10 ** 13      # trades below this do not count as a buyer or a fee sample

STATE_FILE = "paper_state_v3.json"
TRADES_FILE = "paper_trades_v3.csv"
TRADE_FIELDS = ["closed_at", "strategy", "token", "symbol", "curve",
                "launch_block", "seen_after_s", "entry_after_s",
                "buyers_at_entry", "dev_sold_at_entry", "fee_b",
                "entry_price", "exit_price", "multiple", "reason", "hold_s",
                "net_pct", "gas_eth", "pnl_eth"]


# --------------------------------------------------------------------- rpc
class RangeTooBig(Exception):
    pass


class Rpc:
    def __init__(self):
        self.index = 0
        self.session = requests.Session()

    def call(self, method: str, params: list):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last = None
        for attempt in range(2 * len(RPCS)):
            url = RPCS[self.index % len(RPCS)]
            try:
                data = self.session.post(url, json=body, timeout=15).json()
            except (requests.RequestException, ValueError) as exc:
                # network trouble, or a reply that is not JSON at all
                data, last = None, type(exc).__name__
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            if data is not None:
                last = error_text(data)
                if method == "eth_getLogs" and is_range_error(last):
                    raise RangeTooBig(last)
            self.index += 1
            time.sleep(0.5 * (attempt + 1))
        raise ConnectionError(f"{method} failed on every RPC: {last}")


def error_text(data) -> str:
    """Endpoints report errors as a dict, a plain string, or something else."""
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        err = err.get("message", err)
    return str(err)[:160]


def is_range_error(message: str) -> bool:
    """True when the RPC says the block range or result set is too big.

    Rate limiting ("too many requests", "rate limit") is NOT a range error:
    shrinking the range would only send more requests and fall behind.
    """
    text = message.lower()
    if any(w in text for w in ("rate", "request", "429", "throttl")):
        return False
    return any(w in text for w in ("range", "too many results", "more than",
                                   "exceed", "response size", "too large"))


def words(data_hex: str) -> list[int]:
    raw = data_hex[2:]
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]


def as_address(word: str) -> str:
    return "0x" + word[-40:].lower()


def decode_string(result: str | None) -> str:
    if not result or result == "0x":
        return ""
    raw = bytes.fromhex(result[2:])
    if len(raw) >= 64:
        length = int.from_bytes(raw[32:64], "big")
        if 0 < length <= len(raw) - 64:
            raw = raw[64:64 + length]
    text = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return "".join(ch for ch in text if ch.isprintable())[:20]


# ------------------------------------------------------------------- curve
def buy_price(x: float) -> float:
    """Average price we pay for SIZE ETH when the curve holds x."""
    return x * x / K * (1 + SIZE / x)


def sell_price(x: float) -> float:
    """Average price we get selling a SIZE-sized position into the curve."""
    x = max(x, 1.0)
    return x * x / K * (1 - SIZE / x)


# ------------------------------------------------------------------ trader
class PaperTrader:
    def __init__(self):
        self.rpc = Rpc()
        self.watching = {}                     # curve -> launch info
        self.reserves = {}                     # curve -> virtual ETH reserve (wei)
        self.positions = defaultdict(dict)     # curve -> {strategy: position}
        self.results = defaultdict(list)       # strategy -> [(net, pnl)]
        self.skipped = defaultdict(int)
        self.seen_lags = []
        self.model_err = deque(maxlen=2000)    # curve formula vs actual buys
        self.symbols = {}
        self.gas_eth = 0.0
        self.gas_checked = 0.0
        self.peak_open = 0
        self.head_n = self.head_t = 0
        self.block_time = 0.103
        self.last_block = 0
        self.last_save = 0.0
        self.span = MAX_RANGE
        self.bad_logs = 0
        self.load_results()
        new_file = not os.path.exists(TRADES_FILE) or os.path.getsize(TRADES_FILE) == 0
        self.out = open(TRADES_FILE, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.out, fieldnames=TRADE_FIELDS)
        if new_file:
            self.writer.writeheader()

    # ------------------------------------------------------------ startup
    def load_results(self) -> None:
        """Continue the score across restarts."""
        if not os.path.exists(TRADES_FILE):
            return
        with open(TRADES_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    self.results[row["strategy"]].append(
                        (float(row["net_pct"]) / 100, float(row["pnl_eth"])))
                except (KeyError, TypeError, ValueError):
                    continue

    def start(self) -> None:
        while True:                       # at boot the network may not be up yet
            try:
                self.refresh_head()
                break
            except Exception as exc:      # noqa: BLE001 - keep retrying
                print(f"  cannot reach the chain yet ({type(exc).__name__}) "
                      f"- retrying in 30s")
                time.sleep(30)
        try:
            past = self.rpc.call("eth_getBlockByNumber",
                                 [hex(self.head_n - 10_000), False])
            self.block_time = (self.head_t - int(past["timestamp"], 16)) / 10_000
        except Exception:                 # noqa: BLE001 - keep the default
            pass
        self.last_block = self.head_n
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                gap = self.head_n - state["last_block"]
                if gap <= MAX_RESUME_BLOCKS:
                    self.last_block = state["last_block"]
                    for curve, by_name in state["positions"].items():
                        self.positions[curve] = by_name
                    self.reserves = {c: x for c, x in state.get("reserves", {}).items()
                                     if c in self.positions}
                    print(f"  resuming {self.open_count()} open positions, "
                          f"catching up {gap} blocks")
                else:
                    print(f"  saved state is {gap * self.block_time / 3600:.1f} h "
                          f"old - starting fresh, old open positions dropped")
            except (OSError, ValueError, KeyError):
                print("  could not read the saved state - starting fresh")
        print(f"  head block {self.head_n}, ~{self.block_time:.3f} s per block")
        print(f"  curve: {X0 / 1e18:.2f} ETH x {Y0 / 1e27:.2f}B tokens, "
              f"extra slippage {EXTRA_SLIPPAGE * 100:.1f}% per side\n")

    def save(self) -> None:
        live = {c: x for c, x in self.reserves.items() if c in self.positions}
        state = {"last_block": self.last_block, "positions": self.positions,
                 "reserves": live}
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass                        # retried next loop

    # --------------------------------------------------------------- chain
    def refresh_head(self) -> None:
        head = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        self.head_n = int(head["number"], 16)
        self.head_t = int(head["timestamp"], 16)

    def time_of(self, block: int) -> float:
        return self.head_t - (self.head_n - block) * self.block_time

    def get_logs(self, start: int, end: int) -> list:
        logs, cursor = [], start
        while cursor <= end:
            span = self.span                   # shrinks once if an RPC objects
            stop = min(cursor + span - 1, end)
            params = [{"fromBlock": hex(cursor), "toBlock": hex(stop),
                       "topics": [[TOPIC_LAUNCHED, TOPIC_BUY, TOPIC_SELL,
                                   TOPIC_GRADUATED]]}]
            try:
                logs.extend(self.rpc.call("eth_getLogs", params))
                cursor = stop + 1
                # grow back after a squeeze, so one bad reply cannot leave
                # the trader crawling through the chain a block at a time
                self.span = min(MAX_RANGE, span * 2)
            except RangeTooBig:
                if span == 1:
                    raise ConnectionError("getLogs refuses even one block")
                self.span = max(1, span // 2)
        return logs

    def refresh_gas(self) -> None:
        if time.time() - self.gas_checked < GAS_REFRESH_SECONDS:
            return
        try:
            price = int(self.rpc.call("eth_gasPrice", []), 16)
            self.gas_eth = price * GAS_UNITS / 1e18
        except (ConnectionError, ValueError, TypeError):
            pass
        self.gas_checked = time.time()

    def symbol(self, token: str) -> str:
        if token not in self.symbols:
            try:
                self.symbols[token] = decode_string(self.rpc.call(
                    "eth_call", [{"to": token, "data": SEL_SYMBOL}, "latest"])) or "?"
            except Exception:             # noqa: BLE001 - a name is not worth a crash
                return "?"
        return self.symbols[token]

    # --------------------------------------------------------------- logs
    def on_launch(self, log: dict) -> None:
        t = log["topics"]
        data = words(log["data"])
        if data and data[0] != 0:
            # paired with a token instead of ETH: its curve holds that token,
            # not ETH, so the ETH curve formula would give nonsense prices
            self.skipped["token-paired"] += 1
            return
        block = int(log["blockNumber"], 16)
        lag = (self.head_n - block) * self.block_time
        curve = as_address(t[2])
        self.reserves[curve] = X0
        self.watching[curve] = {
            "token": as_address(t[1]), "launch_block": block,
            "launch_tx": log["transactionHash"], "dev": as_address(t[3]),
            "dev_from_buy": False, "seen_lag": lag, "traded": False,
            "fees": [], "dev_sold": False, "buyers": set(),
        }

    def on_trade(self, log: dict) -> None:
        curve = log["address"].lower()
        if curve not in self.reserves:
            return                             # not a curve we track
        d = (words(log["data"]) + [0, 0, 0, 0])[:4]
        if log["topics"][0] == TOPIC_BUY:
            side, gross, moved = "buy", d[0], d[1]
            flow = gross - d[2] - d[3]         # ETH into the curve
        else:
            side, moved = "sell", d[0]
            gross = d[1] + d[2] + d[3]
            flow = -gross                      # ETH out of the curve
        x = self.reserves[curve]
        if side == "buy" and flow >= MIN_AMOUNT and moved:
            predicted = K / x - K / (x + flow)
            self.model_err.append(abs(predicted / moved - 1))
        self.reserves[curve] = x + flow        # every trade moves the curve
        who = as_address(log["topics"][2]) if len(log["topics"]) > 2 else ""

        watch = self.watching.get(curve)
        if watch and moved and gross >= MIN_AMOUNT:
            # the dev is whoever received the buy made in the launch tx
            if (side == "buy" and not watch["dev_from_buy"]
                    and log["transactionHash"] == watch["launch_tx"]):
                watch["dev"], watch["dev_from_buy"] = who, True
            elif side == "sell" and who == watch["dev"]:
                watch["dev_sold"] = True
            elif side == "buy" and who != watch["dev"]:
                watch["buyers"].add(who)
            watch["traded"] = True
            watch["fees"].append(d[3] / gross)

        now_sell = sell_price(self.reserves[curve])
        for pos in self.positions.get(curve, {}).values():
            pos["price"] = now_sell
            r = now_sell / pos["entry_price"]
            if not pos["trigger"]:
                if r >= pos["tp"]:
                    pos["trigger"] = "tp"
                elif pos["sl"] is not None and r <= pos["sl"]:
                    pos["trigger"] = "sl"

    def on_graduation(self, log: dict) -> None:
        addrs = {as_address(t) for t in log["topics"][1:]}
        for curve, by_name in self.positions.items():
            for pos in by_name.values():
                if curve in addrs or pos["token"] in addrs:
                    pos["trigger"] = pos["trigger"] or "graduated"
        for curve in [c for c, w in self.watching.items()
                      if c in addrs or w["token"] in addrs]:
            del self.watching[curve]

    # ------------------------------------------------------- entry / exit
    def decide_entries(self, now: float) -> None:
        for curve, w in list(self.watching.items()):
            launch_t = self.time_of(w["launch_block"])
            if now < launch_t + ENTRY_DELAY:
                continue
            del self.watching[curve]
            if w["seen_lag"] > LATE_SECONDS:
                self.skipped["seen late"] += 1
                continue
            self.seen_lags.append(w["seen_lag"])
            if not w["traded"]:
                self.skipped["no trades yet"] += 1
                continue
            if w["dev_sold"]:
                self.skipped["dev sold"] += 1
                continue
            x = self.reserves[curve]
            if not X0 * 0.99 <= x <= X0 + GRAD_ETH * 1.05:
                self.skipped["odd curve"] += 1      # not a normal ETH curve
                continue
            fee_b = statistics.median(w["fees"])
            buyers = len(w["buyers"])
            entry = buy_price(x)
            for name, tp, sl, hold, max_fee, max_buyers in STRATEGIES:
                if fee_b > max_fee + 1e-9 or buyers > max_buyers:
                    continue
                self.positions[curve][name] = {
                    "token": w["token"], "launch_block": w["launch_block"],
                    "seen_lag": round(w["seen_lag"], 2),
                    "entry_t": now, "entry_after": round(now - launch_t, 1),
                    "entry_price": entry, "price": sell_price(self.reserves[curve]),
                    "fee_b": fee_b, "buyers": buyers, "dev_sold": False,
                    "tp": tp, "sl": sl, "hold": hold, "trigger": None,
                }
        self.peak_open = max(self.peak_open, self.open_count())
        # stop tracking reserves of curves nobody holds or watches
        for curve in [c for c in self.reserves
                      if c not in self.watching and c not in self.positions]:
            del self.reserves[curve]

    def check_exits(self, now: float) -> None:
        for curve in list(self.positions):
            by_name = self.positions[curve]
            for name in list(by_name):
                pos = by_name[name]
                reason = pos["trigger"]
                if not reason and now >= pos["entry_t"] + pos["hold"]:
                    reason = "time"
                if reason:
                    self.close(curve, name, pos, reason, now)
                    del by_name[name]
            if not by_name:
                del self.positions[curve]

    def close(self, curve: str, name: str, pos: dict, reason: str, now: float) -> None:
        mult = pos["price"] / pos["entry_price"]
        cost = FEE_A + pos["fee_b"] + EXTRA_SLIPPAGE
        net = mult * (1 - cost) ** 2 - 1
        gas = 2 * self.gas_eth
        pnl = SIZE_ETH * net - gas
        self.results[name].append((net, pnl))
        self.writer.writerow({
            "closed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "strategy": name,
            "token": pos["token"], "symbol": self.symbol(pos["token"]),
            "curve": curve, "launch_block": pos["launch_block"],
            "seen_after_s": pos["seen_lag"], "entry_after_s": pos["entry_after"],
            "buyers_at_entry": pos["buyers"], "dev_sold_at_entry": pos["dev_sold"],
            "fee_b": round(pos["fee_b"], 4), "entry_price": pos["entry_price"],
            "exit_price": pos["price"], "multiple": round(mult, 4),
            "reason": reason, "hold_s": round(now - pos["entry_t"]),
            "net_pct": round(net * 100, 2), "gas_eth": f"{gas:.8f}",
            "pnl_eth": f"{pnl:.8f}",
        })
        self.out.flush()

    def open_count(self) -> int:
        return sum(len(v) for v in self.positions.values())

    # --------------------------------------------------------------- loop
    def step(self) -> bool:
        """One poll. Returns True while catching up (no sleep needed)."""
        if self.last_block >= self.head_n - MAX_RANGE:   # near the head
            self.refresh_head()
        self.refresh_gas()
        if self.head_n <= self.last_block:
            return False
        start = self.last_block + 1
        end = min(self.head_n, start + MAX_RANGE - 1)
        logs = []
        for log in self.get_logs(start, end):
            try:
                log["_key"] = (int(log["blockNumber"], 16), int(log["logIndex"], 16))
                logs.append(log)
            except BAD_LOG:
                self.bad_logs += 1
        logs.sort(key=lambda x: x["_key"])
        # A malformed log is skipped and counted. Raising instead would make
        # the same blocks fail forever and stop the trader.
        for log in logs:                       # launches first: a launch tx
            try:                               # can log its buy earlier
                if (log["topics"][0] == TOPIC_LAUNCHED
                        and log["address"].lower() == PONS_FACTORY):
                    self.on_launch(log)
            except BAD_LOG:
                self.bad_logs += 1
        for log in logs:
            try:
                topic = log["topics"][0]
                if topic in (TOPIC_BUY, TOPIC_SELL):
                    self.on_trade(log)
                elif (topic == TOPIC_GRADUATED
                      and log["address"].lower() == PONS_FACTORY):
                    self.on_graduation(log)
            except BAD_LOG:
                self.bad_logs += 1
        now = self.time_of(end)
        self.decide_entries(now)
        self.check_exits(now)
        self.last_block = end
        if time.time() - self.last_save >= 5:
            self.save()
            self.last_save = time.time()
        return end < self.head_n

    def report(self) -> None:
        lag = (f"{statistics.median(self.seen_lags):.1f} s"
               if self.seen_lags else "-")
        skipped = ", ".join(f"{k} {v}" for k, v in self.skipped.items()) or "none"
        behind = self.head_n - self.last_block
        status = (f"CATCHING UP, {behind * self.block_time / 60:.0f} min behind "
                  f"({self.span} blocks per call)"
                  if behind > MAX_RANGE else f"launch seen after ~{lag}")
        model = (f"{statistics.median(self.model_err) * 100:.2f}%"
                 if self.model_err else "-")
        print(f"[{time.strftime('%H:%M:%S')}] block {self.last_block}  "
              f"open {self.open_count()} (peak {self.peak_open})  {status}  "
              f"curve model error {model}  gas/trade {self.gas_eth:.7f} ETH")
        bad = f"   bad logs skipped: {self.bad_logs}" if self.bad_logs else ""
        print(f"  skipped: {skipped}{bad}")
        open_by = defaultdict(int)
        for by_name in self.positions.values():
            for name in by_name:
                open_by[name] += 1
        print("  strategy                  open  closed    win   avg net    total pnl")
        for name, *_ in STRATEGIES:
            res = self.results.get(name, [])
            if not res:
                print(f"  {name:<25} {open_by[name]:>4} {0:>7}")
                continue
            nets = [n for n, _ in res]
            print(f"  {name:<25} {open_by[name]:>4} {len(res):>7}   "
                  f"{sum(1 for n in nets if n > 0) / len(nets) * 100:>3.0f}%"
                  f"   {statistics.fmean(nets) * 100:>+6.1f}%"
                  f"   {sum(p for _, p in res):>+.5f} ETH")
        print()

    def run(self) -> None:
        print("\nPons paper trader v3 - virtual trades at real curve prices\n")
        self.start()
        last_report = 0.0
        while True:
            try:
                busy = self.step()
            except ConnectionError as exc:
                print(f"  RPC trouble: {exc} - retrying")
                time.sleep(5)
                continue
            except Exception as exc:      # noqa: BLE001 - never die on one bad reply
                print(f"  unexpected {type(exc).__name__}: {str(exc)[:120]} "
                      f"- retrying in 10s")
                time.sleep(10)
                continue
            if time.time() - last_report >= REPORT_SECONDS:
                self.report()
                last_report = time.time()
            if not busy:
                time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    trader = PaperTrader()
    try:
        trader.run()
    except KeyboardInterrupt:
        trader.save()
        print("\nStopped. Open positions saved; run again to continue.")
