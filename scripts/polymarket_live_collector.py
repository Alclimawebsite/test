"""Enregistre en continu le carnet d'ordres et les trades des marchés Polymarket « Up or Down ».

Une connexion WebSocket par marché (jetons Up et Down), ouverte 2,5 min avant l'ouverture de la
fenêtre et fermée 2 min après la clôture (ou dès que le serveur annonce la résolution). Chaque
message est écrit dans ``data/cache/polymarket/live/<slug>.jsonl.gz`` (gzip en continu, vidé sur
disque toutes les 5 s) avec l'heure locale de réception ``rx`` (ns).

Format 2 (compact, une ligne JSON par événement ; ``a`` = 0 pour le jeton Up, 1 pour Down) :

* ``{"e":"book","a":0,"ts":ms,"b":[[prix,taille],…],"k":[[prix,taille],…]}`` : instantané complet
  (``b`` = bids, ``k`` = asks) ;
* ``{"e":"pc","ts":ms,"c":[[a,prix,taille,"B"|"S",best_bid,best_ask],…]}`` : nouvelles tailles de
  niveaux (``price_change``), **filtrées aux niveaux à ± 0,10 du milieu** de leur jeton ;
  les niveaux plus lointains sont remis à jour par l'instantané suivant ;
* ``{"e":"bba","a":0,"ts":ms,"bb":…,"ba":…}`` : meilleurs bid/ask (``best_bid_ask``) ;
* ``{"e":"trade","a":0,"ts":ms,"p":…,"s":…,"side":"BUY","tx":"0x…"}`` : ``last_trade_price``
  (côté, prix et taille du preneur) ;
* ``{"e":"tick","a":0,"ts":ms,"old":…,"new":…}``, ``{"e":"resolved","ts":ms,"win":"Up"}``,
  ``{"e":"connect","n":k}``, ``{"e":"error","err":…}``, ``{"e":"raw","msg":…}`` (type inconnu).

Les fichiers écrits par la version 1 du collecteur (messages bruts ``{"rx","msg"}``) restent
lisibles : :mod:`tradebot.polymarket_book` accepte les deux formats. Si un fichier ``<slug>.jsonl.gz``
existe déjà au lancement (autre processus), on écrit un segment ``<slug>.s<k>.jsonl.gz`` sans y
toucher. Un ``meta.json`` par marché conserve les métadonnées (jetons, priceToBeat / finalPrice et
issue quand gamma les publie ; rafraîchis pendant 6 h après la clôture).

Débit mesuré le 26/09/2026 avec le format 1 : ≈ 250 Mo/h (6 Go / 24 h) pour BTC 5m + BTC 15m +
ETH 5m, dont 80 % de ``price_change`` (jetons répétés, niveaux lointains). Le format 2 divise
la taille par ≈ 5 (≈ 1,3 Go / 24 h).

Usage : ``nohup python scripts/polymarket_live_collector.py [--series btc:5m,btc:15m,eth:5m]
>> logs/live_collector.log 2>&1 &``. Lecture seule de données publiques : aucun ordre, aucune clé.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import signal
import sys
import time
from pathlib import Path

import websockets

from tradebot.config import CACHE_DIR
from tradebot.polymarket import GAMMA_URL, PolymarketClient, UpDownMarket, parse_market

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LIVE_DIR = CACHE_DIR / "polymarket" / "live"
FORMAT = 2
LEAD_S = 150        # ouvrir la connexion 2,5 min avant l'ouverture de la fenêtre
TAIL_S = 120        # la garder 2 min après la clôture (résolution, derniers trades)
PING_S = 10
STALL_S = 45        # aucun message depuis 45 s -> reconnexion
FLUSH_S = 5.0       # vidage gzip (Z_SYNC_FLUSH) : les données sont lisibles même si le processus meurt
BAND = 0.10         # price_change conservés à ± 0,10 du milieu de leur jeton
RESOLVE_AFTER_S = 180      # 1er rafraîchissement de l'issue 3 min après la fin de la collecte
RESOLVE_MAX_AGE_S = 6 * 3600
RESOLVE_EVERY_S = 120

log = logging.getLogger("live_collector")
_DUMPS = dict(separators=(",", ":"))


class Stalled(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Encodage compact
# ---------------------------------------------------------------------------
def _mid(bb: str | None, ba: str | None) -> float | None:
    try:
        b, a = float(bb), float(ba)
    except (TypeError, ValueError):
        return None
    return (b + a) / 2.0 if b > 0 and a > 0 else None


def encode(m: dict, rx: int, asset_index: dict[str, int], band: float = BAND) -> str | None:
    """Ligne compacte pour un message décodé, ou ``None`` s'il est à ignorer (``new_market``…)."""
    et = m.get("event_type")
    a = asset_index.get(str(m.get("asset_id")))
    if et == "price_change":
        rows = []
        for c in m.get("price_changes") or []:
            ai = asset_index.get(str(c.get("asset_id")))
            if ai is None:
                continue
            mid = _mid(c.get("best_bid"), c.get("best_ask"))
            try:
                if mid is not None and abs(float(c["price"]) - mid) > band + 1e-9:
                    continue
            except (KeyError, ValueError):
                continue
            rows.append([ai, c["price"], c.get("size", "0"), str(c.get("side", ""))[:1], c.get("best_bid"), c.get("best_ask")])
        if not rows:
            return None
        return json.dumps({"rx": rx, "e": "pc", "ts": int(m["timestamp"]), "c": rows}, **_DUMPS)
    if et == "book":
        if a is None:
            return None
        return json.dumps({"rx": rx, "e": "book", "a": a, "ts": int(m["timestamp"]),
                           "b": [[l["price"], l["size"]] for l in m.get("bids") or []],
                           "k": [[l["price"], l["size"]] for l in m.get("asks") or []]}, **_DUMPS)
    if et == "best_bid_ask":
        if a is None:
            return None
        return json.dumps({"rx": rx, "e": "bba", "a": a, "ts": int(m["timestamp"]), "bb": m.get("best_bid"),
                           "ba": m.get("best_ask")}, **_DUMPS)
    if et == "last_trade_price":
        if a is None:
            return None
        return json.dumps({"rx": rx, "e": "trade", "a": a, "ts": int(m["timestamp"]), "p": m.get("price"),
                           "s": m.get("size"), "side": m.get("side"), "tx": m.get("transaction_hash")}, **_DUMPS)
    if et == "tick_size_change":
        if a is None:
            return None
        return json.dumps({"rx": rx, "e": "tick", "a": a, "ts": int(m["timestamp"]), "old": m.get("old_tick_size"),
                           "new": m.get("new_tick_size")}, **_DUMPS)
    if et == "market_resolved":
        if a is None and not any(str(x) in asset_index for x in m.get("assets_ids") or []):
            return None
        return json.dumps({"rx": rx, "e": "resolved", "ts": int(m.get("timestamp") or 0),
                           "win": m.get("winning_outcome")}, **_DUMPS)
    if et in ("new_market", None) and "assets_ids" in m:
        return None
    return json.dumps({"rx": rx, "e": "raw", "msg": m}, **_DUMPS)


def encode_raw(raw: str, rx: int, asset_index: dict[str, int]) -> list[str]:
    """Lignes compactes pour un message WebSocket brut (objet ou liste d'objets)."""
    try:
        m = json.loads(raw)
    except ValueError:
        return [json.dumps({"rx": rx, "e": "raw", "msg": raw[:2000]}, **_DUMPS)]
    msgs = m if isinstance(m, list) else [m]
    out = []
    for x in msgs:
        if not isinstance(x, dict):
            continue
        line = encode(x, rx, asset_index)
        if line is not None:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------
def segment_path(live_dir: Path, slug: str) -> Path:
    """``<slug>.jsonl.gz`` s'il n'existe pas, sinon le premier ``<slug>.s<k>.jsonl.gz`` libre."""
    path = live_dir / f"{slug}.jsonl.gz"
    k = 1
    while path.exists():
        path = live_dir / f"{slug}.s{k}.jsonl.gz"
        k += 1
    return path


def load_meta(live_dir: Path, slug: str) -> dict:
    path = live_dir / f"{slug}.meta.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    return {}


def write_meta(live_dir: Path, meta: dict) -> None:
    path = live_dir / f"{meta['slug']}.meta.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(path)


def market_meta(m: UpDownMarket, old: dict) -> dict:
    meta = dict(old)
    meta.update({"slug": m.slug, "asset": m.asset, "duration": m.duration, "start": m.start.isoformat(),
                 "end": m.end.isoformat(), "condition_id": m.condition_id, "token_up": m.token_up,
                 "token_down": m.token_down, "fee_schedule": m.fee_schedule, "twap_lookback_s": m.twap_lookback_s,
                 "format": FORMAT})
    meta.setdefault("collector_started", time.time())
    return meta


def fetch_resolution(client: PolymarketClient, slug: str) -> dict:
    """Issue et niveaux Chainlink depuis gamma (``{}`` si le marché n'est pas encore résolu)."""
    events = client._get(f"{GAMMA_URL}/events", [("slug", slug), ("limit", 1)])
    ev = (events or [{}])[0]
    if not ev:
        return {}
    fresh = parse_market((ev.get("markets") or [{}])[0], ev)
    return {"resolved_up": fresh.resolved_up, "closed": fresh.closed, "event_metadata": ev.get("eventMetadata"),
            "resolution_checked": time.time()}


def refresh_resolutions(client: PolymarketClient, live_dir: Path = LIVE_DIR, now: float | None = None) -> int:
    """Complète les ``meta.json`` finis dont l'issue manque encore (gamma publie l'issue quelques
    minutes après la clôture). Renvoie le nombre de marchés résolus lors de cet appel."""
    now = time.time() if now is None else now
    n = 0
    for path in sorted(live_dir.glob("*.meta.json")):
        try:
            meta = json.loads(path.read_text())
        except ValueError:
            continue
        fin = meta.get("collector_finished")
        if meta.get("resolved_up") is not None or not fin:
            continue
        if now - fin < RESOLVE_AFTER_S or now - fin > RESOLVE_MAX_AGE_S:
            continue
        if now - float(meta.get("resolution_checked") or 0) < RESOLVE_EVERY_S:
            continue
        try:
            info = fetch_resolution(client, meta["slug"])
        except Exception as exc:  # noqa: BLE001
            log.warning("%s : rafraîchissement de l'issue impossible (%r)", meta["slug"], exc)
            info = {"resolution_checked": now}
        meta.update(info)
        meta.pop("resolution_error", None)
        write_meta(live_dir, meta)
        if meta.get("resolved_up") is not None:
            n += 1
            log.info("%s : issue %s", meta["slug"], "Up" if meta["resolved_up"] else "Down")
    return n


# ---------------------------------------------------------------------------
# Enregistrement d'un marché
# ---------------------------------------------------------------------------
async def record_market(m: UpDownMarket, client: PolymarketClient, live_dir: Path = LIVE_DIR) -> None:
    live_dir.mkdir(parents=True, exist_ok=True)
    path = segment_path(live_dir, m.slug)
    end_at = m.end.timestamp() + TAIL_S
    meta = market_meta(m, load_meta(live_dir, m.slug))
    meta.setdefault("segments", [])
    meta["segments"].append(path.name)
    write_meta(live_dir, meta)
    asset_index = {m.token_up: 0, m.token_down: 1}
    sub = json.dumps({"assets_ids": [m.token_up, m.token_down], "type": "market", "custom_feature_enabled": True})
    n_msgs, n_reconnect, resolved_seen = 0, 0, False
    with gzip.open(path, "at", compresslevel=4) as fh:
        fh.write(json.dumps({"rx": time.time_ns(), "e": "start", "fmt": FORMAT, "slug": m.slug, "up": m.token_up,
                             "dn": m.token_down, "band": BAND}, **_DUMPS) + "\n")
        while time.time() < end_at and not resolved_seen:
            try:
                async with websockets.connect(WS_URL, max_size=None, ping_interval=None, open_timeout=15) as ws:
                    await ws.send(sub)
                    fh.write(json.dumps({"rx": time.time_ns(), "e": "connect", "n": n_reconnect}, **_DUMPS) + "\n")
                    last_ping = last_msg = last_flush = time.time()
                    while time.time() < end_at:
                        timeout = max(0.5, PING_S - (time.time() - last_ping))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if time.time() - last_msg > STALL_S:
                                raise Stalled(f"aucun message depuis {STALL_S} s")
                            await ws.send("PING")
                            last_ping = time.time()
                            continue
                        now = time.time()
                        last_msg = now
                        if raw == "PONG":
                            continue
                        rx = time.time_ns()
                        for line in encode_raw(raw, rx, asset_index):
                            fh.write(line + "\n")
                            n_msgs += 1
                            if '"e":"resolved"' in line:
                                resolved_seen = True
                        if now - last_flush >= FLUSH_S:
                            fh.flush()
                            last_flush = now
                        if resolved_seen:
                            break
            except asyncio.CancelledError:
                fh.write(json.dumps({"rx": time.time_ns(), "e": "stop", "n_msgs": n_msgs}, **_DUMPS) + "\n")
                raise
            except Exception as exc:  # noqa: BLE001 — on se reconnecte quoi qu'il arrive
                msg = repr(exc)[:300]
                fh.write(json.dumps({"rx": time.time_ns(), "e": "error", "err": msg}, **_DUMPS) + "\n")
                if "resolved" in msg and isinstance(exc, websockets.exceptions.ConnectionClosed):
                    resolved_seen = True     # « all subscribed assets resolved » : inutile de se reconnecter
                    break
                n_reconnect += 1
                log.warning("%s : reconnexion %d (%s)", m.slug, n_reconnect, msg)
                await asyncio.sleep(min(10, 1 + n_reconnect))
        fh.write(json.dumps({"rx": time.time_ns(), "e": "stop", "n_msgs": n_msgs}, **_DUMPS) + "\n")
    # issue officielle et niveaux Chainlink, s'ils sont déjà publiés
    try:
        meta.update(await asyncio.to_thread(fetch_resolution, client, m.slug))
    except Exception as exc:  # noqa: BLE001
        meta["resolution_error"] = repr(exc)[:200]
    meta.update(n_msgs=int(meta.get("n_msgs") or 0) + n_msgs, n_reconnect=int(meta.get("n_reconnect") or 0) + n_reconnect,
                collector_finished=time.time(), resolved_seen_ws=resolved_seen)
    write_meta(live_dir, meta)
    log.info("%s : %d messages, %d reconnexions, %.1f Mo", m.slug, n_msgs, n_reconnect, path.stat().st_size / 1e6)


async def scheduler(series: list[tuple[str, str]], poll_s: float = 30.0, live_dir: Path = LIVE_DIR) -> None:
    client = PolymarketClient(use_cache=False)
    tasks: dict[str, asyncio.Task] = {}
    last_refresh = 0.0
    try:
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
                    tasks[m.slug] = asyncio.create_task(record_market(m, client, live_dir))
                    log.info("%s : enregistrement lancé", m.slug)
            for slug, t in list(tasks.items()):
                if t.done():
                    if not t.cancelled() and t.exception():
                        log.error("%s : %r", slug, t.exception())
                    del tasks[slug]
            if now - last_refresh >= RESOLVE_EVERY_S:
                last_refresh = now
                try:
                    await asyncio.to_thread(refresh_resolutions, client, live_dir, now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("rafraîchissement des issues : %r", exc)
            await asyncio.sleep(poll_s)
    except asyncio.CancelledError:
        for t in tasks.values():
            t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        log.info("arrêt propre (%d enregistrements fermés)", len(tasks))
        raise


async def _main_async(series: list[tuple[str, str]], poll_s: float) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await scheduler(series, poll_s)
    except asyncio.CancelledError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--series", default="btc:5m,btc:15m,eth:5m")
    ap.add_argument("--poll", type=float, default=30.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    series = [tuple(s.split(":")) for s in args.series.split(",") if s]
    log.info("collecteur format %d, séries %s", FORMAT, series)
    asyncio.run(_main_async(series, args.poll))
    return 0


if __name__ == "__main__":
    sys.exit(main())
