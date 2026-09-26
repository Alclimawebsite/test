"""Enregistre à la milliseconde les prix de référence utiles au calcul de la latence.

Flux (publics, sans clé) :
* Binance ``data-stream.binance.vision`` : ``bookTicker`` et ``aggTrade`` de BTCUSDT et ETHUSDT ;
* Coinbase ``ws-feed.exchange.coinbase.com`` : ``ticker`` BTC-USD et ETH-USD ;
* Polymarket RTDS ``ws-live-data.polymarket.com`` : ``crypto_prices_chainlink`` (btc/usd, eth/usd :
  le flux Chainlink qui sert à la résolution des marchés Up or Down) et ``crypto_prices``.

Chaque message est écrit tel quel avec l'heure locale de réception (ns) dans
``data/cache/cex_ws/<source>_<AAAAMMJJHH>.jsonl.gz`` (un fichier par source et par heure UTC).
Même horloge que ``polymarket_live_collector.py`` : les deux enregistrements se comparent
directement. Lecture seule, aucune clé.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import sys
import time

import websockets

from tradebot.config import CACHE_DIR

OUT_DIR = CACHE_DIR / "cex_ws"
log = logging.getLogger("cex_ws")

FEEDS = {
    "binance": {
        "url": "wss://data-stream.binance.vision/stream?streams="
               "btcusdt@bookTicker/ethusdt@bookTicker/btcusdt@aggTrade/ethusdt@aggTrade",
        "sub": None, "ping": None,
    },
    "coinbase": {
        "url": "wss://ws-feed.exchange.coinbase.com",
        "sub": [{"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"], "channels": ["ticker"]}],
        "ping": None,
    },
    "rtds": {
        "url": "wss://ws-live-data.polymarket.com",
        "sub": [{"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'},
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"eth/usd"}'},
            {"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"btcusdt"}'},
            {"topic": "crypto_prices", "type": "*", "filters": '{"symbol":"ethusdt"}'},
        ]}],
        "ping": 5.0,
    },
}


class HourlyWriter:
    def __init__(self, source: str):
        self.source, self.hour, self.fh = source, None, None

    def write(self, rx_ns: int, raw: str) -> None:
        hour = time.strftime("%Y%m%d%H", time.gmtime(rx_ns / 1e9))
        if hour != self.hour:
            if self.fh:
                self.fh.close()
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            self.fh = gzip.open(OUT_DIR / f"{self.source}_{hour}.jsonl.gz", "at", compresslevel=3)
            self.hour = hour
        self.fh.write('{"rx":%d,"msg":%s}\n' % (rx_ns, json.dumps(raw)))

    def flush(self) -> None:
        if self.fh:
            self.fh.flush()


async def run_feed(name: str, cfg: dict) -> None:
    w = HourlyWriter(name)
    n_err = 0
    last_flush = time.time()
    while True:
        try:
            async with websockets.connect(cfg["url"], max_size=None, ping_interval=20, open_timeout=15) as ws:
                for s in cfg["sub"] or []:
                    await ws.send(json.dumps(s))
                log.info("%s : connecté", name)
                n_err = 0
                last_ping = time.time()
                while True:
                    timeout = 30.0 if not cfg["ping"] else max(0.2, cfg["ping"] - (time.time() - last_ping))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        if cfg["ping"]:
                            await ws.send("PING")
                            last_ping = time.time()
                            continue
                        raise ConnectionError("30 s sans message")
                    if cfg["ping"] and time.time() - last_ping >= cfg["ping"]:
                        await ws.send("PING")
                        last_ping = time.time()
                    if not raw or raw == "PONG":
                        continue
                    w.write(time.time_ns(), raw if isinstance(raw, str) else raw.decode())
                    if time.time() - last_flush > 5:
                        w.flush()
                        last_flush = time.time()
        except Exception as exc:  # noqa: BLE001 — reconnexion systématique
            n_err += 1
            log.warning("%s : reconnexion %d (%r)", name, n_err, exc)
            w.flush()
            await asyncio.sleep(min(30, 2 ** min(n_err, 5)))


async def main_async(sources: list[str]) -> None:
    await asyncio.gather(*(run_feed(s, FEEDS[s]) for s in sources))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sources", default="binance,coinbase,rtds")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main_async([s for s in args.sources.split(",") if s]))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
