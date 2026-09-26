"""Construit la page autonome « Vigie Up/Down » en mode rejeu (site/rejeu/vigie_rejeu.html).

Une page publiée comme artefact ne peut ouvrir aucune connexion externe : elle rejoue donc, seconde
par seconde, les marchés enregistrés par les collecteurs (``reports/latence/series_alignees_1s.csv.gz``
produit par ``scripts/latency_study.py``), avec la courbe P&L / latence de la même étude. Les données
sont injectées en JSON dans ``site/rejeu/template.html`` (aucun appel réseau, aucune clé).

    python scripts/build_replay_artifact.py [--latence reports/latence] [--out site/rejeu/vigie_rejeu.html]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "site" / "rejeu" / "template.html"
PLACEHOLDER = "/*__DATA__*/"


def _r(x, n: int):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), n)


def _outcome(row: pd.Series):
    for key in ("official_up", "outcome_up"):
        v = row.get(key)
        if v is not None and not pd.isna(v):
            return bool(v)
    return None


def markets_payload(series: pd.DataFrame, marches: pd.DataFrame) -> list[dict]:
    meta = marches.set_index("slug")
    out = []
    for slug, g in series.groupby("market", sort=False):
        g = g.sort_values("t_rel_s")
        row = meta.loc[slug]
        asset, _, dur, start = slug.split("-")
        out.append({
            "slug": slug, "asset": asset, "dur": dur, "S": int(start), "E": int(row["E"]),
            "t0": int(g["t_rel_s"].iloc[0]),
            "up": _outcome(row), "binUp": bool(row["binance_says_up"]),
            "bn": [_r(x, 2) for x in g["binance_mid"]],
            "cl": [_r(x, 3) for x in g["chainlink"]],
            "bid": [_r(x, 3) for x in g["up_bid"]],
            "ask": [_r(x, 3) for x in g["up_ask"]],
            "pf": [_r(x, 4) for x in g["p_formule"]],
            "sg": [_r(x, 4) for x in g["sigma_bp_per_s"]],
        })
        if len(set(g["t_rel_s"].diff().dropna())) > 1:
            raise ValueError(f"{slug} : série non régulière à la seconde")
    out.sort(key=lambda d: (d["S"], d["asset"], d["dur"]))
    return out


def latency_curve(pnl: pd.DataFrame) -> list[dict]:
    """P&L papier par heure selon la latence : prix périmés après un saut de Binance, marge 0, valorisés à +10 s."""
    s = pnl[(pnl["trigger"] == "saut") & (pnl["margin"] == 0) & (pnl["valeur"] == "markout")].sort_values("latency_ms")
    return [{"l": int(r.latency_ms), "v": round(r.per_hour), "lo": round(r.per_hour_lo), "hi": round(r.per_hour_hi),
             "f": round(r.fill_rate, 3)} for r in s.itertuples()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--latence", type=Path, default=ROOT / "reports" / "latence")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "rejeu" / "vigie_rejeu.html")
    args = ap.parse_args()

    series = pd.read_csv(args.latence / "series_alignees_1s.csv.gz")
    marches = pd.read_csv(args.latence / "marches.csv")
    pnl = pd.read_csv(args.latence / "pnl_vs_latence.csv")
    data = markets_payload(series, marches)
    curve = latency_curve(pnl)

    blob = ("window.REPLAY=" + json.dumps(data, separators=(",", ":"), ensure_ascii=False) + ";\n"
            "window.LATCURVE=" + json.dumps(curve, separators=(",", ":")) + ";")
    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise SystemExit(f"repère {PLACEHOLDER} absent de {TEMPLATE}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html.replace(PLACEHOLDER, blob), encoding="utf-8")
    print(f"{args.out} : {len(data)} marchés, {args.out.stat().st_size / 1024:.0f} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
