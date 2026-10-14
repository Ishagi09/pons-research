"""
One-minute check before starting sol_bot.py. Nothing is recorded or traded.

Listens to pump.fun through the RPC in solana_rpc.txt (or the free public
endpoint) and checks:
  1. the stream works, and how much data it sends per day. Helius' free
     plan has 1M credits a month and streaming costs 20 credits per MB,
     which is about 50 GB a month
  2. pump.fun's events decode with the current layout (official IDL)
  3. every trade's reserves satisfy x * y = k - proof the decoding is right.
     Failures are broken down by coin type and instruction, compared with
     a few explanations, and each coin's trades are checked for continuity
     (does every trade start where the previous one ended?)
  4. fee amounts match the fee rates inside the events
  5. how many wallets buy in the same block a coin launches (snipers)
  6. how late this PC sees trades, compared with their block time

Writes probe_details.txt (raw numbers, no keys) for a closer look.

    python probe_solana.py                 # 60 seconds, RPC from solana_rpc.txt
    python probe_solana.py 120             # longer
    python probe_solana.py 60 public       # the free public Solana endpoint
    python probe_solana.py 60 wss://...    # any other endpoint
"""

import asyncio
import base64
import binascii
import json
import statistics
import sys
import time
from collections import Counter, defaultdict

import sol_bot as sb

HELIUS_FREE_CREDITS = 1_000_000     # per month
CREDITS_PER_MB = 20
DETAILS_FILE = "probe_details.txt"
PUBLIC_ENDPOINTS = ["wss://api.mainnet-beta.solana.com", "wss://api.mainnet.solana.com"]
MAYHEM_PROGRAM = "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"
EVENT_KINDS = {
    sb.TRADE_EVENT: "trade",
    sb.CREATE_EVENT: "create",
    sb.COMPLETE_EVENT: "complete",
    bytes([117, 123, 228, 182, 161, 168, 220, 214]): "mayhem_params",
    bytes([111, 176, 109, 139, 49, 108, 213, 251]): "post_complete_buy",
}
K_TOLERANCE = 1e-6
MAX_FAIL_SAMPLES = 40
MAX_FAIL_LOGS = 4
MAX_TRACES = 12


def all_pump_events(logs: list):
    """Like sol_bot.pump_events, for every event kind the probe looks at."""
    stack = []
    for line in logs:
        if not isinstance(line, str):
            continue
        if line.startswith("Program data: "):
            if stack and stack[-1] == sb.PUMP:
                try:
                    data = base64.b64decode(line[14:].split(" ", 1)[0], validate=True)
                except (binascii.Error, ValueError):
                    continue
                kind = EVENT_KINDS.get(data[:8])
                if kind:
                    yield kind, data
            continue
        if line.startswith("Log truncated"):
            return
        if not line.startswith("Program ") or line.startswith("Program log:"):
            continue
        parts = line.split()
        if len(parts) == 4 and parts[2] == "invoke":
            stack.append(parts[1])
        elif len(parts) >= 3 and (parts[2] == "success" or parts[2].startswith("failed")):
            if stack:
                stack.pop()


def decode_mayhem_params(data: bytes) -> dict:
    r = sb.Reader(data)
    return {"timestamp": r.i64(), "mint": r.key(), "vt": r.u64(), "vq": r.u64(),
            "new_vt": r.u64(), "new_vq": r.u64(), "rt": r.u64(), "rq": r.u64()}


def pre_state(ev: dict) -> tuple:
    q, tok, vq, vt = ev["sol"], ev["tokens"], ev["vq"], ev["vt"]
    return (vq - q, vt + tok) if ev["is_buy"] else (vq + q, vt - tok)


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f" ({part / whole * 100:.0f}%)" if whole else "")


class Probe:
    def __init__(self):
        self.msgs = self.size = self.size_failed = 0
        self.events = Counter()
        self.bad = Counter()
        self.failed = 0
        self.k_err = []
        self.k_by = defaultdict(lambda: [0, 0])          # group -> [checked, ok]
        self.k_by_ix = defaultdict(lambda: [0, 0])
        self.k_by_fee = defaultdict(lambda: [0, 0])
        self.alt = Counter()
        self.q_ratio, self.t_ratio = [], []
        self.fail_samples, self.fail_logs = [], []
        self.cont = defaultdict(lambda: [0, 0])          # group -> [checked, continuous]
        self.last_state = {}
        self.fee_ok = self.fee_checked = 0
        self.rates = Counter()
        self.rates_by = defaultdict(Counter)
        self.quotes = Counter()
        self.ix = Counter()
        self.vq_mismatch = 0
        self.lags = []
        self.launch = {}                       # mint -> (slot, dev, symbol)
        self.mayhem_mints = set()
        self.snipers = defaultdict(set)
        self.dev_buys = {}
        self.creates = Counter()
        self.traces = {}                       # mint -> rows, coins launched during the probe
        self.mayhem_updates = []
        self.agent_trades = Counter()
        self.error = None

    # ------------------------------------------------------------ intake
    def feed(self, raw: str, now_wall: float) -> None:
        self.msgs += 1
        self.size += len(raw)
        msg = json.loads(raw)
        if "error" in msg:
            self.error = sb.error_text(msg)
            return
        if msg.get("method") != "logsNotification":
            return
        result = msg["params"]["result"]
        value, slot = result["value"], result["context"]["slot"]
        if value.get("err") is not None:
            self.failed += 1
            self.size_failed += len(raw)
            return
        logs = value.get("logs") or []
        sig = value.get("signature", "")
        agent = any(MAYHEM_PROGRAM in line for line in logs if isinstance(line, str))
        for kind, data in all_pump_events(logs):
            self.events[kind] += 1
            try:
                if kind == "trade":
                    self.trade(sb.decode_trade(data), slot, now_wall, sig, logs, agent)
                elif kind == "create":
                    self.create(sb.decode_create(data), slot)
                elif kind == "mayhem_params":
                    ev = decode_mayhem_params(data)
                    self.last_state[ev["mint"]] = (ev["new_vq"], ev["new_vt"])
                    if len(self.mayhem_updates) < 50:
                        self.mayhem_updates.append((slot, ev))
                    if ev["mint"] in self.traces:
                        self.traces[ev["mint"]].append(
                            f"  {slot} MAYHEM PARAMS vq {ev['vq']} -> {ev['new_vq']}, "
                            f"vt {ev['vt']} -> {ev['new_vt']}")
                elif kind == "complete":
                    sb.decode_complete(data)
            except (ValueError, KeyError, IndexError, ZeroDivisionError):
                self.bad[kind] += 1

    def create(self, ev: dict, slot: int) -> None:
        quote = ev["quote_mint"]
        if quote is None:
            self.creates["layout unknown"] += 1
            return
        self.creates["SOL-paired" if quote in sb.SOL_QUOTES else "other quote"] += 1
        if ev["mayhem"]:
            self.creates["mayhem"] += 1
            self.mayhem_mints.add(ev["mint"])
        if quote in sb.SOL_QUOTES:
            self.launch[ev["mint"]] = (slot, ev["user"], sb.clean(ev["symbol"], 12))
            self.creates[f"start {ev['vq'] / 1e9:g} SOL x {ev['vt'] / 1e15:g}B tokens"] += 1
            if len(self.traces) < MAX_TRACES:
                self.traces[ev["mint"]] = [
                    f"coin {ev['mint']} {sb.clean(ev['symbol'], 12)} "
                    f"mayhem={ev['mayhem']} supply={ev['supply']} "
                    f"start vq={ev['vq']} vt={ev['vt']} creator_fee_bps={ev['creator_fee_bps']}"]

    def trade(self, ev: dict, slot: int, now_wall: float, sig: str, logs: list,
              agent: bool) -> None:
        q, tok, vq, vt = ev["sol"], ev["tokens"], ev["vq"], ev["vt"]
        sol_quote = ev["quote_mint"] in sb.SOL_QUOTES
        mayhem = ev["mayhem"] or ev["mint"] in self.mayhem_mints
        group = "mayhem coins" if mayhem else "normal coins"
        side = "buy" if ev["is_buy"] else "sell"
        self.quotes["SOL" if sol_quote else
                    "unknown" if ev["quote_mint"] is None else "other"] += 1
        self.ix[ev["ix"] or "?"] += 1
        if agent:
            self.agent_trades[group] += 1
        self.lags.append(now_wall - ev["timestamp"])
        if sol_quote and ev["vq_sol_field"] != vq:
            self.vq_mismatch += 1

        err = None
        if sol_quote and q and tok and vq > 0 and vt > 0:
            pre_q, pre_t = pre_state(ev)
            if pre_q > 0 and pre_t > 0:
                err = abs(pre_q * pre_t / (vq * vt) - 1)
                ok = err < K_TOLERANCE
                self.k_err.append(err)
                fee_zero = ev["fee_bps"] == 0 and ev["creator_fee_bps"] == 0
                for table, key in ((self.k_by, group),
                                   (self.k_by_ix, f"{ev['ix'] or '?'} {side}"),
                                   (self.k_by_fee, "zero-fee trades" if fee_zero
                                    else "fee-paying trades")):
                    table[key][0] += 1
                    table[key][1] += ok
                if not ok:
                    self.explain(ev, slot, sig, logs, agent, err, group)
            prev = self.last_state.get(ev["mint"])
            if prev is not None and pre_q > 0 and pre_t > 0:
                jump = abs(pre_q - prev[0]) / prev[0] + abs(pre_t - prev[1]) / prev[1]
                self.cont[group][0] += 1
                self.cont[group][1] += jump < 1e-9
            self.last_state[ev["mint"]] = (vq, vt)

        if sol_quote and q >= 1_000_000:          # fee rounding matters on dust
            creator_bps = ev["creator_fee_bps"] if ev["creator"] != sb.ZERO_KEY else 0
            want_fee = -(-q * ev["fee_bps"] // 10_000)
            want_creator = -(-q * creator_bps // 10_000)
            self.fee_checked += 1
            self.fee_ok += (abs(ev["fee"] - want_fee) <= 1
                            and abs(ev["creator_fee"] - want_creator) <= 1)
            self.rates[(ev["fee_bps"], creator_bps)] += 1
            self.rates_by[group][(ev["fee_bps"], creator_bps)] += 1

        info = self.launch.get(ev["mint"])
        if info and ev["is_buy"]:
            launch_slot, dev, _ = info
            if ev["user"] == dev:
                self.dev_buys.setdefault(ev["mint"], q)
            elif slot == launch_slot and q >= sb.MIN_BUY:
                self.snipers[ev["mint"]].add(ev["user"])
        rows = self.traces.get(ev["mint"])
        if rows is not None and len(rows) < 150:
            rows.append(f"  {slot} {ev['ix'] or '?'} {side} user={ev['user'][:6]} "
                        f"agent={agent} q={q} tok={tok} vq={vq} vt={vt} "
                        f"fee_bps={ev['fee_bps']}+{ev['creator_fee_bps']} "
                        f"k_err={'-' if err is None else f'{err:.2e}'}")

    def explain(self, ev, slot, sig, logs, agent, err, group) -> None:
        """Which simple explanation fits a trade that breaks x*y=k?"""
        q, tok, vq, vt = ev["sol"], ev["tokens"], ev["vq"], ev["vt"]
        k = vq * vt
        buy = ev["is_buy"]
        fees = ev["fee"] + ev["creator_fee"]
        fits = []
        post_q, post_t = (vq + q, vt - tok) if buy else (vq - q, vt + tok)
        if post_q > 0 and post_t > 0 and abs(post_q * post_t / k - 1) < K_TOLERANCE:
            fits.append("reserves are from BEFORE the trade")
        for label, q_eff in (("quote amount without fees", q - fees),
                             ("quote amount plus fees", q + fees)):
            p_q = vq - q_eff if buy else vq + q_eff
            p_t = vt + tok if buy else vt - tok
            if p_q > 0 and p_t > 0 and abs(p_q * p_t / k - 1) < K_TOLERANCE:
                fits.append(label)
        for f in fits or ["no simple explanation"]:
            self.alt[f] += 1
        # the quote / token amount that WOULD make x*y=k hold
        if buy and vt + tok > 0 and vq - q > 0:
            self.q_ratio.append((vq - k / (vt + tok)) / q)
            self.t_ratio.append((k / (vq - q) - vt) / tok)
        elif not buy and vt - tok > 0:
            self.q_ratio.append((k / (vt - tok) - vq) / q)
            self.t_ratio.append((vt - k / (vq + q)) / tok)
        if len(self.fail_samples) < MAX_FAIL_SAMPLES:
            self.fail_samples.append(
                f"{slot} {sig} {group} {ev['ix'] or '?'} {'buy' if buy else 'sell'} "
                f"agent={agent} q={q} tok={tok} vq={vq} vt={vt} rq={ev['rq']} "
                f"rt={ev['rt']} fee_bps={ev['fee_bps']}+{ev['creator_fee_bps']} "
                f"fee={ev['fee']}+{ev['creator_fee']} k_err={err:.3e} fits={fits}")
        if len(self.fail_logs) < MAX_FAIL_LOGS:
            self.fail_logs.append((sig, [ln for ln in logs if isinstance(ln, str)][:80]))

    # ------------------------------------------------------------ report
    def summary(self, seconds: float, label: str) -> bool:
        lines = []

        def say(text: str = "") -> None:
            print(text)
            lines.append(text)

        say("=" * 72)
        if self.error:
            say(f"RPC ERROR: {self.error}")
        if self.msgs <= 1:
            say("No pump.fun data arrived.")
            say("  - with the public endpoint this is common: use a Helius URL")
            return False
        minutes = seconds / 60
        mb_min = self.size / 1e6 / minutes
        gb_day = mb_min * 1.44
        trades = self.events["trade"]
        say(f"STREAM     {self.msgs / seconds:.1f} messages/s, {mb_min:.2f} MB/min "
            f"-> about {gb_day:.1f} GB per day")
        say(f"           {pct(self.failed, self.msgs)} messages are failed "
            f"transactions, {self.size_failed / max(self.size, 1) * 100:.0f}% of the data")
        credits_day = gb_day * 1000 * CREDITS_PER_MB
        say(f"           on Helius ~{credits_day:,.0f} credits/day if counted "
            f"uncompressed; the free plan ({HELIUS_FREE_CREDITS / 1e6:.0f}M/month) "
            f"lasts ~{HELIUS_FREE_CREDITS / max(credits_day, 1):.1f} days")
        c = self.creates
        say(f"PUMP.FUN   {self.events['create'] / minutes:.1f} launches/min: "
            f"{c['SOL-paired']} SOL-paired, {c['other quote']} other quote, "
            f"{c['mayhem']} mayhem")
        starts = sorted(((k[6:], v) for k, v in c.items() if k.startswith("start ")),
                        key=lambda kv: -kv[1])
        if starts:
            say("           curve starts at: " + ", ".join(f"{k} ({v})" for k, v in starts[:3]))
        say(f"           {trades / seconds:.1f} trades/s, {self.events['complete']} "
            f"graduations, {self.events['mayhem_params']} mayhem price updates")
        if self.agent_trades:
            say(f"           trades in transactions run by the mayhem program: "
                f"{dict(self.agent_trades)}")
        bad = sum(self.bad.values())
        total = sum(self.events.values())
        say(f"DECODING   {total - bad}/{total} events decoded"
            + (f"  (failed: {dict(self.bad)})" if bad else ""))
        say(f"           trade quote: {dict(self.quotes)}   "
            f"instructions: {dict(self.ix.most_common(6))}")
        if self.k_err:
            checked = sum(v[0] for v in self.k_by.values())
            ok = sum(v[1] for v in self.k_by.values())
            say(f"CURVE      x*y=k holds on {pct(ok, checked)} SOL trades")
            for name, table in (("by coin", self.k_by), ("by fee", self.k_by_fee),
                                ("by instruction", self.k_by_ix)):
                say(f"           {name}: " + ", ".join(
                    f"{key} {pct(v[1], v[0])}" for key, v in
                    sorted(table.items(), key=lambda kv: -kv[1][0])[:8]))
            if self.alt:
                say(f"           failing trades fit: {dict(self.alt)}")
            if self.q_ratio:
                qs, ts = sorted(self.q_ratio), sorted(self.t_ratio)
                say(f"           to make x*y=k hold, quote x{statistics.median(qs):.4f} "
                    f"(10-90%: {qs[len(qs) // 10]:.4f}-{qs[len(qs) * 9 // 10]:.4f}) "
                    f"or tokens x{statistics.median(ts):.4f}")
            for group, (n, cont) in sorted(self.cont.items()):
                say(f"           {group}: {pct(cont, n)} trades start exactly where "
                    f"the previous trade ended")
        if self.vq_mismatch:
            say(f"           note: {self.vq_mismatch} SOL trades where the two reserve "
                f"fields differ (the quote field is used)")
        if self.fee_checked:
            say(f"FEES       amounts match the rates on {pct(self.fee_ok, self.fee_checked)} trades")
            for group, rates in sorted(self.rates_by.items()):
                say(f"           {group}: " + ", ".join(
                    f"{p / 100:.2f}%+{cr / 100:.2f}% ({n})"
                    for (p, cr), n in rates.most_common(3)))
        if self.launch:
            counts = [len(self.snipers.get(m, ())) for m in self.launch]
            share = sum(1 for n in counts if n) / len(counts) * 100
            say(f"SNIPERS    {share:.0f}% of launches had outside buyers in the launch "
                f"block itself (median {statistics.median(counts):g}, max {max(counts)})")
            if self.dev_buys:
                say(f"           median dev buy at launch: "
                    f"{statistics.median(self.dev_buys.values()) / 1e9:.3f} SOL")
        if self.lags:
            lag = statistics.median(self.lags)
            say(f"TIMING     trades seen ~{lag:.1f} s after their block time "
                f"(needs a correct PC clock)")
            if lag < -2 or lag > 15:
                say("           this looks like a wrong PC clock - turn on "
                    "'Set time automatically' in Windows settings")
        say("=" * 72)

        normal = self.k_by.get("normal coins", [0, 0])
        problems = []
        if trades < 20:
            problems.append("too few trades - run longer: python probe_solana.py 180")
        if total and bad / total > 0.01:
            problems.append("some events did not decode")
        if normal[0] >= 20 and normal[1] / normal[0] < 0.98:
            problems.append("normal coins break x*y=k")
        if self.creates["layout unknown"]:
            problems.append("launch events have an unknown layout")
        if trades >= 200 and not self.events["create"]:
            problems.append("trades arrive but no launch events were seen")
        if self.error:
            problems.append("the RPC returned an error")
        if problems:
            say("VERDICT: PROBLEM - " + "; ".join(problems))
        else:
            say("VERDICT: OK for normal coins")
        path = DETAILS_FILE.replace(".txt", "_public.txt") if "public" in label else DETAILS_FILE
        say(f"Details written to {path} - send that file.")
        self.write_details(lines, label, path)
        return not problems

    def write_details(self, summary_lines: list, label: str, path: str) -> None:
        out = [f"probe_solana details, {time.strftime('%Y-%m-%d %H:%M:%S')}",
               f"endpoint: {label}", ""] + summary_lines
        out += ["", f"== trades that break x*y=k (first {MAX_FAIL_SAMPLES}) =="]
        out += self.fail_samples or ["none"]
        out += ["", "== full logs of the first failing transactions =="]
        for sig, logs in self.fail_logs:
            out += [f"-- {sig}"] + [f"   {ln}" for ln in logs]
        out += ["", "== every trade of coins launched during the probe =="]
        for rows in self.traces.values():
            out += rows + [""]
        out += ["== mayhem price updates =="]
        for slot, ev in self.mayhem_updates:
            out.append(f"  {slot} {ev['mint']} vq {ev['vq']} -> {ev['new_vq']}, "
                       f"vt {ev['vt']} -> {ev['new_vt']}, rq {ev['rq']}, rt {ev['rt']}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
        except OSError as exc:
            print(f"could not write {path}: {exc}")


def endpoints(argv: list) -> tuple:
    """(seconds, [(url, label), ...]) from the command line."""
    seconds, choice = 60.0, None
    for arg in argv[1:]:
        if arg.lower() == "public":
            choice = "public"
        elif arg.startswith(("wss://", "ws://", "https://", "http://")):
            choice = arg
        else:
            seconds = float(arg)
    if choice == "public":
        return seconds, [(u, f"public endpoint {u}") for u in PUBLIC_ENDPOINTS]
    if choice:
        url = ("wss://" + choice[8:] if choice.startswith("https://") else
               "ws://" + choice[7:] if choice.startswith("http://") else choice)
        return seconds, [(url, sb.mask(url))]
    return seconds, [sb.rpc_url()]


async def listen(seconds: float, choices: list) -> None:
    probe = Probe()
    for url, label in choices:
        print(f"\nendpoint: {label}")
        print(f"listening to pump.fun for {seconds:.0f} seconds...")
        started = time.monotonic()
        try:
            async with sb.connect(url) as ws:
                await ws.send(sb.subscribe_message())
                next_note = started + 10
                while True:
                    left = seconds - (time.monotonic() - started)
                    if left <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=left)
                    except asyncio.TimeoutError:
                        break
                    probe.feed(raw, time.time())
                    if probe.error:
                        break
                    if time.monotonic() >= next_note:
                        print(f"  {time.monotonic() - started:>4.0f}s: {probe.msgs} "
                              f"messages, {probe.events['create']} launches, "
                              f"{probe.events['trade']} trades")
                        next_note += 10
        except Exception as exc:                  # noqa: BLE001 - explain and stop
            print(f"\nCONNECTION FAILED: {type(exc).__name__}: {str(exc)[:200]}")
            if probe.msgs == 0:
                continue                          # try the next endpoint, if any
        probe.summary(max(time.monotonic() - started, 1.0), label)
        return
    print("  - check the URL in solana_rpc.txt and the internet connection")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    seconds, choices = endpoints(sys.argv)
    try:
        asyncio.run(listen(seconds, choices))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
