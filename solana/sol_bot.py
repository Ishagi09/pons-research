"""
pump.fun (Solana) recorder and live paper trader - no wallet, no real money.

The same experiment as the Pons research, on Solana's biggest launchpad.
One websocket connection streams every transaction that touches the pump.fun
program. The program's own events (CreateEvent, TradeEvent, CompleteEvent in
its "Program data" logs, decoded with the official IDL layout) give:

  - every new coin, with its starting curve reserves
  - every trade, with the curve's reserves AFTER the trade and the fee rates
    actually charged, so prices come straight from the chain (no curve
    fitting, unlike Pons)
  - every graduation

The bot records SOL-paired coins and their first RECORD_MINUTES of trades to
CSV for later backtests, and paper-trades the STRATEGIES below. They were
fixed before any Solana data was seen; do not tune them on this run.

How a paper trade is priced:
  - the bot decides when it SEES an event; the order lands `react` seconds
    later, at whatever price the curve has by then. Snipers buy in the same
    block as the launch; a home bot cannot, and this is how that is modelled.
  - the coin's own protocol + creator fee, on the buy and on the sell
  - GAS_SOL per transaction (priority fee; token account rent comes back
    when the account is closed after the sell)
  - our own price impact is paid on entry and again on exit (~0.2% extra,
    slightly pessimistic)

Coins paired with anything but SOL (USDC, other pump coins) are skipped:
pricing them as SOL is the mistake that produced fake 72x trades on Pons.

Mayhem coins are recorded as launches but not traded or tracked. On them a
pump.fun program trades without fees, and its trades do not follow the
curve formula (probe_solana.py: x*y=k held on 100% of normal-coin trades and
on under half of mayhem-coin trades), so their prices would not be trusted.

Read-only: nothing is signed or sent. Ctrl+C to stop. A live stream cannot be
replayed, so open positions are not resumed after a restart; stream gaps are
logged in sol_gaps.csv and positions that lived through one are flagged and
left out of the score.

Data source: the free public Solana endpoint. If solana_rpc.txt holds another
RPC URL (e.g. Helius), it is the backup: the bot switches to it when the
public endpoint keeps failing, and back when the backup fails (for example
when its free credits run out).

Setup (once):
    pip install websockets
    solana_rpc.txt - optional, one line with a backup RPC URL, e.g.
                     https://mainnet.helius-rpc.com/?api-key=...
    python probe_solana.py      # one-minute check first

Run:
    python sol_bot.py           # or sol_bot.bat, which restarts it
"""

import asyncio
import base64
import binascii
import csv
import heapq
import json
import os
import re
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict, deque, namedtuple

try:
    import websockets
except ImportError:
    sys.exit("Missing package. Run:  pip install websockets")

# ---------------------------------------------------------------- settings
SIZE_SOL = 0.05            # virtual position, fees included
GAS_SOL = 0.0002           # base + priority fee per transaction (buy or sell)
MIN_BUY_SOL = 0.001        # smaller buys do not count as a buyer
DEFAULT_FEE = 0.0125       # 0.95% protocol + 0.30% creator, until the coin trades
RECORD_MINUTES = 120       # curve trades are recorded this long after a launch
FORGET_MINUTES = 120       # coins with no trade for this long leave memory
STALE_SECONDS = 60         # no message for this long -> reconnect
GOOD_SECONDS = 60          # a connection that streamed this long counts as working
QUICK_FAILS = 2            # this many short-lived connections in a row -> other endpoint
REPORT_SECONDS = 60
FLUSH_SECONDS = 2
TICK_SECONDS = 0.1

Strategy = namedtuple(
    "Strategy", "name entry_after max_buyers min_buyers tp sl hold react")

STRATEGIES = [
    # entry_after: seconds after the bot sees the launch
    # max/min_buyers: outside wallets that bought at least MIN_BUY_SOL by then
    # tp / sl: exit when the position is worth this multiple (before fees)
    # hold: time limit in seconds; react: seconds until our order lands
    #        name                     enter  max   min   tp   sl   hold  react
    Strategy("launch   1.2x 10m fast",    0, None,  0, 1.2, 0.5,  600, 0.5),
    Strategy("launch   1.2x 10m slow",    0, None,  0, 1.2, 0.5,  600, 2.0),
    Strategy("5s 0 buy 1.2x 10m",         5,    0,  0, 1.2, 0.5,  600, 1.0),
    Strategy("5s 0 buy 1.5x 60m",         5,    0,  0, 1.5, 0.5, 3600, 1.0),
    Strategy("5s any   2x/half 60m",      5, None,  0, 2.0, 0.5, 3600, 1.0),
    Strategy("60s 10+  1.3x 10m",        60, None, 10, 1.3, 0.5,  600, 1.0),
]

# ------------------------------------------------------------ pump program
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"
RPC_FILE = "solana_rpc.txt"
COMMITMENT = "confirmed"

# Anchor event discriminators, from pump-fun/pump-public-docs idl/pump.json
CREATE_EVENT = bytes([27, 114, 169, 77, 222, 235, 99, 118])
TRADE_EVENT = bytes([189, 219, 127, 211, 78, 230, 97, 238])
COMPLETE_EVENT = bytes([95, 114, 97, 156, 212, 46, 152, 8])

ZERO_KEY = "1" * 32                                   # Pubkey::default()
WSOL = "So11111111111111111111111111111111111111112"
SOL_QUOTES = {ZERO_KEY, WSOL}                         # SOL-paired coins
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

LAMPORTS = 1_000_000_000
SIZE = SIZE_SOL * LAMPORTS
GAS = GAS_SOL * LAMPORTS
MIN_BUY = MIN_BUY_SOL * LAMPORTS

# ------------------------------------------------------------------ files
LAUNCH_FILE = "sol_launches.csv"
TRADE_FILE = "sol_trades.csv"
GRAD_FILE = "sol_graduations.csv"
GAP_FILE = "sol_gaps.csv"
PAPER_FILE = "sol_paper_trades.csv"
HEARTBEAT_FILE = "sol_heartbeat.txt"

LAUNCH_FIELDS = ["seen_at", "slot", "block_time", "seen_lag_s", "signature",
                 "mint", "dev", "creator", "symbol", "name", "virtual_sol",
                 "virtual_tokens", "real_tokens", "supply", "token_2022",
                 "mayhem", "holder_reward", "creator_fee_bps"]
TRADE_FIELDS = ["seen_at", "slot", "block_time", "mint", "side", "sol",
                "tokens", "user", "virtual_sol", "virtual_tokens", "real_sol",
                "real_tokens", "fee_bps", "creator_fee_bps", "fee",
                "creator_fee", "ix"]
GRAD_FIELDS = ["seen_at", "slot", "block_time", "signature", "mint", "user",
               "minutes_after_launch"]
GAP_FIELDS = ["start", "end", "seconds", "reason"]
PAPER_FIELDS = ["closed_at", "strategy", "mint", "symbol", "opened_at",
                "entry_after_s", "react_s", "buyers_at_entry",
                "dev_sold_at_entry", "fee_b", "mayhem", "entry_mult",
                "multiple", "reason", "hold_s", "net_pct", "gas_sol",
                "pnl_sol", "gap"]


class StreamError(Exception):
    """The RPC answered with an error; reconnect."""


# --------------------------------------------------------------- decoding
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


class Reader:
    """Borsh (Anchor) reader over one event, after its 8-byte discriminator."""

    def __init__(self, data: bytes):
        self.data, self.i = data, 8

    def take(self, n: int) -> bytes:
        j = self.i + n
        if j > len(self.data):
            raise ValueError("event is shorter than its layout")
        chunk = self.data[self.i:j]
        self.i = j
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def flag(self) -> bool:
        return self.take(1)[0] != 0

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def i64(self) -> int:
        return int.from_bytes(self.take(8), "little", signed=True)

    def key(self) -> str:
        return b58(self.take(32))

    def text(self) -> str:
        n = self.u32()
        if n > 2000:
            raise ValueError("implausible string length")
        return self.take(n).decode("utf-8", errors="replace")


def decode_create(data: bytes) -> dict:
    r = Reader(data)
    ev = {"name": r.text(), "symbol": r.text()}
    r.text()                                          # uri
    ev.update(mint=r.key(), curve=r.key(), user=r.key(), creator=r.key(),
              timestamp=r.i64(), vt=r.u64(), vq=r.u64(), rt=r.u64(),
              supply=r.u64(), token_2022=False, mayhem=False,
              holder_reward=False, creator_fee_bps=0, quote_mint=None)
    try:                                              # fields added 2025-26
        ev["token_2022"] = r.key() == TOKEN_2022
        ev["mayhem"] = r.flag()
        r.flag()                                      # cashback (deprecated)
        ev["quote_mint"] = r.key()
        vquote = r.u64()
        ev["creator_fee_bps"] = r.u64()
        ev["holder_reward"] = r.flag()
        r.u8()                                        # depth of the quote chain
        if vquote:
            ev["vq"] = vquote
    except ValueError:
        pass                                          # quote_mint stays None
    return ev


def decode_trade(data: bytes) -> dict:
    r = Reader(data)
    ev = {"mint": r.key(), "sol": r.u64(), "tokens": r.u64(),
          "is_buy": r.flag(), "user": r.key(), "timestamp": r.i64(),
          "vq": r.u64(), "vt": r.u64(), "rq": r.u64(), "rt": r.u64()}
    r.take(32)                                        # fee_recipient
    ev["fee_bps"], ev["fee"] = r.u64(), r.u64()
    ev["creator"] = r.key()
    ev["creator_fee_bps"], ev["creator_fee"] = r.u64(), r.u64()
    ev.update(vq_sol_field=ev["vq"], ix="", mayhem=False, quote_mint=None)
    try:                                              # fields added 2025-26
        r.take(1 + 8 + 8 + 8 + 8)                     # volume tracking
        ev["ix"] = r.text()
        ev["mayhem"] = r.flag()
        r.take(8 * 4)                                 # cashback and buyback
        n = r.u32()                                   # shareholders
        if n > 64:
            raise ValueError("implausible shareholder count")
        r.take(34 * n)                                # pubkey + u16 each
        ev["quote_mint"] = r.key()
        r.u64()                                       # quote_amount
        vquote, rquote = r.u64(), r.u64()
        if vquote:
            ev["vq"], ev["rq"] = vquote, rquote
    except ValueError:
        pass
    return ev


def decode_complete(data: bytes) -> dict:
    r = Reader(data)
    return {"user": r.key(), "mint": r.key(), "curve": r.key(),
            "timestamp": r.i64()}


def pump_events(logs: list):
    """Yield (kind, bytes) for each event the pump program itself logged.

    Other programs write "Program data" lines too, and any Anchor program
    with an event called TradeEvent shares its discriminator. So the call
    stack is followed and only lines logged while pump is running count.
    """
    stack = []
    for line in logs:
        if not isinstance(line, str):
            continue
        if line.startswith("Program data: "):
            if stack and stack[-1] == PUMP:
                try:
                    data = base64.b64decode(line[14:].split(" ", 1)[0],
                                            validate=True)
                except (binascii.Error, ValueError):
                    continue
                head = data[:8]
                if head == TRADE_EVENT:
                    yield "trade", data
                elif head == CREATE_EVENT:
                    yield "create", data
                elif head == COMPLETE_EVENT:
                    yield "complete", data
            continue
        if line.startswith("Log truncated"):
            return
        if not line.startswith("Program ") or line.startswith("Program log:"):
            continue
        parts = line.split()
        if len(parts) == 4 and parts[2] == "invoke":
            stack.append(parts[1])
        elif len(parts) >= 3 and (parts[2] == "success"
                                  or parts[2].startswith("failed")):
            if stack:
                stack.pop()


# -------------------------------------------------------------- connection
def rpc_url() -> tuple:
    """(websocket url, printable label) from solana_rpc.txt or the public RPC."""
    lines = []
    for path in (RPC_FILE, RPC_FILE + ".txt"):    # Windows hides extensions
        try:
            with open(path, encoding="utf-8-sig") as f:
                lines = [ln.strip().strip("\"'") for ln in f if ln.strip()]
            break
        except FileNotFoundError:
            continue
    if not lines:
        return PUBLIC_WS, "public endpoint (no solana_rpc.txt found)"
    text = lines[0]
    if re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text):
        text = "https://mainnet.helius-rpc.com/?api-key=" + text   # bare key
    if text.startswith("https://"):
        url = "wss://" + text[len("https://"):]
    elif text.startswith("http://"):
        url = "ws://" + text[len("http://"):]
    elif text.startswith(("wss://", "ws://")):
        url = text
    else:
        sys.exit(f"{RPC_FILE} should hold one URL that starts with https://")
    return url, mask(url)


def endpoints() -> list:
    """[(url, label)]: the free public endpoint first, solana_rpc.txt as backup."""
    out = [(PUBLIC_WS, "public endpoint")]
    url, label = rpc_url()
    if url != PUBLIC_WS:
        out.append((url, f"backup {label}"))
    return out


def mask(url: str) -> str:
    """Hide the API key, so a screenshot of the output is safe to share."""
    if re.search(r"api[-_]?key=", url):
        return re.sub(r"(api[-_]?key=)([^&]{4})[^&]*", r"\1\2...", url)
    host, _, rest = url.partition("://")[2].partition("/")
    return url.split("://")[0] + "://" + host + ("/..." if rest.strip("/") else "")


def subscribe_message() -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                       "params": [{"mentions": [PUMP]},
                                  {"commitment": COMMITMENT}]})


def connect(url: str):
    return websockets.connect(url, max_size=None, open_timeout=20,
                              ping_interval=20, ping_timeout=60,
                              close_timeout=5)


def error_text(data) -> str:
    """RPCs report errors as a dict, a plain string, or something else."""
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        err = err.get("message", err)
    return str(err)[:160]


def clean(text: str, n: int) -> str:
    return "".join(ch for ch in text if ch.isprintable())[:n]


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


# ------------------------------------------------------------------ state
class Coin:
    __slots__ = ("mint", "symbol", "dev", "seen_m", "vq", "vt", "price0",
                 "fee", "buyers", "dev_sold", "complete", "last_m", "mayhem",
                 "stale", "record_until", "positions", "pending")

    def __init__(self, ev: dict, now_m: float):
        self.mint = ev["mint"]
        self.symbol = clean(ev["symbol"], 12) or "?"
        self.dev = ev["user"]
        self.seen_m = self.last_m = now_m
        self.vq, self.vt = ev["vq"], ev["vt"]
        self.price0 = self.vq / self.vt
        cf = ev["creator_fee_bps"]
        self.fee = (95 + cf) / 10_000 if cf else DEFAULT_FEE
        self.buyers = set()
        self.dev_sold = self.complete = self.stale = False
        self.mayhem = ev["mayhem"]
        self.record_until = now_m + RECORD_MINUTES * 60
        self.positions = []
        self.pending = 0                # decisions / orders not yet run


class Position:
    __slots__ = ("strategy", "coin", "opened_m", "opened_wall", "fee_in",
                 "net_in", "tokens", "buyers", "dev_sold", "entry_mult",
                 "trigger", "gap", "closed")

    def __init__(self, strategy, coin, now_m, now_wall, buyers, dev_sold):
        self.strategy, self.coin = strategy, coin
        self.opened_m, self.opened_wall = now_m, now_wall
        self.fee_in = coin.fee
        self.net_in = SIZE / (1 + coin.fee)           # quote that reaches the curve
        self.tokens = coin.vt * self.net_in / (coin.vq + self.net_in)
        self.buyers, self.dev_sold = buyers, dev_sold
        self.entry_mult = (coin.vq / coin.vt) / coin.price0
        self.trigger = None
        self.gap = coin.stale
        self.closed = False

    def gross(self) -> float:
        """Quote our tokens would fetch from the curve now, before fees."""
        c = self.coin
        return c.vq * self.tokens / (c.vt + self.tokens)


# -------------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.coins = {}
        self.timers = []
        self.seq = 0
        self.live = False
        self.down_since = None
        self.down_reason = "bot was not running"
        self.results = defaultdict(list)          # strategy -> [net, ...]
        self.open = Counter()
        self.skipped = Counter()
        self.count = Counter()
        self.k_err = deque(maxlen=5000)
        self.lag = deque(maxlen=2000)
        self.msgs = self.bytes = 0
        self.last = {"report": time.monotonic(), "flush": 0.0, "beat": 0.0,
                     "msgs": 0, "bytes": 0}
        self.started = time.monotonic()
        self.endpoint = "-"
        self.backup_bytes = 0                     # streamed from the backup RPC
        self.files, self.out = {}, {}
        for name, path, fields in (("launch", LAUNCH_FILE, LAUNCH_FIELDS),
                                   ("trade", TRADE_FILE, TRADE_FIELDS),
                                   ("grad", GRAD_FILE, GRAD_FIELDS),
                                   ("gap", GAP_FILE, GAP_FIELDS),
                                   ("paper", PAPER_FILE, PAPER_FIELDS)):
            if name == "paper":
                self.load_results(path)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            self.files[name] = open(path, "a", newline="", encoding="utf-8")
            self.out[name] = csv.writer(self.files[name])
            if new:
                self.out[name].writerow(fields)
        try:
            with open(HEARTBEAT_FILE) as f:
                self.down_since = float(f.read().strip())
        except (OSError, ValueError):
            pass

    def load_results(self, path: str) -> None:
        """Keep the running score across restarts."""
        if not os.path.exists(path):
            return
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    if row["gap"] != "True":
                        self.results[row["strategy"]].append(
                            float(row["net_pct"]) / 100)
                except (KeyError, TypeError, ValueError):
                    continue

    # ---------------------------------------------------------- messages
    def handle(self, raw: str, now_m: float, now_wall: float) -> None:
        """One websocket message. Raises StreamError when the RPC objects."""
        self.msgs += 1
        self.bytes += len(raw)
        self.run_timers(now_m, now_wall)
        try:
            msg = json.loads(raw)
        except ValueError:
            self.count["unreadable messages"] += 1
            return
        if not isinstance(msg, dict):
            self.count["unreadable messages"] += 1
            return
        if msg.get("method") == "logsNotification":
            try:
                result = msg["params"]["result"]
                self.on_tx(result["value"], result["context"]["slot"],
                           now_m, now_wall)
            except Exception:                     # noqa: BLE001 - never die
                self.bug()
        elif "error" in msg:
            raise StreamError(error_text(msg))
        elif msg.get("id") == 1 and "result" in msg:
            self.went_up(now_wall)

    def on_tx(self, value: dict, slot: int, now_m: float, now_wall: float) -> None:
        if value.get("err") is not None:
            self.count["failed tx"] += 1
            return
        sig = value.get("signature", "")
        for kind, data in pump_events(value.get("logs") or []):
            try:
                if kind == "trade":
                    self.on_trade(decode_trade(data), slot, now_m, now_wall)
                elif kind == "create":
                    self.on_create(decode_create(data), sig, slot, now_m, now_wall)
                else:
                    self.on_complete(decode_complete(data), sig, slot, now_m,
                                     now_wall)
            except (ValueError, KeyError, IndexError, ZeroDivisionError):
                self.count["undecodable events"] += 1

    def on_create(self, ev, sig, slot, now_m, now_wall) -> None:
        self.count["launches"] += 1
        if ev["quote_mint"] is None:
            self.count["launch layout unknown"] += 1
            return
        if ev["quote_mint"] not in SOL_QUOTES:
            self.count["not SOL-paired"] += 1         # USDC or a pump coin
            return
        if ev["vq"] <= 0 or ev["vt"] <= 0 or ev["mint"] in self.coins:
            self.count["odd launches"] += 1
            return
        self.count["SOL launches"] += 1
        lag = now_wall - ev["timestamp"]
        self.lag.append(lag)
        self.out["launch"].writerow([
            f"{now_wall:.3f}", slot, ev["timestamp"], f"{lag:.1f}", sig,
            ev["mint"], ev["user"], ev["creator"], clean(ev["symbol"], 12) or "?",
            clean(ev["name"], 40), ev["vq"], ev["vt"], ev["rt"], ev["supply"],
            ev["token_2022"], ev["mayhem"], ev["holder_reward"],
            ev["creator_fee_bps"]])
        if ev["mayhem"]:
            self.count["mayhem launches"] += 1    # recorded, not traded
            return
        coin = Coin(ev, now_m)
        self.coins[coin.mint] = coin
        for s in STRATEGIES:
            if s.entry_after == 0:
                self.decide(s, coin, now_m)
            else:
                coin.pending += 1
                self.schedule(now_m + s.entry_after, "decide", s, coin)

    def on_trade(self, ev, slot, now_m, now_wall) -> None:
        self.count["trades"] += 1
        coin = self.coins.get(ev["mint"])
        if coin is None or coin.complete:
            return
        q, tok, vq, vt = ev["sol"], ev["tokens"], ev["vq"], ev["vt"]
        if vq <= 0 or vt <= 0:
            self.count["odd trades"] += 1
            return
        if q and tok:            # self-check: x * y must not change on a trade
            pre_q, pre_t = (vq - q, vt + tok) if ev["is_buy"] else (vq + q, vt - tok)
            if pre_q > 0 and pre_t > 0:
                self.k_err.append(abs(pre_q * pre_t / (vq * vt) - 1))
        self.lag.append(now_wall - ev["timestamp"])
        coin.vq, coin.vt = vq, vt
        creator_bps = ev["creator_fee_bps"] if ev["creator"] != ZERO_KEY else 0
        coin.fee = (ev["fee_bps"] + creator_bps) / 10_000
        coin.last_m = now_m
        if q >= MIN_BUY:
            if ev["is_buy"]:
                if ev["user"] != coin.dev:
                    coin.buyers.add(ev["user"])
            elif ev["user"] == coin.dev:
                coin.dev_sold = True
        if now_m <= coin.record_until:
            self.count["recorded trades"] += 1
            self.out["trade"].writerow([
                f"{now_wall:.3f}", slot, ev["timestamp"], coin.mint,
                "buy" if ev["is_buy"] else "sell", q, tok, ev["user"], vq, vt,
                ev["rq"], ev["rt"], ev["fee_bps"], ev["creator_fee_bps"],
                ev["fee"], ev["creator_fee"], ev["ix"]])
        for pos in coin.positions:
            if pos.trigger is None:
                mult = pos.gross() / pos.net_in
                if mult >= pos.strategy.tp:
                    self.trigger(pos, "tp", now_m)
                elif mult <= pos.strategy.sl:
                    self.trigger(pos, "sl", now_m)

    def on_complete(self, ev, sig, slot, now_m, now_wall) -> None:
        self.count["graduations"] += 1
        coin = self.coins.get(ev["mint"])
        minutes = ""
        if coin is not None:
            minutes = round((now_m - coin.seen_m) / 60, 1)
            coin.complete = True
            coin.last_m = now_m
            for pos in list(coin.positions):          # the curve is closed now;
                self.close(pos, "graduated", now_m, now_wall)   # last curve price
        self.out["grad"].writerow([f"{now_wall:.3f}", slot, ev["timestamp"],
                                   sig, ev["mint"], ev["user"], minutes])

    # ------------------------------------------------------------ orders
    def schedule(self, due: float, action: str, *args) -> None:
        self.seq += 1
        heapq.heappush(self.timers, (due, self.seq, action, args))

    def run_timers(self, now_m: float, now_wall: float) -> None:
        """Run everything due by now, at its own due time.

        Called before every message is applied, so an order that lands at
        time T sees exactly the trades that arrived before T.
        """
        while self.timers and self.timers[0][0] <= now_m:
            due, _, action, args = heapq.heappop(self.timers)
            try:
                getattr(self, "do_" + action)(*args, due, now_wall - (now_m - due))
            except Exception:                     # noqa: BLE001 - never die
                self.bug()

    def decide(self, s: Strategy, coin: Coin, now_m: float) -> None:
        if coin.complete:
            self.skipped["graduated before entry"] += 1
            return
        if coin.stale or not self.live:
            self.skipped["stream gap"] += 1
            return
        buyers = len(coin.buyers)
        if s.entry_after > 0 and coin.dev_sold:
            return
        if s.max_buyers is not None and buyers > s.max_buyers:
            return
        if buyers < s.min_buyers:
            return
        coin.pending += 1
        self.schedule(now_m + s.react, "enter", s, coin, buyers, coin.dev_sold)

    def do_decide(self, s, coin, now_m, now_wall) -> None:
        coin.pending -= 1
        self.decide(s, coin, now_m)

    def do_enter(self, s, coin, buyers, dev_sold, now_m, now_wall) -> None:
        coin.pending -= 1
        if coin.complete:
            self.skipped["graduated before entry"] += 1
            return
        if coin.stale or not self.live:
            self.skipped["stream gap"] += 1
            return
        pos = Position(s, coin, now_m, now_wall, buyers, dev_sold)
        coin.positions.append(pos)
        self.open[s.name] += 1
        self.schedule(now_m + s.hold, "timeout", pos)

    def trigger(self, pos: Position, reason: str, now_m: float) -> None:
        pos.trigger = reason
        self.schedule(now_m + pos.strategy.react, "exit", pos)

    def do_timeout(self, pos, now_m, now_wall) -> None:
        if not pos.closed and pos.trigger is None:
            self.trigger(pos, "time", now_m)

    def do_exit(self, pos, now_m, now_wall) -> None:
        if not pos.closed:
            self.close(pos, pos.trigger, now_m, now_wall)

    def close(self, pos: Position, reason: str, now_m: float, now_wall: float) -> None:
        coin, s = pos.coin, pos.strategy
        gross = pos.gross()
        pnl = gross * (1 - coin.fee) - SIZE - 2 * GAS
        net = pnl / SIZE
        pos.closed = True
        coin.positions.remove(pos)
        self.open[s.name] -= 1
        if not pos.gap:
            self.results[s.name].append(net)
        self.out["paper"].writerow([
            stamp(now_wall), s.name, coin.mint, coin.symbol,
            f"{pos.opened_wall:.1f}", round(pos.opened_m - coin.seen_m, 1),
            s.react, pos.buyers, pos.dev_sold, round(pos.fee_in, 4), coin.mayhem,
            round(pos.entry_mult, 4), round(gross / pos.net_in, 4), reason,
            round(now_m - pos.opened_m, 1), round(net * 100, 2),
            f"{2 * GAS_SOL:.6f}", f"{pnl / LAMPORTS:.6f}", pos.gap])

    # ------------------------------------------------------- stream gaps
    def went_down(self, reason: str, now_wall: float) -> None:
        if not self.live:
            return
        self.live = False
        self.down_since, self.down_reason = now_wall, reason
        for coin in self.coins.values():
            coin.stale = True                 # buyer counts now incomplete
            for pos in coin.positions:
                pos.gap = True

    def went_up(self, now_wall: float) -> None:
        if self.live:
            return
        if self.down_since is not None:
            self.out["gap"].writerow([stamp(self.down_since), stamp(now_wall),
                                      round(now_wall - self.down_since),
                                      self.down_reason])
        self.live, self.down_since = True, None
        print(f"  [{time.strftime('%H:%M:%S')}] stream live - pump.fun events "
              f"are coming in")

    # ------------------------------------------------------ housekeeping
    def housekeeping(self, now_m: float, now_wall: float) -> None:
        self.run_timers(now_m, now_wall)
        if now_m - self.last["flush"] >= FLUSH_SECONDS:
            for f in self.files.values():
                f.flush()
            self.last["flush"] = now_m
        if self.live and now_m - self.last["beat"] >= 30:
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(f"{now_wall:.0f}")
            except OSError:
                pass
            self.last["beat"] = now_m
        if now_m - self.last["report"] >= REPORT_SECONDS:
            self.report(now_m)
            self.prune(now_m)

    def prune(self, now_m: float) -> None:
        cutoff = now_m - FORGET_MINUTES * 60
        for mint in [m for m, c in self.coins.items()
                     if not c.positions and not c.pending
                     and (c.last_m < cutoff or (c.complete and now_m - c.last_m > 300))]:
            del self.coins[mint]

    def report(self, now_m: float) -> None:
        span = max(now_m - self.last["report"], 1e-9)
        rate = (self.msgs - self.last["msgs"]) / span
        mb_min = (self.bytes - self.last["bytes"]) / span * 60 / 1e6
        self.last.update(report=now_m, msgs=self.msgs, bytes=self.bytes)
        lag = f"{statistics.median(self.lag):.1f} s" if self.lag else "-"
        kerr = (f"{statistics.median(self.k_err) * 100:.4f}%"
                if self.k_err else "-")
        up = int(now_m - self.started)
        state = (f"LIVE via {self.endpoint.split(' ')[0]}" if self.live
                 else "DOWN, reconnecting")
        c = self.count
        print(f"[{time.strftime('%H:%M:%S')}] {state}  up {up // 3600}h"
              f"{up % 3600 // 60:02d}m  {rate:.0f} msg/s  {mb_min:.1f} MB/min "
              f"(~{mb_min * 1.44:.1f} GB/day)  seen ~{lag} after block  "
              f"curve check {kerr}")
        disk = 0
        for f in self.files.values():
            try:
                disk += os.path.getsize(f.name)
            except OSError:
                pass
        backup = (f"  backup used {self.backup_bytes / 1e6:,.0f} MB "
                  f"(~{self.backup_bytes / 1e6 * 20:,.0f} Helius credits)"
                  if self.backup_bytes else "")
        print(f"  launches {c['SOL launches']} SOL-paired ({c['mayhem launches']} "
              f"mayhem, not traded), {c['not SOL-paired']} other quote  graduated "
              f"{c['graduations']}  trades recorded {c['recorded trades']}  "
              f"coins in memory {len(self.coins)}  data files {disk / 1e6:.0f} MB"
              + backup)
        problems = {k: v for k, v in c.items()
                    if k in ("undecodable events", "launch layout unknown",
                             "odd launches", "odd trades", "unreadable messages",
                             "internal errors") and v}
        skipped = ", ".join(f"{k} {v}" for k, v in self.skipped.items())
        if skipped or problems:
            print(f"  skipped: {skipped or 'none'}"
                  + (f"   problems: {problems}" if problems else ""))
        print("  strategy                 open  closed   win   avg net    total pnl")
        for s in STRATEGIES:
            res = self.results.get(s.name, [])
            if not res:
                print(f"  {s.name:<24} {self.open[s.name]:>5} {0:>7}")
                continue
            print(f"  {s.name:<24} {self.open[s.name]:>5} {len(res):>7}  "
                  f"{sum(1 for n in res if n > 0) / len(res) * 100:>4.0f}%"
                  f"   {statistics.fmean(res) * 100:>+6.1f}%"
                  f"   {sum(res) * SIZE_SOL:>+9.4f} SOL")
        print()

    def bug(self) -> None:
        self.count["internal errors"] += 1
        if self.count["internal errors"] <= 3:      # show the first few only
            traceback.print_exc()

    # -------------------------------------------------------------- run
    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.housekeeping(time.monotonic(), time.time())
            except Exception:                         # noqa: BLE001 - never die
                self.bug()

    async def stream(self) -> None:
        """Stream forever. Stays on an endpoint while it works; after
        QUICK_FAILS short-lived connections in a row it tries the other one."""
        choices = endpoints()
        for _, label in choices:
            print(f"  endpoint: {label}")
        print()
        current, quick_fails, backoff = 0, 0, 2
        while True:
            url, self.endpoint = choices[current]
            first = last = None
            reason = "connection closed by the server"
            try:
                async with connect(url) as ws:
                    await ws.send(subscribe_message())
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(),
                                                         timeout=STALE_SECONDS)
                        except asyncio.TimeoutError:
                            reason = f"no data for {STALE_SECONDS}s"
                            break
                        now = time.monotonic()
                        first = first or now
                        last = now
                        if current:
                            self.backup_bytes += len(raw)
                        self.handle(raw, now, time.time())
            except StreamError as exc:
                reason = f"RPC error: {exc}"
            except Exception as exc:                  # noqa: BLE001 - reconnect
                reason = f"{type(exc).__name__}: {str(exc)[:120]}"
            self.went_down(f"{reason} [{self.endpoint.split(' ')[0]}]", time.time())
            if first is not None and last - first >= GOOD_SECONDS:
                quick_fails, backoff = 0, 2           # it worked for a while
            else:
                quick_fails += 1
                backoff = min(backoff * 2, 60)
            note = f"reconnecting in {backoff}s"
            if quick_fails >= QUICK_FAILS and len(choices) > 1:
                current = (current + 1) % len(choices)
                quick_fails, backoff = 0, 2
                note = f"switching to the {choices[current][1].split(' ')[0]} endpoint"
            print(f"  [{time.strftime('%H:%M:%S')}] stream down ({reason}) - {note}")
            await asyncio.sleep(backoff)

    def shutdown(self) -> None:
        now_m, now_wall = time.monotonic(), time.time()
        for coin in list(self.coins.values()):
            for pos in list(coin.positions):          # kept as a record only
                pos.gap = True
                self.close(pos, "stopped", now_m, now_wall)
        for f in self.files.values():
            f.close()


async def run(bot: Bot) -> None:
    tick = asyncio.create_task(bot.ticker())
    try:
        await bot.stream()
    finally:
        tick.cancel()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")     # coin names have emoji
    print("\npump.fun paper trader - virtual trades at exact curve prices\n")
    print(f"  {len(STRATEGIES)} strategies, {SIZE_SOL} SOL per trade, gas "
          f"{GAS_SOL} SOL per transaction")
    bot = Bot()
    try:
        asyncio.run(run(bot))
    except KeyboardInterrupt:
        pass
    finally:
        bot.shutdown()
        print("\nStopped. Data is in the sol_*.csv files.")


if __name__ == "__main__":
    main()
