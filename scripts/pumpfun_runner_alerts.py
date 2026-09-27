#!/usr/bin/env python
"""Détecteur live de runners pump.fun — SIMULATION PAPIER, aucun ordre.

Suit en continu :

* les nouveaux lancements (liste triée par date de création) : évaluation 1, 5 et 15 min
  après la création ;
* les tokens qui viennent de trader (liste triée par dernier trade) : ceux qui franchissent
  50 % de la courbe sont évalués à ce moment ;
* les graduations : évaluation 5 min après le premier trade sur PumpSwap.

À chaque moment, les variables sont calculées exactement comme dans l'étude
(``tradebot.pumpfun_runners.features_at``) puis notées par la logistique exportée par
``scripts/pumpfun_runners_study.py`` (``reports/pumpfun_runners/modele_{moment}_{cible}_{h}h.json``).
Au-dessus du seuil (par défaut : celui du top 10 % de l'échantillon d'entraînement), une
alerte est écrite dans ``logs/pumpfun_alerts.jsonl`` avec le prix d'entrée papier, pour que
l'étude puisse ensuite vérifier l'issue. Sans modèle pour un moment, rien n'est noté : une
règle non testée ne produit pas d'alerte.

Signaux sociaux : liens déclarés au lancement, promotion payée DexScreener (datée) et, si
``X_BEARER_TOKEN`` est défini, le nombre de posts X citant l'adresse du token dans l'heure
(affiché avec l'alerte, pas utilisé par le score tant que l'étude ne l'a pas mesuré).

    python scripts/pumpfun_runner_alerts.py [--target x10] [--h 6] [--threshold auto] [--hours 0]
"""

from __future__ import annotations

import argparse
import heapq
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np

from tradebot import pumpfun as pf
from tradebot import pumpfun_runners as pr
from tradebot import pumpfun_social as ps
from tradebot.config import REPORTS_DIR, ROOT

log = logging.getLogger("pumpfun_alerts")
MODELS_DIR = REPORTS_DIR / "pumpfun_runners"
ALERTS = ROOT / "logs" / "pumpfun_alerts.jsonl"
SHOWN = ("n_buyers", "buy_sol", "sell_ratio", "top10_share", "dev_buy_sol", "dev_sold", "snipe_n3", "progress",
         "tw_profile", "tw_community", "has_telegram", "dex_paid_profile", "dex_boost_amount")


def load_models(target: str, h: int) -> dict[str, dict]:
    out = {}
    for m in pr.MOMENTS:
        p = MODELS_DIR / f"modele_{m}_{target}_{h}h.json"
        if p.exists():
            out[m] = json.loads(p.read_text())
    return out


class Detector:
    def __init__(self, cli: pf.PumpFunClient, dex_cli: pf.PumpFunClient, models: dict[str, dict],
                 threshold: float | None, costs: pr.Costs):
        self.cli, self.dex_cli, self.models, self.threshold, self.costs = cli, dex_cli, models, threshold, costs
        self.coins: dict[str, dict] = {}
        self.queue: list[tuple[float, str, str]] = []          # (instant t, mint, moment)
        self.done: set[tuple[str, str]] = set()
        self.x = ps.XCounts.from_env()

    def schedule(self, mint: str, moment: str, t_ms: float) -> None:
        if (mint, moment) not in self.done and moment in self.models:
            self.done.add((mint, moment))
            heapq.heappush(self.queue, (t_ms, mint, moment))

    # -- relevés -----------------------------------------------------------
    def poll_launches(self) -> None:
        for c in self.cli.recent_coins(known=set(self.coins)):
            if not pf.is_standard_curve(c):
                continue
            self.coins[c["mint"]] = c
            for m, k in pr.LAUNCH_OFFSETS.items():
                self.schedule(c["mint"], m, c["created_timestamp"] + k * pr.MIN_MS)

    def poll_active(self, pages: int = 2) -> None:
        p50 = float(pf.curve_price(pr.PRE_GRAD_PROGRESS)) * pf.TOTAL_SUPPLY      # market cap en SOL à 50 %
        for off in range(0, 50 * pages, 50):
            for c in self.cli.coins(offset=off, sort="last_trade_timestamp"):
                if not pf.is_standard_curve(c) or c.get("complete"):
                    continue
                self.coins.setdefault(c["mint"], c)
                if (c.get("market_cap") or 0) >= p50:
                    self.schedule(c["mint"], "pre_grad", time.time() * 1000)

    def poll_graduations(self) -> None:
        for c in self.cli.coins(offset=0, complete=True):
            if not pf.is_standard_curve(c) or (c["mint"], "post_grad") in self.done:
                continue
            self.coins.setdefault(c["mint"], c)
            self.schedule(c["mint"], "post_grad", time.time() * 1000 + pr.POST_GRAD_DELAY_MS)

    # -- évaluation --------------------------------------------------------
    def evaluate(self, mint: str, moment: str, t_ms: int) -> dict | None:
        coin = self.coins[mint]
        created = int(coin["created_timestamp"])
        start = created if moment.startswith("launch") else t_ms - pr.WINDOW_MS
        tr = self.cli.trades(mint, end_ms=t_ms, start_ms=start, max_pages=10)
        if moment.startswith("launch") and tr.attrs.get("truncated"):
            return None
        candles = pr.splice_candles([]) if moment.startswith("launch") else pr.fetch_candles(self.cli, mint, created)
        orders = None
        if len(tr) >= 20 or not moment.startswith("launch"):
            try:
                orders = ps.dexscreener_orders(self.dex_cli, mint)
            except Exception:  # noqa: BLE001
                orders = None
        f = pr.features_at(moment, t_ms, coin, tr, candles, orders)
        if moment == "pre_grad" and not (f.get("progress", 0) >= pr.PRE_GRAD_PROGRESS):
            return None
        model = self.models[moment]
        score = pr.score_logit(model, f)
        thr = self.threshold if self.threshold is not None else model["score_top10_threshold"]
        out = {"t_ms": t_ms, "mint": mint, "symbol": coin.get("symbol"), "moment": moment, "score": score,
               "threshold": thr, "alert": score >= thr, "price_sol": f.get("price_sol"),
               "features": {k: f.get(k) for k in SHOWN}, "model": f"{model['target']}_{model['h']}h",
               "url": f"https://pump.fun/coin/{mint}"}
        if out["alert"] and self.x is not None:
            out["x_posts_1h"] = self.x.mentions(mint, coin.get("symbol") or "", t_ms - pr.HOUR_MS, t_ms - 15_000)
        return out

    def run_due(self) -> int:
        n = 0
        now = time.time() * 1000
        while self.queue and self.queue[0][0] <= now - 2000:          # 2 s : les trades de t sont publiés
            t, mint, moment = heapq.heappop(self.queue)
            if now - t > 10 * pr.MIN_MS:                              # trop tard : l'instant de décision est passé
                continue
            try:
                r = self.evaluate(mint, moment, int(t))
            except Exception as exc:  # noqa: BLE001
                log.warning("%s %s : %r", mint, moment, exc)
                continue
            if r is None:
                continue
            n += 1
            pf.append_jsonl(ALERTS, [r])
            if r["alert"]:
                log.warning("ALERTE %s %-10s %-10s score %.3f (seuil %.3f) %s", r["model"], r["moment"],
                            r["symbol"], r["score"], r["threshold"], r["url"])
        return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="x10", choices=("x10", "x3", "grad", "mcap1m", "mcap100k"))
    ap.add_argument("--h", type=int, default=6)
    ap.add_argument("--threshold", type=float, default=None, help="seuil de score (défaut : top 10 %% de l'entraînement)")
    ap.add_argument("--hours", type=float, default=0.0, help="durée (0 = sans fin)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    models = load_models(a.target, a.h)
    if not models:
        log.error("aucun modèle %s_%sh dans %s : lancer d'abord scripts/pumpfun_runners_study.py", a.target, a.h, MODELS_DIR)
        return 1
    log.info("modèles chargés : %s", ", ".join(f"{m} (n={v['n']}, positifs={v['n_pos']})" for m, v in models.items()))
    det = Detector(pf.PumpFunClient(per_second=1.0), pf.PumpFunClient(per_second=0.9), models, a.threshold, pr.Costs())
    t_end = time.time() + a.hours * 3600 if a.hours > 0 else math.inf
    next_act = next_grad = 0.0
    while time.time() < t_end:
        t0 = time.time()
        try:
            det.poll_launches()
            if t0 >= next_act and "pre_grad" in models:
                det.poll_active()
                next_act = t0 + 60
            if t0 >= next_grad and "post_grad" in models:
                det.poll_graduations()
                next_grad = t0 + 60
            n = det.run_due()
            log.info("%d évaluations, %d en attente", n, len(det.queue))
        except Exception as exc:  # noqa: BLE001
            log.warning("cycle en échec : %r", exc)
        time.sleep(max(1.0, 15 - (time.time() - t0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
