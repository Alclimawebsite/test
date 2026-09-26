"""Enregistre en continu le carnet d'ordres et les trades des marchés Polymarket « Up or Down ».

Une connexion WebSocket par marché (jetons Up et Down), ouverte avant l'ouverture de la fenêtre
et fermée après la résolution. Chaque message est écrit tel quel dans
``data/cache/polymarket/live/<slug>.jsonl.gz`` (gzip en continu) avec l'heure locale de
réception (ns) ; les événements ``new_market`` (tous marchés, volumineux) sont ignorés. Un ``meta.json`` par marché conserve les métadonnées
(jetons, priceToBeat quand il est connu, issue).

Usage : ``python scripts/polymarket_live_collector.py [--series btc:5m,btc:15m,eth:5m]``.
Lecture seule de données publiques : aucun ordre, aucune clé.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import websockets

from tradebot.config import CACHE_DIR
from tradebot.polymarket import GAMMA_URL, PolymarketClient, UpDownMarket, parse_market

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LIVE_DIR = CACHE_DIR / "polymarket" / "live"
LEAD_S = 150        # ouvrir la connexion 2,5 min avant l'ouverture de la fenêtre
TAIL_S = 120        # la garder 2 min après la clôture (résolution, derniers trades)
PING_S = 10

log = logging.getLogger("live_collector")


async def record_market(m: UpDownMarket, client: PolymarketClient) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = LIVE_DIR / f"{m.slug}.jsonl.gz"
    end_at = m.end.timestamp() + TAIL_S
    meta = {"slug": m.slug, "asset": m.asset, "duration": m.duration, "start": m.start.isoformat(),
            "end": m.end.isoformat(), "condition_id": m.condition_id, "token_up": m.token_up,
            "token_down": m.token_down, "fee_schedule": m.fee_schedule, "twap_lookback_s": m.twap_lookback_s,
            "collector_started": time.time()}
    (LIVE_DIR / f"{m.slug}.meta.json").write_text(json.dumps(meta))
    sub = json.dumps({"assets_ids": [m.token_up, m.token_down], "type": "market", "custom_feature_enabled": True})
    n_msgs, n_reconnect = 0, 0
    with gzip.open(path, "at", compresslevel=3) as fh:
        while time.time() < end_at:
            try:
                async with websockets.connect(WS_URL, max_size=None, ping_interval=None, open_timeout=15) as ws:
                    await ws.send(sub)
                    fh.write(json.dumps({"rx": time.time_ns(), "ev": "connect", "n": n_reconnect}) + "\n")
                    last_ping = time.time()
                    while time.time() < end_at:
                        timeout = max(0.5, PING_S - (time.time() - last_ping))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            await ws.send("PING")
                            last_ping = time.time()
                            continue
                        if raw == "PONG" or (len(raw) > 60 and raw[:60].find('"new_market"') >= 0):
                            continue
                        fh.write(json.dumps({"rx": time.time_ns(), "msg": raw}, separators=(",", ":")) + "\n")
                        n_msgs += 1
            except Exception as exc:  # noqa: BLE001 — on se reconnecte quoi qu'il arrive
                n_reconnect += 1
                fh.write(json.dumps({"rx": time.time_ns(), "ev": "error", "err": repr(exc)[:300]}) + "\n")
                log.warning("%s : reconnexion %d (%r)", m.slug, n_reconnect, exc)
                await asyncio.sleep(min(10, 1 + n_reconnect))
    # issue officielle et niveaux Chainlink, s'ils sont déjà publiés
    try:
        events = await asyncio.to_thread(
            client._get, f"{GAMMA_URL}/events", [("slug", m.slug), ("limit", 1)])
        ev = (events or [{}])[0]
        fresh = parse_market((ev.get("markets") or [{}])[0], ev) if ev else None
        meta.update(resolved_up=fresh.resolved_up if fresh else None,
                    closed=fresh.closed if fresh else None,
                    event_metadata=ev.get("eventMetadata"))
    except Exception as exc:  # noqa: BLE001
        meta["resolution_error"] = repr(exc)[:200]
    meta.update(n_msgs=n_msgs, n_reconnect=n_reconnect, collector_finished=time.time())
    (LIVE_DIR / f"{m.slug}.meta.json").write_text(json.dumps(meta))
    log.info("%s : %d messages, %d reconnexions", m.slug, n_msgs, n_reconnect)


async def scheduler(series: list[tuple[str, str]], poll_s: float = 30.0) -> None:
    client = PolymarketClient(use_cache=False)
    tasks: dict[str, asyncio.Task] = {}
    while True:
        now = time.time()
        for asset, dur in series:
            try:
                markets = await asyncio.to_thread(client.current_and_next_markets, asset, dur, 2)
            except Exception as exc:  # noqa: BLE001
                log.warning("gamma %s %s : %r", asset, dur, exc)
                continue
            for m in markets:
                if m.slug in tasks or m.end.timestamp() + TAIL_S < now:
                    continue
                if m.start.timestamp() - now > LEAD_S:
                    continue
                tasks[m.slug] = asyncio.create_task(record_market(m, client))
                log.info("%s : enregistrement lancé", m.slug)
        for slug, t in list(tasks.items()):
            if t.done():
                if t.exception():
                    log.error("%s : %r", slug, t.exception())
                del tasks[slug]
        await asyncio.sleep(poll_s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--series", default="btc:5m,btc:15m,eth:5m")
    ap.add_argument("--poll", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    series = [tuple(s.split(":")) for s in args.series.split(",") if s]
    try:
        asyncio.run(scheduler(series, args.poll))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
