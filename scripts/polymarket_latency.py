#!/usr/bin/env python
"""How fast can we see a Polymarket trade (and the wallet behind it)? Read-only latency study.

Scope: the crypto "Up or Down" markets (BTC 5m). Nothing here places orders or uses keys:
public GETs on data-api / clob, public Polygon JSON-RPC (HTTP + WSS), public CLOB market
WebSocket. Findings are written up in French in ``docs/research/polymarket_temps_reel.md``.

Sub-commands
------------
``clock``
    Local clock offset against ``https://clob.polymarket.com/time`` (integer seconds) and the
    Cloudflare ``Date`` header, bracketed over requests spread across the second.
``live``
    Runs everything below concurrently for ``--minutes`` (default 10) on the current BTC 5m
    market and the next ones, then writes CSVs + ``summary.json`` to ``--out``:

    * data-api ``/v2/trades?condition=...`` (taker_only=true, limit 1000, first page) polled
      every ~1 s, twice: with a cache-busting query parameter (``busted``) and with a fixed URL
      (``naive``, served by CloudFront with ``Cache-Control: max-age=300``);
    * data-api ``/v2/trades?user=W&taker_only=false`` for a few very active wallets ``W``;
    * Polygon head followed every ~1 s over HTTP (``eth_blockNumber`` + ``eth_getLogs`` on the
      CTF Exchange V2 contracts), and ``eth_subscribe`` (newHeads, logs, pending txs) over WSS;
    * the public CLOB market WebSocket (``last_trade_price`` carries the tx hash, no wallet);
    * the legacy RTDS WebSocket, topic ``activity`` (``trades`` / ``orders_matched``, with wallet);
    * ``eth_getTransactionByHash`` right after a WebSocket trade (is the tx visible before
      it is mined?).
``report``
    Recomputes ``summary.json`` from the CSVs of a previous ``live`` run.
``roles``
    ``taker_only=true`` vs ``false`` on a recently closed market, checked against the on-chain
    ``OrderFilled`` events (who is maker, who is taker, duplicates).
``rpc-limits``
    ``eth_getLogs`` block-range sizes, wallet-filtered ranges, history depth (pruning) and
    burst throughput of the public node.

Usage::

    . .venv/bin/activate
    python scripts/polymarket_latency.py clock
    python scripts/polymarket_latency.py live --minutes 10 --out /tmp/pm_latency
    python scripts/polymarket_latency.py roles --out /tmp/pm_latency
    python scripts/polymarket_latency.py rpc-limits --out /tmp/pm_latency
    python scripts/polymarket_latency.py report --out /tmp/pm_latency

On-chain decoding (CLOB V2, live since 2026-04-28; github.com/Polymarket/ctf-exchange-v2,
``src/exchange/mixins/Events.sol``)::

    OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
                uint8 side, uint256 tokenId, uint256 makerAmountFilled,
                uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)

One event per maker order filled (``maker`` = maker wallet, ``taker`` = taker wallet) plus one
for the taker order (``maker`` = taker wallet, ``taker`` = the exchange contract itself),
followed by ``OrdersMatched``. ``side`` is the side of the order the event describes; BUY:
makerAmount = pUSD, takerAmount = shares; SELL: the reverse (both with 6 decimals).
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import json
import logging
import math
import queue
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests

from tradebot import polymarket as pm
from tradebot.config import CACHE_DIR

log = logging.getLogger("polymarket_latency")

DATA_URL = pm.DATA_URL
CLOB_URL = pm.CLOB_URL
RPC_HTTP = "https://polygon-bor-rpc.publicnode.com"
RPC_WSS = "wss://polygon-bor-rpc.publicnode.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"  # legacy Real-Time Data Socket, topic "activity"
USER_AGENT = "tradebot/0.1 (read-only research)"
DEFAULT_OUT = CACHE_DIR / "polymarket_latency"

# CLOB V2 exchanges on Polygon (docs.polymarket.com/resources/contracts, read 2026-09-25).
CTF_EXCHANGE_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
NEGRISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310f59"
EXCHANGES = {CTF_EXCHANGE_V2: "ctf", NEGRISK_EXCHANGE_V2: "negrisk"}


# ---------------------------------------------------------------------------
# Keccak-256 (Ethereum variant) — tiny pure-Python version, only used for event topics.
# ---------------------------------------------------------------------------
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _keccak_f(a: list[list[int]]) -> list[list[int]]:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data)
    p.append(0x01)
    while len(p) % rate:
        p.append(0)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        blk = p[off : off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(blk[8 * i : 8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


ORDER_FILLED_SIG = "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
ORDERS_MATCHED_SIG = "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)"
TOPIC_ORDER_FILLED = event_topic(ORDER_FILLED_SIG)
TOPIC_ORDERS_MATCHED = event_topic(ORDERS_MATCHED_SIG)
# Self-check against the hash of the empty string and the well-known ERC-20 Transfer topic.
assert keccak256(b"").hex().startswith("c5d2460186f7233c927e7db2dcc703c0")
assert event_topic("Transfer(address,address,uint256)").startswith("0xddf252ad1be2c89b69c2b068")


_ORDER_TUPLE = "(uint256,address,address,uint256,uint256,uint256,uint8,uint8,uint256,bytes32,bytes32,bytes)"
MATCH_ORDERS_SIG = f"matchOrders(bytes32,{_ORDER_TUPLE},{_ORDER_TUPLE}[],uint256,uint256[],uint256,uint256[])"
MATCH_ORDERS_SELECTOR = event_topic(MATCH_ORDERS_SIG)[:10]  # 0x3c2b4399


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _addr_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def decode_order_filled(lg: dict) -> dict:
    """Decode a CLOB V2 ``OrderFilled`` log (raw JSON-RPC log object)."""
    data = lg["data"][2:]
    words = [int(data[64 * i : 64 * (i + 1)], 16) for i in range(7)]
    side = "BUY" if words[0] == 0 else "SELL"
    maker_amt, taker_amt, fee = words[2], words[3], words[4]
    usdc, shares = (maker_amt, taker_amt) if side == "BUY" else (taker_amt, maker_amt)
    exchange = lg["address"].lower()
    counterparty = _addr(lg["topics"][3])
    ts = lg.get("blockTimestamp")
    return {
        "block": int(lg["blockNumber"], 16),
        "block_ts": int(ts, 16) if ts else None,
        "tx_hash": lg["transactionHash"].lower(),
        "log_index": int(lg["logIndex"], 16),
        "exchange": EXCHANGES.get(exchange, exchange),
        "order_hash": lg["topics"][1],
        "wallet": _addr(lg["topics"][2]),  # the order's maker = funding (proxy) wallet
        "counterparty": counterparty,
        "role": "taker" if counterparty == exchange else "maker",
        "side": side,
        "token_id": str(words[1]),
        "shares": shares / 1e6,
        "usdc": usdc / 1e6,
        "price": usdc / shares if shares else math.nan,
        "fee": fee / 1e6,
        "builder": f"0x{words[5]:064x}",
    }


def decode_match_orders(input_hex: str) -> dict | None:
    """Wallets inside a (possibly still pending) ``matchOrders`` call; None for other calls.

    ABI layout: conditionId, offset(takerOrder), offset(makerOrders[]), takerFillAmount, ...;
    each Order tuple is ``salt, maker, signer, tokenId, makerAmount, takerAmount, side,
    signatureType, timestamp(ms), metadata, builder, offset(signature)``.
    """
    if not input_hex or not input_hex.startswith(MATCH_ORDERS_SELECTOR):
        return None
    a = bytes.fromhex(input_hex[10:])

    def word(off: int) -> int:
        return int.from_bytes(a[off : off + 32], "big")

    def order(off: int) -> dict:
        return {
            "maker": "0x" + a[off + 44 : off + 64].hex(),
            "token_id": str(word(off + 96)),
            "side": "BUY" if word(off + 192) == 0 else "SELL",
            "maker_amount": word(off + 128) / 1e6,
            "taker_amount": word(off + 160) / 1e6,
            "order_ts_ms": word(off + 256),
        }

    try:
        taker = order(word(32))
        m_off = word(64)
        n = word(m_off)
        base = m_off + 32
        makers = [order(base + word(base + 32 * i)) for i in range(min(n, 64))]
    except (ValueError, IndexError):
        return None
    return {
        "condition_id": "0x" + a[:32].hex(),
        "taker_wallet": taker["maker"],
        "taker_side": taker["side"],
        "token_id": taker["token_id"],
        "taker_fill": word(96) / 1e6,
        "taker_order_ts_ms": taker["order_ts_ms"],
        "n_makers": n,
        "maker_wallets": ";".join(sorted({m["maker"] for m in makers})),
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def describe(x: Any) -> dict:
    """n, min, p10, median, p90, max, mean of a numeric vector (NaN dropped)."""
    a = np.asarray(pd.to_numeric(pd.Series(x), errors="coerce").dropna(), dtype="float64")
    if not len(a):
        return {"n": 0}
    q = np.percentile(a, [10, 50, 90])
    return {
        "n": int(len(a)),
        "min": round(float(a.min()), 3),
        "p10": round(float(q[0]), 3),
        "median": round(float(q[1]), 3),
        "p90": round(float(q[2]), 3),
        "max": round(float(a.max()), 3),
        "mean": round(float(a.mean()), 3),
    }


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


class RpcError(RuntimeError):
    def __init__(self, code: Any, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Rpc:
    """Minimal JSON-RPC over HTTP (one instance per thread: requests.Session is not thread-safe)."""

    def __init__(self, url: str = RPC_HTTP, timeout: float = 30.0):
        self.url, self.timeout = url, timeout
        self.s = _session()
        self.calls = 0
        self.errors: Counter = Counter()
        self.last_bytes = 0

    def call(self, method: str, params: list) -> Any:
        self.calls += 1
        try:
            r = self.s.post(
                self.url, json={"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.errors[type(exc).__name__] += 1
            raise RpcError("network", str(exc)) from exc
        self.last_bytes = len(r.content)
        if r.status_code != 200:
            self.errors[f"http_{r.status_code}"] += 1
            raise RpcError(f"http_{r.status_code}", r.text[:200])
        j = r.json()
        if "error" in j:
            err = j["error"] or {}
            self.errors[str(err.get("code"))] += 1
            raise RpcError(err.get("code"), str(err.get("message")))
        return j["result"]

    def head(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, number: int) -> int | None:
        b = self.call("eth_getBlockByNumber", [hex(number), False])
        return int(b["timestamp"], 16) if b else None

    def block_at(self, ts: int, lo: int | None = None) -> int:
        """Last block with timestamp <= ts (binary search)."""
        hi = self.head()
        lo = lo if lo is not None else hi - 100_000
        if (self.block_ts(lo) or 0) > ts:
            return lo
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if (self.block_ts(mid) or 0) <= ts:
                lo = mid
            else:
                hi = mid
        return lo

    def fills(self, b0: int, b1: int, chunk: int = 200, **flt: Any) -> list[dict]:
        """Decoded OrderFilled logs of both V2 exchanges in [b0, b1], optional topic filters."""
        # topics: [event, orderHash, maker, taker]
        topics: list = [TOPIC_ORDER_FILLED, None, None, None]
        if flt.get("maker"):
            topics[2] = _addr_topic(flt["maker"])
        if flt.get("taker"):
            topics[3] = _addr_topic(flt["taker"])
        while topics and topics[-1] is None:
            topics.pop()
        out: list[dict] = []
        for lo in range(b0, b1 + 1, chunk):
            hi = min(b1, lo + chunk - 1)
            logs = self.call("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi), "address": list(EXCHANGES),
                                              "topics": topics}])
            out.extend(decode_order_filled(lg) for lg in logs)
        return out


def fetch_trades(session: requests.Session, max_pages: int = 50, **params: Any) -> list[dict]:
    """All pages of data-api ``/v2/trades`` (cache-busted)."""
    rows: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        p = {k: v for k, v in params.items() if v is not None}
        p["_"] = str(time.time_ns())
        if cursor:
            p["cursor"] = cursor
        else:
            p.setdefault("limit", 1000)
        r = session.get(f"{DATA_URL}/v2/trades", params=p, timeout=30)
        r.raise_for_status()
        j = r.json()
        rows.extend(j.get("data") or [])
        pg = j.get("pagination") or {}
        if not pg.get("has_more") or not pg.get("next_cursor"):
            break
        cursor = pg["next_cursor"]
    return rows


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------
def clock_check(n: int = 12) -> dict:
    """Offset ``local − server`` (s) bracketed from integer-second server clocks.

    If a request is sent at local ``t0``, answered at ``t1`` and the server reports second ``s``
    (floor of its time at processing), then ``t0 − (s + 1) < offset <= t1 − s``. Requests are
    started at evenly spread phases of the second so that the intersection of the brackets is
    only about one round-trip wide. Two independent sources: CLOB ``/time`` and the ``Date``
    header written by Cloudflare's edge.
    """
    s = _session()
    lo_c, hi_c, lo_d, hi_d = -math.inf, math.inf, -math.inf, math.inf
    rtts = []
    for i in range(n):
        target = i / n
        frac = time.time() % 1.0
        time.sleep((target - frac) % 1.0)
        t0 = time.time()
        try:
            r = s.get(f"{CLOB_URL}/time", timeout=10)
        except requests.RequestException:
            continue
        t1 = time.time()
        rtts.append(t1 - t0)
        try:
            srv = int(float(r.text.strip()))
            lo_c, hi_c = max(lo_c, t0 - srv - 1), min(hi_c, t1 - srv)
        except ValueError:
            pass
        date = r.headers.get("Date")
        if date:
            d = email.utils.parsedate_to_datetime(date).timestamp()
            lo_d, hi_d = max(lo_d, t0 - d - 1), min(hi_d, t1 - d)
    lo, hi = max(lo_c, lo_d), min(hi_c, hi_d)
    return {
        "measured_at": round(time.time(), 3),
        "n": len(rtts),
        "rtt_median_s": round(float(np.median(rtts)), 3) if rtts else None,
        "clob_time_offset_s": [round(lo_c, 3), round(hi_c, 3)],
        "date_header_offset_s": [round(lo_d, 3), round(hi_d, 3)],
        "offset_bracket_s": [round(lo, 3), round(hi, 3)],
        "offset_mid_s": round((lo + hi) / 2, 3) if lo <= hi else None,
        "consistent": bool(lo <= hi),
        "note": "offset = local clock − server clock; positive = local clock ahead",
    }


# ---------------------------------------------------------------------------
# Live collectors
# ---------------------------------------------------------------------------
class Store:
    """Thread-safe bag of row lists, counters and numeric samples."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tables: dict[str, list[dict]] = defaultdict(list)
        self.counters: dict[str, Counter] = defaultdict(Counter)
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.meta: dict[str, Any] = {}

    def add(self, table: str, row: dict) -> None:
        with self.lock:
            self.tables[table].append(row)

    def count(self, name: str, key: str, n: int = 1) -> None:
        with self.lock:
            self.counters[name][key] += n

    def sample(self, name: str, value: float) -> None:
        with self.lock:
            self.samples[name].append(value)


def _sleep_until(t: float, stop: threading.Event) -> None:
    dt = t - time.time()
    if dt > 0:
        stop.wait(dt)


def poll_data_api(store: Store, stop: threading.Event, t_end: float, name: str,
                  params_fn: Callable[[], dict], interval: float = 1.0) -> None:
    """Poll ``/v2/trades`` (first page) and record when each trade first appears.

    Trades already present in the first successful answer are the baseline (not timed); the
    baseline is taken again whenever the ``condition``/``user`` parameter changes.
    """
    s = _session()
    seen: set = set()
    first = True
    shape = None
    i = 0
    next_t = time.time()
    while not stop.is_set() and time.time() < t_end:
        params = params_fn()
        if (params.get("condition"), params.get("user")) != shape:
            shape, first = (params.get("condition"), params.get("user")), True
        t0 = time.time()
        rows: list[dict] = []
        status, headers, err = -1, {}, ""
        try:
            r = s.get(f"{DATA_URL}/v2/trades", params=params, timeout=10)
            status, headers = r.status_code, r.headers
            if status == 200:
                rows = r.json().get("data") or []
        except (requests.RequestException, ValueError) as exc:
            err = type(exc).__name__
        t1 = time.time()
        store.add(f"polls_{name}", {
            "poll": i, "t_sent": t0, "t_recv": t1, "status": status, "error": err,
            "x_cache": headers.get("x-cache", ""), "age": headers.get("age", ""),
            "n_rows": len(rows), "max_ts": max((int(x.get("timestamp", 0)) for x in rows), default=None),
        })
        if status == 200:
            occ: Counter = Counter()
            for row in rows:
                k0 = (row.get("transaction_hash"), row.get("token_id"), row.get("proxy_wallet"), row.get("side"),
                      row.get("size"), row.get("price"), row.get("timestamp"))
                occ[k0] += 1
                key = (*k0, occ[k0])
                if key in seen:
                    continue
                seen.add(key)
                if first:
                    continue
                store.add(f"trades_{name}", {
                    "t_seen": t1, "t_sent": t0, "poll": i, "timestamp": int(row.get("timestamp", 0)),
                    "user_param": params.get("user") or "",
                    "tx_hash": str(row.get("transaction_hash", "")).lower(),
                    "wallet": str(row.get("proxy_wallet", "")).lower(), "side": row.get("side"),
                    "outcome": row.get("outcome"), "token_id": str(row.get("token_id")),
                    "size": row.get("size"), "price": row.get("price"), "slug": row.get("slug"),
                })
            first = False
        elif status == 429:
            retry = pm._f(headers.get("Retry-After"))
            stop.wait(min(retry if np.isfinite(retry) else 2.0, 10.0))
        i += 1
        next_t += interval
        if next_t < time.time():
            next_t = time.time()
        _sleep_until(next_t, stop)


def follow_chain_http(store: Store, stop: threading.Event, t_end: float, tokens: dict[str, tuple[str, str]],
                      wallets: set[str], interval: float = 1.0) -> None:
    """Follow the Polygon head over HTTP: eth_blockNumber every ``interval`` s, then eth_getLogs.

    Short timeout: a stuck request is abandoned and simply retried at the next tick.
    """
    rpc = Rpc(timeout=5)
    try:
        last = rpc.head()
    except RpcError as exc:
        store.add("errors", {"component": "chain_http", "error": str(exc)})
        return
    head_seen: dict[int, float] = {}
    retried: set[int] = set()
    next_t = time.time()
    while not stop.is_set() and time.time() < t_end:
        t_ask = time.time()
        try:
            head = rpc.head()
        except RpcError as exc:
            store.count("chain_http_errors", f"blockNumber {exc.code}: {exc.message[:80]}")
            next_t = time.time() + interval
            _sleep_until(next_t, stop)
            continue
        t_head = time.time()
        store.sample("rpc_blocknumber_rtt", t_head - t_ask)
        for b in range(last + 1, head + 1):
            head_seen.setdefault(b, t_head)
        if head > last:
            try:
                logs = rpc.call("eth_getLogs", [{
                    "fromBlock": hex(last + 1), "toBlock": hex(head), "address": list(EXCHANGES),
                    "topics": [[TOPIC_ORDER_FILLED, TOPIC_ORDERS_MATCHED]],
                }])
            except RpcError as exc:
                store.count("chain_http_errors", f"getLogs {exc.code}: {exc.message[:80]}")
                logs = None
            t_logs = time.time()
            if logs is not None:
                per_block: dict[int, list[dict]] = defaultdict(list)
                for lg in logs:
                    per_block[int(lg["blockNumber"], 16)].append(lg)
                upto = head
                if not per_block.get(head) and head not in retried:
                    # head block's logs possibly not indexed yet: re-ask next round
                    retried.add(head)
                    upto = head - 1
                for b in range(last + 1, upto + 1):
                    blk = per_block.get(b, [])
                    ts = int(blk[0]["blockTimestamp"], 16) if blk and blk[0].get("blockTimestamp") else None
                    if ts is None:
                        try:
                            ts = rpc.block_ts(b)
                        except RpcError:
                            ts = None
                    n_filled = sum(1 for lg in blk if lg["topics"][0] == TOPIC_ORDER_FILLED)
                    store.add("chain_blocks", {"block": b, "block_ts": ts, "t_head_seen": head_seen.get(b),
                                               "t_logs_seen": t_logs, "n_order_filled": n_filled,
                                               "n_logs": len(blk), "retried": b in retried})
                    for lg in blk:
                        if lg["topics"][0] != TOPIC_ORDER_FILLED:
                            continue
                        f = decode_order_filled(lg)
                        if f["token_id"] in tokens or f["wallet"] in wallets or f["counterparty"] in wallets:
                            slug, outcome = tokens.get(f["token_id"], ("", ""))
                            store.add("chain_fills", {**f, "slug": slug, "outcome": outcome, "t_seen": t_logs,
                                                      "source": "http"})
                last = upto
        next_t += interval
        if next_t < time.time():
            next_t = time.time()
        _sleep_until(next_t, stop)
    store.meta["rpc_http_calls"] = rpc.calls
    store.meta["rpc_http_errors"] = dict(rpc.errors)


def probe_mempool(store: Store, stop: threading.Event, t_end: float, q: queue.Queue,
                  max_probes: int = 80, min_gap: float = 1.5) -> None:
    """eth_getTransactionByHash on the freshest WebSocket tx hash: visible before being mined?"""
    rpc = Rpc(timeout=10)
    n = 0
    while not stop.is_set() and time.time() < t_end and n < max_probes:
        try:
            h, t_ws = q.get(timeout=0.5)
        except queue.Empty:
            continue
        while True:  # keep only the most recent hash
            try:
                h, t_ws = q.get_nowait()
            except queue.Empty:
                break
        t0 = time.time()
        res, err = None, ""
        try:
            res = rpc.call("eth_getTransactionByHash", [h])
        except RpcError as exc:
            err = str(exc.code)
        t1 = time.time()
        dec = decode_match_orders((res or {}).get("input", "")) or {}
        store.add("mempool", {
            "tx_hash": h, "t_ws": t_ws, "t_query": t0, "t_answer": t1, "error": err,
            "found": res is not None, "pending": bool(res) and res.get("blockNumber") is None,
            "block": int(res["blockNumber"], 16) if res and res.get("blockNumber") else None,
            "to": (res or {}).get("to"), "from": (res or {}).get("from"),
            "taker_wallet": dec.get("taker_wallet"), "maker_wallets": dec.get("maker_wallets"),
        })
        n += 1
        _sleep_until(time.time() + min_gap, stop)


async def market_ws(store: Store, stop: threading.Event, t_end: float, tokens: dict[str, tuple[str, str]],
                    probe_q: queue.Queue) -> None:
    """Public CLOB market channel: event counts, transport lag, last_trade_price rows."""
    import websockets

    seen_tx: set[str] = set()
    while not stop.is_set() and time.time() < t_end:
        try:
            async with websockets.connect(MARKET_WS, max_size=None, open_timeout=15) as ws:
                await ws.send(json.dumps({"assets_ids": list(tokens), "type": "market"}))
                store.count("ws_connections", "market")
                last_ping = time.time()
                while not stop.is_set() and time.time() < t_end:
                    if time.time() - last_ping >= 10:
                        await ws.send("PING")
                        last_ping = time.time()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    t = time.time()
                    if msg in ("PONG", b"PONG"):
                        continue
                    try:
                        data = json.loads(msg)
                    except ValueError:
                        store.count("ws_events", "non_json")
                        continue
                    for it in data if isinstance(data, list) else [data]:
                        et = str(it.get("event_type", "?"))
                        store.count("ws_events", et)
                        ts = pm._f(it.get("timestamp"))
                        if np.isfinite(ts):
                            store.sample(f"ws_lag_{et}", t - ts / 1000.0)
                        if et == "last_trade_price":
                            h = str(it.get("transaction_hash") or "").lower()
                            slug, outcome = tokens.get(str(it.get("asset_id")), ("", ""))
                            store.add("ws_trades", {
                                "t_recv": t, "ts_ms": ts, "tx_hash": h, "token_id": str(it.get("asset_id")),
                                "slug": slug, "outcome": outcome, "side": it.get("side"), "price": it.get("price"),
                                "size": it.get("size"), "fee_rate_bps": it.get("fee_rate_bps"),
                                "keys": ",".join(sorted(it)),
                            })
                            if h and h not in seen_tx:
                                seen_tx.add(h)
                                probe_q.put((h, t))
        except Exception as exc:  # network hiccup: reconnect
            store.add("errors", {"component": "market_ws", "error": repr(exc)[:300], "t": time.time()})
            await asyncio.sleep(1.0)


async def chain_wss(store: Store, stop: threading.Event, t_end: float, tokens: dict[str, tuple[str, str]],
                    wallets: set[str]) -> None:
    """eth_subscribe newHeads / logs / newPendingTransactions on the public WSS endpoint."""
    import websockets

    subs: dict[str, str] = {}
    kinds = {1: "heads", 2: "logs", 3: "pending"}
    log_blocks: dict[int, dict] = {}
    pending_seen: dict[str, float] = {}
    while not stop.is_set() and time.time() < t_end:  # the public endpoint drops sockets: reconnect
        subs.clear()
        try:
            async with websockets.connect(RPC_WSS, max_size=None, open_timeout=15) as ws:
                store.count("ws_connections", "rpc_wss")
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe", "params": [
                    "logs", {"address": list(EXCHANGES), "topics": [TOPIC_ORDER_FILLED]}]}))
                # ``true`` = full transaction objects (supported by publicnode): calldata readable at once
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "eth_subscribe",
                                          "params": ["newPendingTransactions", True]}))
                while not stop.is_set() and time.time() < t_end:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    t = time.time()
                    j = json.loads(msg)
                    if "id" in j and j.get("id") in kinds:
                        kind = kinds[j["id"]]
                        if "error" in j:
                            store.meta[f"wss_subscribe_{kind}"] = f"error: {j['error']}"
                        else:
                            subs[str(j.get("result"))] = kind
                            store.meta[f"wss_subscribe_{kind}"] = "ok"
                        continue
                    p = j.get("params") or {}
                    kind = subs.get(str(p.get("subscription")))
                    res = p.get("result")
                    if kind == "heads":
                        store.add("wss_heads", {"block": int(res["number"], 16), "block_ts": int(res["timestamp"], 16),
                                                "t_recv": t})
                    elif kind == "logs":
                        bn = int(res["blockNumber"], 16)
                        e = log_blocks.setdefault(bn, {"block": bn, "t_first": t, "t_last": t, "n": 0})
                        e["t_last"], e["n"] = t, e["n"] + 1
                        f = decode_order_filled(res)
                        if f["token_id"] in tokens or f["wallet"] in wallets or f["counterparty"] in wallets:
                            slug, outcome = tokens.get(f["token_id"], ("", ""))
                            store.add("chain_fills", {**f, "slug": slug, "outcome": outcome, "t_seen": t, "source": "wss"})
                    elif kind == "pending":
                        store.count("wss_pending", "tx")
                        if isinstance(res, str):  # hash-only fallback
                            pending_seen.setdefault(res.lower(), t)
                            continue
                        if str(res.get("to", "")).lower() not in EXCHANGES:
                            continue
                        store.count("wss_pending", "to_exchange")
                        dec = decode_match_orders(res.get("input", ""))
                        if dec is None:
                            store.count("wss_pending", "exchange_other_call")
                            continue
                        store.add("pending_matches", {"tx_hash": str(res.get("hash", "")).lower(), "t_recv": t,
                                                      "from": res.get("from"), **dec})
        except Exception as exc:
            store.add("errors", {"component": "chain_wss", "error": repr(exc)[:300], "t": time.time()})
            await asyncio.sleep(1.0)
    for e in log_blocks.values():
        store.add("wss_log_blocks", e)
    for h, t in pending_seen.items():
        store.add("wss_pending", {"tx_hash": h, "t_recv": t})


async def rtds_activity(store: Store, stop: threading.Event, t_end: float, tokens: dict[str, tuple[str, str]],
                        wallets: set[str]) -> None:
    """Legacy RTDS ``activity`` topic (types ``trades`` and ``orders_matched``): public, with wallet.

    Measured on 2026-09-25: without application-level ``PING`` messages the server silently stops
    sending after ~6 minutes while the socket stays open. Hence a ``PING`` every 5 s and a watchdog
    that reconnects after ``stall_s`` seconds without data (the unfiltered feed carries ~20 msg/s).
    """
    import websockets

    stall_s = 15.0
    while not stop.is_set() and time.time() < t_end:
        try:
            async with websockets.connect(RTDS_WS, max_size=None, open_timeout=15) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "activity", "type": "trades"}, {"topic": "activity", "type": "orders_matched"}]}))
                store.count("ws_connections", "rtds")
                last_ping = last_data = time.time()
                while not stop.is_set() and time.time() < t_end:
                    if time.time() - last_ping >= 5:
                        await ws.send("PING")
                        last_ping = time.time()
                    if time.time() - last_data > stall_s:
                        store.add("errors", {"component": "rtds", "error": "stall: reconnect", "t": time.time()})
                        break
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    t = time.time()
                    try:
                        j = json.loads(msg)
                    except ValueError:  # "" = answer to PING
                        continue
                    last_data = t
                    typ, p = str(j.get("type")), j.get("payload")
                    if not isinstance(p, dict):
                        continue
                    store.count("rtds_events", typ)
                    ts = pm._f(p.get("timestamp"))
                    if np.isfinite(ts):
                        store.sample(f"rtds_lag_vs_trade_ts_{typ}", t - ts)
                    msg_ts = pm._f(j.get("timestamp"))
                    if np.isfinite(msg_ts):
                        store.sample(f"rtds_transport_{typ}", t - msg_ts / 1000.0)
                    wallet = str(p.get("proxyWallet", "")).lower()
                    asset = str(p.get("asset"))
                    if asset in tokens or wallet in wallets:
                        store.add("rtds", {
                            "type": typ, "t_recv": t, "msg_ts_ms": msg_ts, "timestamp": ts,
                            "tx_hash": str(p.get("transactionHash", "")).lower(), "wallet": wallet,
                            "token_id": asset, "side": p.get("side"), "size": p.get("size"), "price": p.get("price"),
                            "outcome": p.get("outcome"), "slug": p.get("slug"), "tracked": asset in tokens,
                        })
        except Exception as exc:
            store.add("errors", {"component": "rtds", "error": repr(exc)[:300], "t": time.time()})
            await asyncio.sleep(1.0)


def pick_active_wallets(session: requests.Session, condition_ids: list[str], n: int = 1) -> list[tuple[str, int]]:
    """Most frequent ``proxy_wallet`` values among the last 4 minutes of trades of these markets
    (``condition_ids=[]``: the global feed, all markets)."""
    rows = fetch_trades(session, max_pages=1, condition=",".join(condition_ids) or None, limit=1000)
    t_max = max((int(r.get("timestamp", 0)) for r in rows), default=0)
    cnt = Counter(str(r.get("proxy_wallet", "")).lower() for r in rows if int(r.get("timestamp", 0)) >= t_max - 240)
    return cnt.most_common(n)


def run_live(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = pm.PolymarketClient()
    session = _session()
    store = Store()
    duration = args.seconds if args.seconds else args.minutes * 60
    print("[live] clock check...", flush=True)
    clock0 = clock_check()
    n_next = max(1, math.ceil(duration / 300) + 1)
    markets = client.current_and_next_markets(args.asset, args.duration, n_next=n_next)
    if not markets:
        raise SystemExit("no current market found")
    tokens: dict[str, tuple[str, str]] = {}
    for m in markets:
        tokens[m.token_up] = (m.slug, "Up")
        tokens[m.token_down] = (m.slug, "Down")
    cids = [m.condition_id for m in markets]
    prev = client.get_updown_market(args.asset, args.duration, int(markets[0].start.timestamp()) - 300)
    if args.wallet:
        picked = [(w.lower(), None) for w in args.wallet.split(",")]
    elif args.wallet_source == "market":
        picked = pick_active_wallets(session, cids[:1] + ([prev.condition_id] if prev else []), args.wallets)
    else:  # most active wallets of the whole platform right now: many samples in a few minutes
        picked = pick_active_wallets(session, [], args.wallets)
    wallets = [w for w, _ in picked]
    store.meta.update({
        "markets": [m.slug for m in markets], "condition_ids": cids, "wallets": wallets,
        "wallet_rows_in_last_1000": dict(picked), "start": time.time(), "duration_s": duration,
        "clock_start": clock0, "interval_s": args.interval,
    })
    print(f"[live] {len(markets)} markets from {markets[0].slug}; wallets={wallets}; {duration:.0f} s", flush=True)
    stop = threading.Event()
    t_end = time.time() + duration
    probe_q: queue.Queue = queue.Queue()

    def open_cid() -> str:
        now = time.time()
        for m in markets:
            if m.start.timestamp() <= now < m.end.timestamp():
                return m.condition_id
        return cids[0]

    def cond_params(bust: bool, single: bool = False) -> Callable[[], dict]:
        def f() -> dict:
            p = {"condition": open_cid() if single else ",".join(cids), "limit": 1000, "taker_only": "true"}
            if bust:
                p["_"] = str(time.time_ns())
            return p
        return f

    def user_params(w: str) -> Callable[[], dict]:
        return lambda: {"user": w, "limit": 200, "taker_only": "false", "_": str(time.time_ns())}

    def poller(name: str, fn: Callable[[], dict]) -> threading.Thread:
        return threading.Thread(target=poll_data_api, args=(store, stop, t_end, name, fn, args.interval))

    threads = [
        poller("busted", cond_params(True)),
        threading.Thread(target=follow_chain_http, args=(store, stop, t_end, tokens, set(wallets), args.interval)),
        poller("single", cond_params(True, single=True)),
        threading.Thread(target=probe_mempool, args=(store, stop, t_end, probe_q)),
    ]
    if not args.no_naive:
        threads.append(poller("naive", cond_params(False)))
    threads += [poller(f"user{i}", user_params(w)) for i, w in enumerate(wallets)]

    # one event loop per socket: a busy socket (the market channel sends hundreds of messages per
    # second) must not delay the timestamps taken on the others
    for coro_fn, extra in ((market_ws, probe_q), (chain_wss, set(wallets)), (rtds_activity, set(wallets))):
        threads.append(threading.Thread(
            target=lambda f=coro_fn, x=extra: asyncio.run(f(store, stop, t_end, tokens, x))))
    for th in threads:
        th.daemon = True
        th.start()
        time.sleep(0.15)  # spread the pollers inside the second
    try:
        while any(th.is_alive() for th in threads) and time.time() < t_end + 30:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    stop.set()
    for th in threads:
        th.join(timeout=15)
    store.meta["clock_end"] = clock_check()
    store.meta["end"] = time.time()
    for name, rows in store.tables.items():
        pd.DataFrame(rows).to_csv(out / f"{name}.csv", index=False)
    lags = {k: describe(v) for k, v in store.samples.items()}
    meta = {**store.meta, "counters": {k: dict(v) for k, v in store.counters.items()}, "sample_stats": lags}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    return summarize(out)


# ---------------------------------------------------------------------------
# Summary of a live run
# ---------------------------------------------------------------------------
def _read(out: Path, name: str) -> pd.DataFrame:
    p = out / f"{name}.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(p, dtype={"token_id": "str", "tx_hash": "str", "wallet": "str"})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def summarize(out: Path) -> dict:
    """Latency statistics of a ``live`` run (all times in seconds, local clock)."""
    meta = json.loads((out / "meta.json").read_text())
    counters = meta.get("counters") or {}
    S: dict[str, Any] = {
        "markets": meta.get("markets"), "wallets": meta.get("wallets"),
        "duration_s": meta.get("duration_s"), "clock_start": meta.get("clock_start"),
        "clock_end": meta.get("clock_end"), "counters": counters,
        "wss_subscriptions": {k: v for k, v in meta.items() if k.startswith("wss_subscribe_")},
        "rpc_http": {"calls": meta.get("rpc_http_calls"), "errors": meta.get("rpc_http_errors"),
                     "eth_blockNumber_rtt_s": (meta.get("sample_stats") or {}).get("rpc_blocknumber_rtt")},
    }
    # -- data-api pollers -------------------------------------------------------
    api: dict[str, pd.DataFrame] = {}
    names = ["busted", "single", "naive"] + sorted(p.stem.removeprefix("polls_") for p in out.glob("polls_user*.csv"))
    for name in names:
        polls, tr = _read(out, f"polls_{name}"), _read(out, f"trades_{name}")
        if polls.empty:
            continue
        d: dict[str, Any] = {
            "polls": int(len(polls)),
            "status": {str(k): int(v) for k, v in polls["status"].value_counts().items()},
            "rtt_s": describe(polls["t_recv"] - polls["t_sent"]),
            "x_cache": {str(k): int(v) for k, v in polls["x_cache"].fillna("").value_counts().items()},
            "cache_age_s": describe(pd.to_numeric(polls["age"], errors="coerce")),
            # how often the newest trade of the answer changes (refresh cadence of the feed)
            "newest_ts_change_every_s": describe(polls.loc[polls["max_ts"].diff().fillna(0) != 0, "t_recv"].diff()),
            "new_trades": int(len(tr)),
        }
        if not tr.empty:
            d["delay_s"] = describe(tr["t_seen"] - tr["timestamp"])
            api[name] = tr
        S[f"data_api_{name}"] = d
    # -- chain: HTTP polling and WSS subscriptions --------------------------------
    blocks = _read(out, "chain_blocks")
    if not blocks.empty:
        blocks = blocks.dropna(subset=["block_ts"]).drop_duplicates("block")
        S["chain_http"] = {
            "blocks": int(len(blocks)),
            "block_interval_s": describe(blocks.sort_values("block")["block_ts"].diff()),
            "head_seen_minus_block_ts_s": describe(blocks["t_head_seen"] - blocks["block_ts"]),
            "logs_seen_minus_block_ts_s": describe(blocks["t_logs_seen"] - blocks["block_ts"]),
            "order_filled_per_block": describe(blocks["n_order_filled"]),
            "blocks_without_logs": int((blocks["n_logs"] == 0).sum()),
        }
    heads = _read(out, "wss_heads")
    if not heads.empty:
        S["chain_wss_heads"] = {"recv_minus_block_ts_s": describe(heads["t_recv"] - heads["block_ts"])}
    wlb = _read(out, "wss_log_blocks")
    ts_parts = [df[["block", "block_ts"]] for df in (blocks, heads) if not df.empty]
    if not wlb.empty and ts_parts:
        wlb = wlb.merge(pd.concat(ts_parts).drop_duplicates("block"), on="block", how="left")
        S["chain_wss_logs"] = {"blocks": int(len(wlb)),
                               "first_log_minus_block_ts_s": describe(wlb["t_first"] - wlb["block_ts"]),
                               "last_log_minus_block_ts_s": describe(wlb["t_last"] - wlb["block_ts"])}
    # -- market websocket -----------------------------------------------------------
    ws = _read(out, "ws_trades")
    S["market_ws"] = {
        "events": counters.get("ws_events"),
        "last_trade_price_keys": sorted(set(ws["keys"])) if not ws.empty else None,
        "transport_lag_s": {k.removeprefix("ws_lag_"): v for k, v in (meta.get("sample_stats") or {}).items()
                            if k.startswith("ws_lag_")},
    }
    rt = _read(out, "rtds")
    S["rtds_activity"] = {
        "events": counters.get("rtds_events"),
        "lag_vs_trade_timestamp_s": {k.removeprefix("rtds_lag_vs_trade_ts_"): v for k, v in
                                     (meta.get("sample_stats") or {}).items() if k.startswith("rtds_lag_vs_trade_ts_")},
        "transport_lag_s": {k.removeprefix("rtds_transport_"): v for k, v in
                            (meta.get("sample_stats") or {}).items() if k.startswith("rtds_transport_")},
    }
    all_fills = _read(out, "chain_fills")
    fills = all_fills[all_fills["slug"].fillna("") != ""] if not all_fills.empty else all_fills
    pend = _read(out, "pending_matches")
    if not pend.empty:
        pend = pend.sort_values("t_recv").drop_duplicates("tx_hash")
    # -- per-transaction timeline (tracked markets) ---------------------------------
    tl = pd.DataFrame()
    if not fills.empty:
        f_http = fills[fills["source"] == "http"].groupby("tx_hash").agg(block_ts=("block_ts", "min"),
                                                                          t_chain_http=("t_seen", "min"))
        f_wss = fills[fills["source"] == "wss"].groupby("tx_hash").agg(t_chain_wss=("t_seen", "min"))
        tl = f_http.join(f_wss, how="outer")
        if not ws.empty:
            w = ws.groupby("tx_hash").agg(t_ws=("t_recv", "min"), ws_ts=("ts_ms", "min"))
            w["ws_ts"] = w["ws_ts"] / 1000.0
            tl = tl.join(w, how="outer")
        if not pend.empty:
            tl = tl.join(pend.set_index("tx_hash")[["t_recv"]].rename(columns={"t_recv": "t_pending"}), how="left")
        if not rt.empty:
            for typ, col in (("trades", "t_rtds_trade"), ("orders_matched", "t_rtds_matched")):
                r = rt[(rt["type"] == typ) & rt["tracked"].astype(bool)]
                if len(r):
                    tl = tl.join(r.groupby("tx_hash").agg(**{col: ("t_recv", "min")}), how="left")
        for name in ("busted", "single", "naive"):
            if name in api:
                a = api[name].groupby("tx_hash").agg(**{f"t_api_{name}": ("t_seen", "min"),
                                                        f"api_ts_{name}": ("timestamp", "min")})
                tl = tl.join(a, how="left")
        tl.index.name = "tx_hash"
        tl.reset_index().to_csv(out / "timeline.csv", index=False)

        def diff(a: str, b: str) -> dict:
            return describe(tl[a] - tl[b]) if a in tl and b in tl else {"n": 0}

        S["timeline"] = {
            "n_tx": int(len(tl)),
            "ws_recv_minus_ws_ts": diff("t_ws", "ws_ts"),
            "pending_minus_ws_recv": diff("t_pending", "t_ws"),
            "block_ts_minus_ws_ts": diff("block_ts", "ws_ts"),
            "block_ts_minus_pending": diff("block_ts", "t_pending"),
            "chain_wss_minus_block_ts": diff("t_chain_wss", "block_ts"),
            "chain_http_minus_block_ts": diff("t_chain_http", "block_ts"),
            "chain_wss_minus_ws_recv": diff("t_chain_wss", "t_ws"),
            "chain_http_minus_ws_recv": diff("t_chain_http", "t_ws"),
            "rtds_matched_minus_block_ts": diff("t_rtds_matched", "block_ts"),
            "rtds_trade_minus_block_ts": diff("t_rtds_trade", "block_ts"),
            "rtds_matched_minus_chain_wss": diff("t_rtds_matched", "t_chain_wss"),
            "rtds_matched_minus_pending": diff("t_rtds_matched", "t_pending"),
            "rtds_trade_minus_rtds_matched": diff("t_rtds_trade", "t_rtds_matched"),
            "api_busted_minus_rtds_trade": diff("t_api_busted", "t_rtds_trade"),
            "api_busted_minus_block_ts": diff("t_api_busted", "block_ts"),
            "api_single_minus_block_ts": diff("t_api_single", "block_ts"),
            "api_naive_minus_block_ts": diff("t_api_naive", "block_ts"),
            "api_busted_minus_chain_wss": diff("t_api_busted", "t_chain_wss"),
            "api_busted_minus_chain_http": diff("t_api_busted", "t_chain_http"),
            "api_busted_minus_ws_ts": diff("t_api_busted", "ws_ts"),
            "api_single_minus_api_busted": diff("t_api_single", "t_api_busted"),
            "ws_tx_seen_pending": f"{int(tl[['t_ws', 't_pending']].dropna().shape[0])}/"
                                  f"{int(tl['t_ws'].notna().sum()) if 't_ws' in tl else 0}",
            "api_timestamp_equals_block_ts": (
                f"{int((tl['api_ts_busted'] == tl['block_ts']).sum())}/"
                f"{int(tl[['api_ts_busted', 'block_ts']].dropna().shape[0])}" if "api_ts_busted" in tl else None),
        }
    # -- data-api rows vs on-chain taker fills (same tx, token, wallet) ------------
    if "busted" in api and not fills.empty:
        tk = fills[(fills["role"] == "taker") & (fills["source"] == "http")]
        a = api["busted"]
        m = a.merge(tk, on=["tx_hash", "token_id", "wallet"], how="inner", suffixes=("_api", "_chain"))
        late = tk[tk["t_seen"] < tk["t_seen"].max() - 30]  # leave time for the API to catch up
        S["api_vs_chain"] = {
            "api_rows_timed": int(len(a)),
            "api_rows_matching_chain_taker_fill": int(len(m)),
            "side_equal": int((m["side_api"] == m["side_chain"]).sum()),
            "size_equal_1e-6": int((np.abs(m["size"].astype(float) - m["shares"]) < 1e-6).sum()),
            "price_abs_diff_max": round(float(np.abs(m["price_api"].astype(float) - m["price_chain"]).max()), 8)
            if len(m) else None,
            "timestamp_equals_block_ts": int((m["timestamp"] == m["block_ts"]).sum()),
            "chain_taker_tx_older_than_30s": int(late["tx_hash"].nunique()),
            "of_which_missing_in_api": int(len(set(late["tx_hash"]) - set(a["tx_hash"]))),
        }
    # -- RTDS rows vs on-chain fills: which roles does each RTDS type carry? ----------
    if not rt.empty and not fills.empty:
        f = fills[fills["source"] == "http"].drop_duplicates(["tx_hash", "wallet", "token_id", "role"])
        rr = rt[rt["tracked"].astype(bool)]
        late = rr[rr["t_recv"] < rr["t_recv"].max() - 30]
        d = {}
        for typ in ("trades", "orders_matched"):
            x = late[late["type"] == typ].merge(f[["tx_hash", "wallet", "token_id", "role", "side", "shares"]],
                                                 on=["tx_hash", "wallet", "token_id"], how="left",
                                                 suffixes=("", "_chain"))
            d[typ] = {"rows": int((late["type"] == typ).sum()),
                      "roles": {str(k): int(v) for k, v in x["role"].fillna("no_match").value_counts().items()},
                      "rows_per_tx": describe(late[late["type"] == typ].groupby("tx_hash").size())}
        tx_chain = set(f.loc[f["t_seen"] < f["t_seen"].max() - 30, "tx_hash"])
        d["chain_tx_older_than_30s"] = len(tx_chain)
        d["of_which_missing_in_rtds_trades"] = len(tx_chain - set(rr.loc[rr["type"] == "trades", "tx_hash"]))
        S["rtds_vs_chain"] = d
    # -- pending transactions decoded from the mempool ------------------------------
    if not pend.empty:
        d = {"pending_matchorders_tx": int(len(pend)), "counters": counters.get("wss_pending")}
        if not fills.empty:
            tk = fills[(fills["role"] == "taker")].sort_values("t_seen").drop_duplicates("tx_hash")
            j = pend.merge(tk[["tx_hash", "wallet", "block_ts", "t_seen"]], on="tx_hash", how="inner")
            d["tracked_tx_pending_and_mined"] = int(len(j))
            d["decoded_taker_wallet_equals_chain"] = int((j["taker_wallet"] == j["wallet"]).sum())
            d["pending_minus_order_created_s"] = describe(j["t_recv"] - j["taker_order_ts_ms"] / 1000.0)
            d["block_ts_minus_pending_s"] = describe(j["block_ts"] - j["t_recv"])
            d["log_seen_minus_pending_s"] = describe(j["t_seen"] - j["t_recv"])
            mined_tx = set(fills["tx_hash"])
            tracked_pend = pend[pend["token_id"].astype(str).isin(set(fills["token_id"].astype(str)))]
            tracked_pend = tracked_pend[tracked_pend["t_recv"] < tracked_pend["t_recv"].max() - 30]
            d["tracked_pending_older_than_30s"] = int(len(tracked_pend))
            d["of_which_never_seen_mined"] = int((~tracked_pend["tx_hash"].isin(mined_tx)).sum())
        S["pending"] = d
    # -- followed wallets ---------------------------------------------------------------
    S["wallet_tracking"] = {}
    for i, w in enumerate(meta.get("wallets") or []):
        u = api.get(f"user{i}")
        wf = all_fills[(all_fills["wallet"] == w) | (all_fills["counterparty"] == w)] if not all_fills.empty \
            else all_fills
        if u is None and wf.empty:
            continue
        j = pd.DataFrame()
        if not wf.empty:
            j = wf[wf["source"] == "http"].groupby("tx_hash").agg(
                block_ts=("block_ts", "min"), t_chain_http=("t_seen", "min"),
                roles=("role", lambda r: ",".join(sorted(set(r)))))
            j = j.join(wf[wf["source"] == "wss"].groupby("tx_hash").agg(t_chain_wss=("t_seen", "min")), how="outer")
        if not pend.empty:
            pw = pend[(pend["taker_wallet"] == w) | pend["maker_wallets"].fillna("").str.contains(w, regex=False)]
            j = j.join(pw.set_index("tx_hash")[["t_recv"]].rename(columns={"t_recv": "t_pending"}), how="outer")
        if not rt.empty:
            rw = rt[rt["wallet"] == w]
            if len(rw):
                j = j.join(rw.groupby("tx_hash").agg(t_rtds=("t_recv", "min")), how="outer")
        if u is not None:
            j = j.join(u.groupby("tx_hash").agg(t_api=("t_seen", "min"), api_ts=("timestamp", "min")), how="outer")
            if "busted" in api:
                j = j.join(api["busted"].groupby("tx_hash").agg(t_api_cond=("t_seen", "min")), how="left")

        def dj(a: str, b: str, jj: pd.DataFrame = j) -> dict:
            return describe(jj[a] - jj[b]) if a in jj and b in jj else {"n": 0}

        S["wallet_tracking"][w] = {
            "api_rows": int(len(u)) if u is not None else 0,
            "api_tx": int(u["tx_hash"].nunique()) if u is not None else 0,
            "api_markets": {str(k): int(v) for k, v in u["slug"].str.rsplit("-", n=1).str[0].value_counts()
                            .head(6).items()} if u is not None else {},
            "chain_tx": int(j["t_chain_http"].notna().sum()) if "t_chain_http" in j else 0,
            "roles_on_chain": {str(k): int(v) for k, v in j["roles"].value_counts().items()} if "roles" in j else {},
            "api_delay_vs_trade_timestamp": describe(u["t_seen"] - u["timestamp"]) if u is not None else {"n": 0},
            "api_minus_block_ts": dj("t_api", "block_ts"),
            "chain_wss_minus_block_ts": dj("t_chain_wss", "block_ts"),
            "chain_http_minus_block_ts": dj("t_chain_http", "block_ts"),
            "pending_minus_block_ts": dj("t_pending", "block_ts"),
            "rtds_minus_block_ts": dj("t_rtds", "block_ts"),
            "rtds_minus_pending": dj("t_rtds", "t_pending"),
            "api_minus_rtds": dj("t_api", "t_rtds"),
            "api_minus_pending": dj("t_api", "t_pending"),
            "api_minus_chain_wss": dj("t_api", "t_chain_wss"),
            "api_minus_chain_http": dj("t_api", "t_chain_http"),
            "user_feed_minus_condition_feed_same_tx": dj("t_api", "t_api_cond"),
        }
    # -- mempool probe over HTTP (eth_getTransactionByHash right after the WebSocket trade) --
    mp = _read(out, "mempool")
    if not mp.empty:
        S["mempool_probe_http"] = {
            "probes": int(len(mp)), "found": int(mp["found"].sum()), "pending": int(mp["pending"].sum()),
            "query_minus_ws_recv_s": describe(mp["t_query"] - mp["t_ws"]),
            "answer_rtt_s": describe(mp["t_answer"] - mp["t_query"]),
        }
    errs = _read(out, "errors")
    if not errs.empty:
        S["errors"] = {str(k): int(v) for k, v in errs["component"].value_counts().items()}
    (out / "summary.json").write_text(json.dumps(S, indent=1, default=str))
    return S


# ---------------------------------------------------------------------------
# taker_only semantics
# ---------------------------------------------------------------------------
def run_roles(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = pm.PolymarketClient()
    s = _session()
    start = (int(time.time()) // 300) * 300 - 600 if not args.start else int(args.start)
    m = client.get_updown_market(args.asset, args.duration, start)
    if m is None:
        raise SystemExit(f"no market at {start}")
    t_true = fetch_trades(s, condition=m.condition_id, taker_only="true")
    t_false = fetch_trades(s, condition=m.condition_id, taker_only="false")
    A = pd.DataFrame(t_true)
    B = pd.DataFrame(t_false)
    for df in (A, B):
        df["tx_hash"] = df["transaction_hash"].str.lower()
        df["wallet"] = df["proxy_wallet"].str.lower()
        df["token_id"] = df["token_id"].astype(str)
        df["size6"] = df["size"].astype(float).round(6)
    ts0, ts1 = int(A["timestamp"].min()), int(A["timestamp"].max())
    ts0 = max(ts0, ts1 - args.max_hours * 3600)
    rpc = Rpc(timeout=90)
    b0 = rpc.block_at(ts0 - 2)
    b1 = rpc.block_at(ts1 + 2, lo=b0)
    t0 = time.time()
    fills = pd.DataFrame(f for f in rpc.fills(b0, b1 + 1) if f["token_id"] in (m.token_up, m.token_down))
    t_logs = time.time() - t0
    fills["size6"] = fills["shares"].round(6)
    A.to_csv(out / "roles_api_taker_only_true.csv", index=False)
    B.to_csv(out / "roles_api_taker_only_false.csv", index=False)
    fills.to_csv(out / "roles_chain_fills.csv", index=False)
    in_range = lambda df: df[(df["timestamp"] >= ts0) & (df["timestamp"] <= ts1)]  # noqa: E731
    A, B = in_range(A), in_range(B)
    key = ["tx_hash", "wallet", "token_id", "side", "size6"]
    tk, mk = fills[fills["role"] == "taker"], fills[fills["role"] == "maker"]

    def multiset_match(api: pd.DataFrame, chain: pd.DataFrame, cols: list[str]) -> int:
        ca, cc = Counter(map(tuple, api[cols].values.tolist())), Counter(map(tuple, chain[cols].values.tolist()))
        return int(sum(min(n, cc[k]) for k, n in ca.items()))

    ca, cb = Counter(map(tuple, A[key].values.tolist())), Counter(map(tuple, B[key].values.tolist()))
    extra = cb - ca  # rows only present with taker_only=false
    extra_df = pd.DataFrame([k for k, n in extra.items() for _ in range(n)], columns=key)
    dup = B.groupby("tx_hash").size()
    both_buy = mk.merge(tk[["tx_hash", "side", "token_id"]], on="tx_hash", suffixes=("", "_taker"))
    res = {
        "market": m.slug, "window": [str(m.start), str(m.end)], "trade_ts_range": [ts0, ts1],
        "blocks": [b0, b1], "getlogs_seconds": round(t_logs, 1),
        "api_taker_only_true_rows": int(len(A)), "api_taker_only_false_rows": int(len(B)),
        "chain_taker_fills": int(len(tk)), "chain_maker_fills": int(len(mk)),
        "true_rows_matching_chain_taker_fill": multiset_match(A, tk, key),
        "true_rows_matching_chain_maker_fill": multiset_match(A, mk, key),
        "false_rows_extra": int(len(extra_df)),
        "extra_rows_matching_chain_maker_fill": multiset_match(extra_df, mk, key) if len(extra_df) else 0,
        "extra_rows_matching_chain_maker_fill_ignoring_side": multiset_match(
            extra_df, mk, ["tx_hash", "wallet", "token_id", "size6"]) if len(extra_df) else 0,
        "false_rows_matching_any_chain_fill": multiset_match(B, fills, key),
        "rows_per_tx_false": describe(dup),
        "tx_with_single_row_false": int((dup == 1).sum()),
        "maker_fills_other_token_than_taker": int((both_buy["token_id"] != both_buy["token_id_taker"]).sum()),
        "maker_fills_same_side_as_taker": int((both_buy["side"] == both_buy["side_taker"]).sum()),
        "maker_fill_pairs": int(len(both_buy)),
        "self_trades_wallet_both_roles_same_tx": int(len(set(zip(tk["tx_hash"], tk["wallet"]))
                                                         & set(zip(mk["tx_hash"], mk["wallet"])))),
    }
    (out / "roles.json").write_text(json.dumps(res, indent=1, default=str))
    return res


# ---------------------------------------------------------------------------
# Public node limits
# ---------------------------------------------------------------------------
def run_rpc_limits(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rpc = Rpc(timeout=120)
    head = rpc.head()
    res: dict[str, Any] = {"head": head, "getlogs_range": [], "wallet_range": []}
    flt = {"address": list(EXCHANGES), "topics": [TOPIC_ORDER_FILLED]}
    for n in (1, 10, 100, 1000, 5000):
        t0 = time.time()
        try:
            logs = rpc.call("eth_getLogs", [{**flt, "fromBlock": hex(head - 10 - n + 1), "toBlock": hex(head - 10)}])
            row = {"blocks": n, "seconds": round(time.time() - t0, 2), "logs": len(logs),
                   "mbytes": round(rpc.last_bytes / 1e6, 1)}
        except RpcError as exc:
            row = {"blocks": n, "seconds": round(time.time() - t0, 2), "error": str(exc)[:200]}
        res["getlogs_range"].append(row)
        print(row, flush=True)
    # a wallet filter (indexed ``maker`` topic = topics[2]) makes long ranges cheap
    s = _session()
    wallet = args.wallet
    if not wallet:
        wallet = pick_active_wallets(s, [])[0][0]
    res["wallet"] = wallet
    for n in (1_000, 10_000, 20_000):
        t0 = time.time()
        try:
            logs = rpc.call("eth_getLogs", [{**flt, "fromBlock": hex(head - n), "toBlock": hex(head),
                                             "topics": [TOPIC_ORDER_FILLED, None, _addr_topic(wallet)]}])
            row = {"blocks": n, "seconds": round(time.time() - t0, 2), "logs": len(logs)}
        except RpcError as exc:
            row = {"blocks": n, "seconds": round(time.time() - t0, 2), "error": str(exc)[:200]}
        res["wallet_range"].append(row)
        print("wallet", row, flush=True)
    # history depth: oldest block whose logs are still served
    def ok(b: int) -> bool:
        try:
            rpc.call("eth_getLogs", [{**flt, "fromBlock": hex(b), "toBlock": hex(b)}])
            return True
        except RpcError:
            return False

    lo, hi = head - 5_000_000, head
    if ok(lo):
        res["oldest_logs_block"] = f"<= {lo}"
    else:
        while hi - lo > 100:
            mid = (lo + hi) // 2
            lo, hi = (lo, mid) if ok(mid) else (mid, hi)
        ts_hi, ts_head = rpc.block_ts(hi), rpc.block_ts(head)
        res["oldest_logs_block"] = hi
        res["history_days"] = round((ts_head - ts_hi) / 86400, 2) if ts_hi and ts_head else None
        res["history_blocks"] = head - hi
    # burst throughput (sequential and 8 threads)
    def burst(n: int, workers: int) -> dict:
        def one(_: int) -> str:
            r = Rpc(timeout=10)
            try:
                r.head()
                return "ok"
            except RpcError as exc:
                return str(exc.code)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outc = Counter(ex.map(one, range(n)))
        dt = time.time() - t0
        return {"requests": n, "workers": workers, "seconds": round(dt, 2), "req_per_s": round(n / dt, 1),
                "outcomes": dict(outc)}

    res["burst"] = [burst(40, 1), burst(120, 8)]
    print(res["burst"], flush=True)
    (out / "rpc_limits.json").write_text(json.dumps(res, indent=1, default=str))
    return res


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("clock", "live", "report", "roles", "rpc-limits"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=str(DEFAULT_OUT))
        p.add_argument("--asset", default="btc")
        p.add_argument("--duration", default="5m")
        p.add_argument("--wallet", default=None, help="wallet(s) to follow, comma-separated")
        if name == "live":
            p.add_argument("--minutes", type=float, default=10.0)
            p.add_argument("--seconds", type=float, default=0.0, help="overrides --minutes (quick tests)")
            p.add_argument("--interval", type=float, default=1.0)
            p.add_argument("--no-naive", action="store_true")
            p.add_argument("--wallets", type=int, default=3, help="number of active wallets to follow")
            p.add_argument("--wallet-source", choices=("global", "market"), default="global",
                           help="pick the wallets among all markets or among the tracked market")
        if name == "roles":
            p.add_argument("--start", default=None, help="window start (unix s); default: closed 10 min ago")
            p.add_argument("--max-hours", type=float, default=3.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "clock":
        res = clock_check()
    elif args.cmd == "live":
        res = run_live(args)
    elif args.cmd == "report":
        res = summarize(Path(args.out))
    elif args.cmd == "roles":
        res = run_roles(args)
    else:
        res = run_rpc_limits(args)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
