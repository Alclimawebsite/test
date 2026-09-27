#!/usr/bin/env python
"""Collecteur pump.fun : enregistre TOUS les lancements et graduations, au fil de l'eau.

L'API ne sert que les ≈ 1050 derniers lancements (≈ 55 min) : sans ce collecteur, un
échantillon historique ne contiendrait que les survivants. Chaque token est enregistré au
premier passage, avec ce que l'on savait à cet instant (``seen_ms``) : liens sociaux,
description, créateur. Les trades, eux, se relisent après coup (voir ``tradebot.pumpfun``).

Sorties (``data/cache/pumpfun/``, JSON Lines, ajout seulement) :

* ``launches.jsonl``     : un instantané par nouveau token ;
* ``graduations.jsonl``  : un instantané par token vu gradué ;
* ``dex_boosts.jsonl``, ``dex_profiles.jsonl`` : flux DexScreener (boosts et fiches payés),
  horodatés à la réception, pour savoir *quand* un token a été promu.

    python scripts/pumpfun_collector.py [--every 30] [--hours 0 (sans fin)]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from tradebot import pumpfun as pf
from tradebot import pumpfun_social as ps

log = logging.getLogger("pumpfun_collector")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=float, default=30.0, help="secondes entre deux relevés des lancements")
    ap.add_argument("--grad-every", type=float, default=300.0, help="secondes entre deux relevés des graduations")
    ap.add_argument("--dex-every", type=float, default=60.0, help="secondes entre deux relevés DexScreener")
    ap.add_argument("--hours", type=float, default=0.0, help="durée (0 = sans fin)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    root = pf.CACHE
    cli = pf.PumpFunClient(per_second=3.0)
    known_l = {r["mint"] for r in pf.read_jsonl(root / "launches.jsonl")}
    known_g = {r["mint"] for r in pf.read_jsonl(root / "graduations.jsonl")}
    seen_dex: set[tuple] = set()
    log.info("reprise : %d lancements, %d graduations déjà enregistrés", len(known_l), len(known_g))

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    t_end = time.time() + a.hours * 3600 if a.hours > 0 else float("inf")
    next_g = next_d = 0.0
    n_l = n_g = 0
    while not stop["flag"] and time.time() < t_end:
        t0 = time.time()
        try:
            now = int(time.time() * 1000)
            new = cli.recent_coins(known=known_l)
            pf.append_jsonl(root / "launches.jsonl", [pf.slim(c, now) for c in reversed(new)])
            known_l.update(c["mint"] for c in new)
            n_l += len(new)
            if len(new) >= pf.LISTING_CAP:
                log.warning("%d nouveaux lancements : trou possible (plafond de l'API atteint)", len(new))
            if t0 >= next_g:
                g = cli.recent_coins(complete=True, known=known_g)
                pf.append_jsonl(root / "graduations.jsonl", [pf.slim(c, now) for c in reversed(g)])
                known_g.update(c["mint"] for c in g)
                n_g += len(g)
                next_g = t0 + a.grad_every
            if t0 >= next_d:
                for kind, rows in ps.dexscreener_feeds(cli).items():
                    fresh = [r for r in rows if (kind, r.get("tokenAddress"), r.get("totalAmount"), r.get("amount")) not in seen_dex]
                    seen_dex.update((kind, r.get("tokenAddress"), r.get("totalAmount"), r.get("amount")) for r in fresh)
                    pf.append_jsonl(root / f"dex_{kind}.jsonl", [{**r, "seen_ms": now} for r in fresh])
                next_d = t0 + a.dex_every
            log.info("+%d lancements (total %d), +%d graduations cumulées", len(new), len(known_l), n_g)
        except Exception as exc:  # noqa: BLE001 — le collecteur ne doit pas s'arrêter
            log.warning("relevé en échec : %r", exc)
        time.sleep(max(1.0, a.every - (time.time() - t0)))
    log.info("arrêt : %d lancements et %d graduations ajoutés", n_l, n_g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
