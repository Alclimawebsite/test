"""Arbitrages entre marchés Polymarket « Up or Down » logiquement liés (BTC 5m / 15m, 4h).

Question : existe-t-il des moments où l'on gagne à coup sûr, quel que soit le résultat (Up ou
Down), combien, et est-ce exécutable ? Prédire le sens à coup sûr est impossible ; la seule voie
est l'arbitrage entre marchés dont les issues sont liées par la chaîne de niveaux Chainlink
(voir :mod:`tradebot.polymarket_arbitrage`).

Étapes
------
1. **Chaîne K/F** : égalités ``priceToBeat``/``finalPrice`` entre 5m, 15m et 4h (caches
   ``event_meta`` + métadonnées 4h lues sur gamma), contrôle des implications logiques sur les
   issues officielles, tableau des 5m consécutives (aucune contrainte logique).
2. **Historique 04/09 → 24/09** (trades preneurs datés au bloc) : pour chaque 15m et ses trois
   5m, prix d'achat estimé de chaque jeton à chaque seconde (dernier achat preneur de ce jeton
   il y a moins de 5 s ; variantes « + 1 c » et « moins de 2 s »), niveaux connus à cet instant,
   coût minimal d'un paiement garanti de 1 $ (sommets du polyèdre couvrant = programme linéaire
   exact), épisodes, programme linéaire avec tailles pour 10 / 50 / 100 parts. C'est une
   DÉTECTION, pas une preuve d'exécution.
3. **Carnet réel à la milliseconde** (collecteur WebSocket, BTC 5m et 15m enregistrés
   simultanément) : mêmes calculs sur les vrais meilleurs asks et tailles (3 niveaux), durée de
   vie des violations en ms, profit exécutable si l'ordre arrive 0 / 100 / 300 / 1 000 / 3 000 ms
   plus tard (les deux jambes doivent être remplies : la jambe la plus pauvre limite le lot).
   Niveaux ``V`` recalculés depuis Chainlink RTDS (TWAP des 60 points ``[T−62 s, T−3 s]``) quand
   le flux est enregistré.
4. **Issue déjà connue** : achat du gagnant après la clôture (ou dès que le TWAP est fixé).
5. README (français), CSV, PNG dans ``reports/polymarket/arbitrage/``.

Usage :

    . .venv/bin/activate
    python scripts/polymarket_arbitrage.py                 # tout
    python scripts/polymarket_arbitrage.py --days 2026-09-10 --max-live-groups 2   # essai rapide

Simulation papier / lecture seule : aucun ordre, aucune clé. Seules requêtes réseau : métadonnées
4h sur gamma (lecture seule, désactivables avec ``--no-network``).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot import polymarket as pm  # noqa: E402
from tradebot import polymarket_arbitrage as pa  # noqa: E402
from tradebot import polymarket_book as bk  # noqa: E402
from tradebot.config import CACHE_DIR, REPORTS_DIR  # noqa: E402
from tradebot.report import fmt_number, to_markdown  # noqa: E402

log = logging.getLogger("arbitrage")

TRADES_DIR = CACHE_DIR / "polymarket" / "wallets" / "trades"
META_FILES = (CACHE_DIR / "timesfm_amplitude" / "event_meta.parquet", CACHE_DIR / "diag" / "event_meta.parquet")
RTDS_DIR = CACHE_DIR / "cex_ws"
DAYS = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-09-04", "2026-09-24", freq="D")]
VARIANTS = {
    "A": {"max_age": 5.0, "plus": 0.0, "mode": "last", "label": "dernier achat < 5 s"},
    "B": {"max_age": 5.0, "plus": 0.01, "mode": "last", "label": "dernier achat < 5 s, + 1 c"},
    "C": {"max_age": 2.0, "plus": 0.0, "mode": "block_max", "label": "prix max du dernier bloc < 2 s"},
}
SIZES = (10, 50, 100)
LATENCIES_MS = (0, 100, 300, 1000, 3000)
LIVE_PLAN_SETS = 100
BIG = 1e6
TWAP_OFFSET_S = 3          # V(T) ≈ moyenne des points Chainlink RTDS d'horodatage T−62 … T−3 (parfois T−61 … T−2)
RTDS_OFFSETS = (0, 1, 2, 3, 4, 5, 6)
PHASES = ("avant S", "avant clôture #1", "entre #1 et #2", "après clôture #2")
SEGMENTS = ((-60, 0), (0, 300), (300, 600), (600, 900))
LIVE_SEGMENTS = ((-150, 0), (0, 300), (300, 600), (600, 900))
MARGIN_BINS = np.arange(-60.0, 20.01, 0.5)          # marge en cents
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW, C_VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#4a3aa7"
L = pa.GROUP_LEGS


def fr(x, nd=2, signed=False, pct=False) -> str:
    return fmt_number(x, nd, signed=signed, pct=pct)


def cents(x, nd=1, signed=False) -> str:
    return (fmt_number(100 * x, nd, signed=signed) + " c") if x is not None and np.isfinite(x) else "—"


def usd(x, nd=2) -> str:
    return (fmt_number(x, nd) + " $") if x is not None and np.isfinite(x) else "—"


class Timer:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, name: str):
        timer = self

        class _Ctx:
            def __enter__(self):
                self.t = time.time()
                log.info("… %s", name)

            def __exit__(self, *exc):
                dt = time.time() - self.t
                timer.rows.append({"etape": name, "secondes": round(dt, 1)})
                log.info("%s : %.1f s", name, dt)

        return _Ctx()


# ---------------------------------------------------------------------------
# 1. Niveaux de la chaîne
# ---------------------------------------------------------------------------
def load_meta() -> pd.DataFrame:
    parts = [pd.read_parquet(p)[["slug", "price_to_beat", "final_price"]] for p in META_FILES if p.exists()]
    m = pd.concat(parts, ignore_index=True)
    m = m.sort_values("final_price", na_position="last").drop_duplicates("slug", keep="first")
    return m.reset_index(drop=True)


def fetch_4h_meta(asset: str, start: str, end: str) -> pd.DataFrame:
    """``eventMetadata`` des 4h (gamma, lecture seule, en mémoire)."""
    client = pm.PolymarketClient(use_cache=False)
    starts = pm.slot_starts("4h", pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"))
    slugs = [pm.updown_slug(asset, "4h", s) for s in starts]
    rows = []
    for i in range(0, len(slugs), pm.GAMMA_BATCH):
        batch = slugs[i: i + pm.GAMMA_BATCH]
        evs = client._get(f"{pm.GAMMA_URL}/events", [("slug", s) for s in batch] + [("limit", len(batch))]) or []
        for ev in evs:
            md = ev.get("eventMetadata") or {}
            rows.append({"slug": ev.get("slug"), "price_to_beat": pm._f(md.get("priceToBeat")),
                         "final_price": pm._f(md.get("finalPrice"))})
    return pd.DataFrame(rows, columns=["slug", "price_to_beat", "final_price"])


def level_map(meta: pd.DataFrame, asset: str = "btc") -> dict[int, float]:
    """Niveau de la chaîne à chaque instant T : ``K`` des marchés qui commencent à T, ``F`` de ceux
    qui finissent à T (égaux, vérifié)."""
    lv: dict[int, float] = {}
    for slug, k, f in meta[["slug", "price_to_beat", "final_price"]].itertuples(index=False):
        a, d, s = pa.parse_slug(slug)
        if a != asset or d not in ("5m", "15m"):
            continue
        e = s + (300 if d == "5m" else 900)
        if np.isfinite(k):
            lv.setdefault(s, float(k))
        if np.isfinite(f):
            lv.setdefault(e, float(f))
    return lv


def group_values(lv: dict[int, float], S: int) -> dict[int, float]:
    return {k: lv.get(S + 300 * k, math.nan) for k in range(4)}


def official_outcomes(start: str, end: str) -> dict[str, bool]:
    client = pm.PolymarketClient()
    out = {}
    for dur in ("5m", "15m"):
        for m in client.list_updown_markets("btc", dur, pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                                            only_closed=True):
            if m.resolved_up is not None:
                out[m.slug] = bool(m.resolved_up)
    return out


def logic_checks(lv: dict[int, float], outcomes: dict[str, bool], starts: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Implications logiques sur les issues OFFICIELLES et tableau des 5m consécutives."""
    rows = []
    cnt = {"n": 0, "tous_up_15m_down": 0, "tous_down_15m_up": 0, "v2_ge_v0_5m3_up_15m_down": 0,
           "v2_lt_v0_5m3_down_15m_up": 0, "issue_differente_de_la_chaine": 0, "n_issues": 0}
    for S in starts:
        o15 = outcomes.get(f"btc-updown-15m-{S}")
        o5 = [outcomes.get(f"btc-updown-5m-{S + 300 * k}") for k in range(3)]
        V = group_values(lv, S)
        if o15 is None or any(o is None for o in o5):
            continue
        cnt["n"] += 1
        cnt["tous_up_15m_down"] += all(o5) and not o15
        cnt["tous_down_15m_up"] += (not any(o5)) and o15
        if np.isfinite(V[2]) and np.isfinite(V[0]):
            cnt["v2_ge_v0_5m3_up_15m_down"] += (V[2] >= V[0]) and o5[2] and not o15
            cnt["v2_lt_v0_5m3_down_15m_up"] += (V[2] < V[0]) and (not o5[2]) and o15
        if all(np.isfinite(list(V.values()))):
            chain = [V[1] >= V[0], V[2] >= V[1], V[3] >= V[2]]
            cnt["n_issues"] += 4
            cnt["issue_differente_de_la_chaine"] += int(sum(c != o for c, o in zip(chain, o5)) + ((V[3] >= V[0]) != o15))
    labels = {
        "tous_up_15m_down": "trois 5m Up mais 15m Down (impossible)",
        "tous_down_15m_up": "trois 5m Down mais 15m Up (impossible)",
        "v2_ge_v0_5m3_up_15m_down": "V2 ≥ V0, 5m#3 Up mais 15m Down (impossible)",
        "v2_lt_v0_5m3_down_15m_up": "V2 < V0, 5m#3 Down mais 15m Up (impossible)",
        "issue_differente_de_la_chaine": "issue officielle ≠ comparaison des niveaux de la chaîne",
    }
    for k, lab in labels.items():
        rows.append({"controle": lab, "n": cnt["n_issues"] if k == "issue_differente_de_la_chaine" else cnt["n"],
                     "violations": int(cnt[k])})
    # 5m consécutives : table 2×2 (aucune contrainte logique)
    pairs = []
    all5 = sorted(int(s.split("-")[-1]) for s in outcomes if "-5m-" in s)
    set5 = set(all5)
    for s in all5:
        if s + 300 in set5:
            pairs.append((outcomes[f"btc-updown-5m-{s}"], outcomes[f"btc-updown-5m-{s + 300}"]))
    P = pd.DataFrame(pairs, columns=["n", "n1"])
    tab = pd.crosstab(P["n"].map({True: "Up", False: "Down"}), P["n1"].map({True: "Up", False: "Down"}))
    tab.index.name, tab.columns.name = "5m n", "5m n+1"
    tab2 = tab.reset_index().melt(id_vars="5m n", var_name="5m n+1", value_name="n")
    tab2["part"] = tab2["n"] / tab2["n"].sum()
    return pd.DataFrame(rows), tab2


# ---------------------------------------------------------------------------
# 2. Historique (trades preneurs)
# ---------------------------------------------------------------------------
def load_day_trades(day: str) -> pd.DataFrame:
    cols = ["ts", "side", "outcome", "price", "size", "slug", "seq"]
    frames = []
    for dur in ("5m", "15m"):
        tk = TRADES_DIR / f"btc_{dur}_{day}_taker.parquet"
        al = TRADES_DIR / f"btc_{dur}_{day}_all.parquet"
        got: set[str] = set()
        if tk.exists():
            d = pd.read_parquet(tk, columns=cols)
            d["slug"] = d["slug"].astype(str)
            got = set(d["slug"].unique())
            frames.append(d)
        if al.exists():
            d = pd.read_parquet(al, columns=cols + ["role"])
            d = d[d["role"].astype(str) == "taker"][cols]
            d["slug"] = d["slug"].astype(str)
            frames.append(d[~d["slug"].isin(got)])
    if not frames:
        return pd.DataFrame(columns=cols + ["t"])
    tr = pd.concat(frames, ignore_index=True)
    for c in ("side", "outcome"):
        tr[c] = tr[c].astype(str)
    tr["t"] = (tr["ts"].astype("int64") // 10**9).astype(np.int64)
    return tr.sort_values(["slug", "t", "seq"], kind="stable").reset_index(drop=True)


def _segment_setup(legs, active, known):
    sub = tuple(legs[i] for i in active)
    A = pa.payoff_matrix(pa.possible_outcomes(sub, known))
    V = pa.cover_vertices(A)
    cols = [c for i in active for c in (2 * i, 2 * i + 1)]
    return A, V, cols


def _cross_vertices(V: np.ndarray) -> np.ndarray:
    """Sommets qui mêlent au moins deux marchés (hors « Up + Down » d'un même marché)."""
    if not len(V):
        return V
    n_mk = ((V[:, 0::2] + V[:, 1::2]) > 1e-12).sum(axis=1)
    return V[n_mk >= 2]


def history_group(S: int, by_slug: dict[str, pd.DataFrame], V: dict[int, float]) -> dict:
    slugs = [f"btc-updown-15m-{S}", f"btc-updown-5m-{S}", f"btc-updown-5m-{S + 300}", f"btc-updown-5m-{S + 600}"]
    grid = np.arange(S - 60, S + 900, dtype=np.int64)
    t_rel = grid - S
    N = len(grid)
    tok = []
    for slug in slugs:
        d = by_slug.get(slug)
        for side in ("Up", "Down"):
            if d is None:
                tok.append((np.zeros(0), np.zeros(0), np.zeros(0)))
                continue
            sel = d[(d["side"] == "BUY") & (d["outcome"] == side) & (d["t"] >= S - 70) & (d["t"] < S + 900)]
            tok.append((sel["t"].to_numpy(), sel["price"].to_numpy(), sel["size"].to_numpy()))
    seg_id = np.full(N, -1)
    setups = []
    for k, (a, b) in enumerate(SEGMENTS):
        seg_id[(t_rel >= a) & (t_rel < b)] = k
        active, known = pa.group_state(a, L, V)
        setups.append((active, known) + _segment_setup(L, active, known))
    names = pa.token_names(L)
    out = {"episodes": [], "groups": [], "incons": [], "hist": {}}
    for vname, var in VARIANTS.items():
        P = np.full((N, 8), np.nan)
        SZ = np.full((N, 8), np.nan)
        AGE = np.full((N, 8), np.nan)
        for i, (ts, p, s) in enumerate(tok):
            P[:, i], SZ[:, i], AGE[:, i] = pa.last_trade_grid(ts, p, s, grid, var["max_age"], var["mode"])
        P = P + var["plus"]
        P[P >= 1.0] = np.nan
        # un marché : ask Up + ask Down >= 1 (carnet unifié) ; sinon l'estimation la plus ancienne est périmée
        for m in range(4):
            u, dn = 2 * m, 2 * m + 1
            both = np.isfinite(P[:, u]) & np.isfinite(P[:, dn])
            bad = both & (P[:, u] + P[:, dn] < 1.0 - 1e-9)
            with np.errstate(invalid="ignore"):
                bad_fee = both & (pa.taker_cost(P[:, u]) + pa.taker_cost(P[:, dn]) < 1.0 - 1e-9)
            for k in range(4):
                sel = seg_id == k
                out["incons"].append({"variante": vname, "marche": L[m].name, "phase": PHASES[k],
                                      "secondes_deux_cotes": int((both & sel).sum()), "secondes_somme_lt_1": int((bad & sel).sum()),
                                      "secondes_cout_lt_1": int((bad_fee & sel).sum())})
            old_u = bad & (AGE[:, u] >= AGE[:, dn])
            old_d = bad & (AGE[:, dn] >= AGE[:, u])
            P[old_u, u] = np.nan
            P[old_d, dn] = np.nan
        C = np.where(np.isfinite(P), pa.taker_cost(np.nan_to_num(P, nan=0.5)), np.inf)
        margin = np.full(N, np.nan)
        vid = np.full(N, -1)
        for k, (active, known, A, Vx, cols) in enumerate(setups):
            rows = seg_id == k
            best, idx = pa.min_cover_cost(C[rows][:, cols], Vx)
            mg = 1.0 - best
            mg[~np.isfinite(mg)] = np.nan
            margin[rows] = mg
            vid[rows] = idx
            bx, _ = pa.min_cover_cost(C[rows][:, cols], _cross_vertices(Vx))
            mx = 1.0 - bx                      # marge de la meilleure couverture inter-marchés
            fin = np.isfinite(mx)
            h = np.histogram(np.clip(100 * mx[fin], MARGIN_BINS[0], MARGIN_BINS[-1] - 1e-9), bins=MARGIN_BINS)[0]
            key = (vname, PHASES[k])
            out["hist"][key] = out["hist"].get(key, 0) + h
        flag = np.nan_to_num(margin, nan=-1.0) > 1e-9
        eps = pa.episodes(t_rel, flag, max_gap=1, keys=seg_id)
        g = {"variante": vname, "S": S, "slug15": slugs[0], "marge_max": float(np.nanmax(margin)) if np.isfinite(margin).any() else np.nan,
             "n_episodes": len(eps), "secondes_arbitrage": int(flag.sum())}
        if np.isfinite(g["marge_max"]):
            g["phase_marge_max"] = PHASES[int(seg_id[int(np.nanargmax(margin))])]
        out["groups"].append(g)
        for i0, i1 in eps:
            k = int(seg_id[i0])
            active, known, A, Vx, cols = setups[k]
            v = Vx[vid[i0]]
            legs_i = [cols[j] for j in np.flatnonzero(v > 1e-12)]
            prices = P[i0, cols]
            sizes = SZ[i0, cols]
            row = {"variante": vname, "slug15": slugs[0], "S": S, "jour": pd.Timestamp(S, unit="s").strftime("%Y-%m-%d"),
                   "phase": PHASES[k], "t0_rel_s": int(t_rel[i0]), "t1_rel_s": int(t_rel[i1]), "duree_s": int(t_rel[i1] - t_rel[i0] + 1),
                   "marge_debut": float(margin[i0]), "marge_max": float(np.nanmax(margin[i0:i1 + 1])),
                   "portefeuille": " + ".join(f"{names[j]}" + ("" if abs(v[cols.index(j)] - 1) < 1e-9 else f" ×{v[cols.index(j)]:.2g}")
                                              for j in legs_i),
                   "prix_jambes": " / ".join(f"{P[i0, j]:.3f}" for j in legs_i),
                   "ages_jambes_s": " / ".join(f"{AGE[i0, j]:.0f}" for j in legs_i),
                   "tailles_jambes": " / ".join(f"{SZ[i0, j]:.1f}" for j in legs_i),
                   "ecart_ages_s": float(np.nanmax(AGE[i0, legs_i]) - np.nanmin(AGE[i0, legs_i])),
                   "taille_min": float(np.nanmin(SZ[i0, legs_i]))}
            for n in SIZES:
                r = pa.solve_arbitrage(A, list(prices), list(sizes), max_sets=n)
                row[f"profit_{n}"] = r.profit
                row[f"lots_{n}"] = r.payout
                row[f"profit_{n}_sans_limite"] = n * row["marge_debut"]
            out["episodes"].append(row)
    return out


def sniping_history(S: int, by_slug: dict[str, pd.DataFrame], V: dict[int, float]) -> list[dict]:
    """Achats preneurs du gagnant quand l'issue est déjà fixée : bloc daté >= E (après la clôture)
    et, à part, blocs datés E − 2 s et E − 1 s (le TWAP est figé à E − 3 s, connu via RTDS vers
    E − 1,7 s)."""
    rows = []
    mk = [(f"btc-updown-15m-{S}", 0, 3, S + 900)] + [(f"btc-updown-5m-{S + 300 * k}", k, k + 1, S + 300 * (k + 1)) for k in range(3)]
    for slug, a, b, E in mk:
        d = by_slug.get(slug)
        if d is None or not (np.isfinite(V[a]) and np.isfinite(V[b])):
            continue
        win = "Up" if V[b] >= V[a] else "Down"
        row = {"slug": slug, "duree": slug.split("-")[2], "ecart_pb": 1e4 * (V[b] - V[a]) / V[a]}
        buys = d[(d["side"] == "BUY") & (d["outcome"] == win)]
        for tag, sel in (("apres", buys[buys["t"] >= E]), ("avant", buys[(buys["t"] >= E - 2) & (buys["t"] < E)])):
            mg = 1.0 - pa.taker_cost(sel["price"].to_numpy())
            pos = mg > 0
            row[f"n_achats_{tag}"] = int(len(sel))
            row[f"n_marge_pos_{tag}"] = int(pos.sum())
            row[f"parts_marge_pos_{tag}"] = float(sel["size"].to_numpy()[pos].sum())
            row[f"profit_{tag}"] = float((sel["size"].to_numpy() * mg)[pos].sum())
            row[f"prix_min_{tag}"] = float(sel["price"].min()) if len(sel) else math.nan
            row[f"delai_median_s_{tag}"] = float((sel["t"] - E).median()) if len(sel) else math.nan
        rows.append(row)
    return rows


def history_day(day: str, lv: dict[int, float]) -> dict:
    t0 = time.time()
    tr = load_day_trades(day)
    by_slug = {s: g for s, g in tr.groupby("slug", sort=False)}
    d0 = int(pd.Timestamp(day, tz="UTC").timestamp())
    res = {"episodes": [], "groups": [], "incons": [], "hist": {}, "snipe": [], "day": day, "n_trades": len(tr)}
    for S in range(d0, d0 + 86400, 900):
        if f"btc-updown-15m-{S}" not in by_slug:
            continue
        V = group_values(lv, S)
        out = history_group(S, by_slug, V)
        res["episodes"] += out["episodes"]
        res["groups"] += out["groups"]
        res["incons"] += out["incons"]
        for k, h in out["hist"].items():
            res["hist"][k] = res["hist"].get(k, 0) + h
        res["snipe"] += sniping_history(S, by_slug, V)
    res["secondes"] = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# 3. Carnet réel
# ---------------------------------------------------------------------------
def load_rtds(rtds_dir: Path = RTDS_DIR) -> tuple[dict[int, float], dict[int, int]]:
    """Points Chainlink btc/usd du flux RTDS : ``{ts_s: valeur}`` et ``{ts_s: rx_ns}`` (réception)."""
    vals: dict[int, float] = {}
    rxs: dict[int, int] = {}
    for f in sorted(rtds_dir.glob("rtds_*.jsonl.gz")):
        try:
            with gzip.open(f, "rt") as fh:
                for line in fh:
                    if not line.endswith("\n"):
                        continue
                    try:
                        d = json.loads(line)
                        if "msg" not in d:
                            continue
                        m = json.loads(d["msg"])
                    except ValueError:
                        continue
                    p = m.get("payload") or {}
                    if p.get("symbol") != "btc/usd":
                        continue
                    if m.get("topic") == "crypto_prices_chainlink" and "value" in p:
                        s = int(p["timestamp"]) // 1000
                        vals.setdefault(s, float(p["value"]))
                        rxs.setdefault(s, int(d["rx"]))
                    elif m.get("type") == "subscribe":
                        for x in p.get("data") or []:
                            s = int(x["timestamp"]) // 1000
                            vals.setdefault(s, float(x["value"]))
                            rxs.setdefault(s, int(d["rx"]))
        except (EOFError, OSError, zlib.error):
            continue
    return vals, rxs


def rtds_level(vals: dict[int, float], rxs: dict[int, int], T: int, offset: int = TWAP_OFFSET_S) -> tuple[float, float, int]:
    """(V(T) via RTDS, instant de réception du dernier point nécessaire (s), nb de points)."""
    pts = [vals[s] for s in range(T - 59 - offset, T - offset + 1) if s in vals]
    last = T - offset
    rx = rxs.get(last)
    return (float(np.mean(pts)) if len(pts) == 60 else math.nan, (rx / 1e9 if rx else math.nan), len(pts))


class _Empty:
    events: list = []
    meta: dict = {}


def live_level_map(live_dir: Path) -> tuple[dict[int, float], dict[str, bool]]:
    lv: dict[int, float] = {}
    res: dict[str, bool] = {}
    for p in sorted(live_dir.glob("btc-updown-*.meta.json")):
        try:
            meta = json.loads(p.read_text())
        except ValueError:
            continue
        _, d, s = pa.parse_slug(meta["slug"])
        em = meta.get("event_metadata") or {}
        k, f = pm._f(em.get("priceToBeat")), pm._f(em.get("finalPrice"))
        if np.isfinite(k):
            lv.setdefault(s, k)
        if np.isfinite(f):
            lv.setdefault(s + (300 if d == "5m" else 900), f)
        if meta.get("resolved_up") is not None:
            res[meta["slug"]] = bool(meta["resolved_up"])
    return lv, res


def _fill(px_row: np.ndarray, sz_row: np.ndarray, qty: float, limit: float) -> tuple[float, float]:
    """(parts remplies, coût frais compris) pour ``qty`` parts à prix <= ``limit`` sur un carnet."""
    got, cost = 0.0, 0.0
    for p, s in zip(px_row, sz_row):
        if not np.isfinite(p) or p > limit + 1e-9 or s <= 0 or got >= qty - 1e-12:
            continue
        q = min(s, qty - got)
        got += q
        cost += q * pa.taker_cost(float(p))
    return got, cost


def live_group(S: int, live_dir: str, lv: dict[int, float], rtds_v: dict[int, float], rtds_known: dict[int, float],
               resolved: dict[str, bool]) -> dict:
    t_start = time.time()
    live_dir = Path(live_dir)
    slugs = [f"btc-updown-15m-{S}", f"btc-updown-5m-{S}", f"btc-updown-5m-{S + 300}", f"btc-updown-5m-{S + 600}"]
    markets, present = [], []
    for s in slugs:
        if (live_dir / f"{s}.meta.json").exists() and bk.market_files(s, live_dir):
            markets.append(bk.load_market(s, live_dir))
            present.append(True)
        else:
            markets.append(_Empty())
            present.append(False)
    st = pa.live_group_states(markets, depth=3)
    rx = st["rx"]
    N = len(rx)
    info = {"S": S, "slug15": slugs[0], "marches_presents": int(sum(present)), "etats": N,
            "evenements": int(sum(len(m.events) for m in markets))}
    V = {}
    src = {}
    for k in range(4):
        vr = rtds_v.get(S + 300 * k, math.nan)
        vm = lv.get(S + 300 * k, math.nan)
        V[k] = vr if np.isfinite(vr) else vm
        src[k] = "rtds" if np.isfinite(vr) else ("gamma" if np.isfinite(vm) else "absent")
        info[f"V{k}_source"] = src[k]
        info[f"V{k}_ecart_rtds_gamma"] = (vr - vm) if (np.isfinite(vr) and np.isfinite(vm)) else math.nan
    out = {"info": info, "episodes": [], "snipe": [], "crossed": [], "hist": {}, "timeline": None}
    if N == 0:
        info["secondes"] = time.time() - t_start
        return out
    t_rel = rx / 1e9 - S
    px = st["px"].astype(np.float64)
    sz = st["sz"].astype(np.float64)
    fresh = st["fresh"]
    best = px[:, :, 0]
    # marché « vivant » : l'enregistrement n'est pas terminé (sinon carnet figé, ex. panne 06:05–10:00)
    last_rx = np.array([max((e[0] for e in mk.events), default=0) for mk in markets], dtype=np.int64)
    alive = rx[:, None] <= last_rx[None, :]
    fresh = fresh & alive
    avail = np.isfinite(best) & np.repeat(fresh, 2, axis=1)
    # carnet croisé (bid >= ask) : état intermédiaire entre deux messages, impossible dans le moteur
    # d'appariement -> marché ignoré le temps de cet état (compté à part, § Up + Down)
    avail_x = avail & ~np.repeat(st["crossed"], 2, axis=1)
    C = np.where(avail_x, pa.taker_cost(np.nan_to_num(best, nan=0.5)), np.inf)
    # Up + Down du même marché : ask Up + ask Down = 1 + écart >= 1 sauf carnet croisé
    for m in range(4):
        if not present[m]:
            continue
        both = avail[:, 2 * m] & avail[:, 2 * m + 1]
        s = best[:, 2 * m] + best[:, 2 * m + 1]
        dt = np.diff(np.r_[rx, rx[-1]]) / 1e6
        out["crossed"].append({"slug": slugs[m], "etats_deux_cotes": int(both.sum()),
                               "etats_somme_lt_1": int((both & (s < 1 - 1e-9)).sum()),
                               "etats_croises": int((st["crossed"][:, m] & both).sum()),
                               "somme_min": float(np.nanmin(np.where(both, s, np.nan))) if both.any() else math.nan,
                               "somme_mediane": float(np.nanmedian(np.where(both, s, np.nan))) if both.any() else math.nan,
                               "ms_somme_lt_1": float(dt[both & (s < 1 - 1e-9)].sum())})
    seg_id = np.full(N, -1)
    setups = []
    for k, (a, b) in enumerate(LIVE_SEGMENTS):
        seg_id[(t_rel >= a) & (t_rel < b)] = k
        active, known = pa.group_state(a, L, V)
        setups.append((active, known) + _segment_setup(L, active, known))
    margin = np.full(N, np.nan)
    vid = np.full(N, -1)
    dt_ms = np.diff(np.r_[rx, rx[-1]]) / 1e6
    for k, (active, known, A, Vx, cols) in enumerate(setups):
        rows = seg_id == k
        if not rows.any():
            continue
        bst, idx = pa.min_cover_cost(C[rows][:, cols], Vx)
        mg = 1.0 - bst
        mg[~np.isfinite(mg)] = np.nan
        margin[rows] = mg
        vid[rows] = idx
        bx, _ = pa.min_cover_cost(C[rows][:, cols], _cross_vertices(Vx))
        mx = 1.0 - bx                          # meilleure couverture inter-marchés
        fin = np.isfinite(mx)
        # histogramme pondéré par la durée (ms) passée dans chaque état
        h = np.histogram(np.clip(100 * mx[fin], MARGIN_BINS[0], MARGIN_BINS[-1] - 1e-9), bins=MARGIN_BINS, weights=dt_ms[rows][fin])[0]
        out["hist"][PHASES[k]] = h
    info["marge_max"] = float(np.nanmax(margin)) if np.isfinite(margin).any() else math.nan
    info["ms_couverts"] = float(dt_ms[np.isfinite(margin)].sum())
    flag = np.nan_to_num(margin, nan=-1.0) > 1e-9
    eps = pa.episodes(rx, flag, max_gap=np.inf, keys=seg_id)
    names = pa.token_names(L)
    for i0, i1 in eps:
        k = int(seg_id[i0])
        active, known, A, Vx, cols = setups[k]
        v = Vx[vid[i0]]
        legs_i = [cols[j] for j in np.flatnonzero(v > 1e-12)]
        censored = i1 + 1 >= N
        end_rx = rx[i1] if censored else rx[i1 + 1]
        row = {"slug15": slugs[0], "S": S, "phase": PHASES[k], "t0_rel_s": float(t_rel[i0]), "rx0": int(rx[i0]),
               "duree_vie_ms": float((end_rx - rx[i0]) / 1e6), "censure": bool(censored), "n_etats": int(i1 - i0 + 1),
               "marge_debut": float(margin[i0]), "marge_max": float(np.nanmax(margin[i0:i1 + 1])),
               "portefeuille": " + ".join(names[j] for j in legs_i),
               "prix_jambes": " / ".join(f"{best[i0, j]:.3f}" for j in legs_i),
               "tailles_meilleur_niveau": " / ".join(f"{sz[i0, j, 0]:.1f}" for j in legs_i),
               "taille_min_meilleur_niveau": float(np.min(sz[i0, legs_i, 0])),
               "V_sources": ",".join(src[n] for n in sorted(known) if n in src)}
        prices = [px[i0, c] if avail_x[i0, c] else np.full(px.shape[2], np.nan) for c in cols]
        sizes = [sz[i0, c] for c in cols]
        for n in SIZES + (None,):
            r = pa.solve_arbitrage(A, prices, sizes, max_sets=(BIG if n is None else n))
            tag = "max" if n is None else str(n)
            row[f"profit_{tag}"] = r.profit
            row[f"lots_{tag}"] = r.payout
        # plan exécuté avec retard : lot de LIVE_PLAN_SETS parts aux prix limites détectés
        plan = pa.solve_arbitrage(A, prices, sizes, max_sets=LIVE_PLAN_SETS)
        for lat in LATENCIES_MS:
            if plan.payout <= 0:
                row[f"profit_lat_{lat}"] = 0.0
                row[f"fraction_lat_{lat}"] = 0.0
                row[f"orphelines_lat_{lat}"] = 0.0
                continue
            t_arr = rx[i0] + lat * 1_000_000
            j = int(np.searchsorted(rx, t_arr, side="right") - 1)
            t_arr_rel = t_arr / 1e9 - S
            fills, costs, qty = [], [], []
            for jj, c in enumerate(cols):
                q = plan.shares[jj]
                if q <= 1e-9:
                    continue
                leg = L[c // 2]
                if (t_arr_rel >= 300 * leg.end or t_arr > last_rx[c // 2] or not fresh[j, c // 2]
                        or st["crossed"][j, c // 2]):
                    got, cost = 0.0, 0.0
                else:
                    got, cost = _fill(px[j, c], sz[j, c], q, plan.max_price[jj])
                fills.append(got)
                costs.append(cost)
                qty.append(q)
            f = min(g / q for g, q in zip(fills, qty)) if qty else 0.0
            # coût du lot couvert : f × quantité de chaque jambe au prix moyen de remplissage
            cost_f = sum((c / g) * f * q if g > 0 else 0.0 for g, c, q in zip(fills, costs, qty))
            row[f"profit_lat_{lat}"] = f * plan.payout - cost_f
            row[f"fraction_lat_{lat}"] = f
            row[f"orphelines_lat_{lat}"] = float(sum(g - f * q for g, q in zip(fills, qty)))
        out["episodes"].append(row)
    # issue déjà connue : ask du gagnant autour de la clôture (le TWAP est figé à E − 3 s)
    mk = [(0, 0, 3, S + 900)] + [(k + 1, k, k + 1, S + 300 * (k + 1)) for k in range(3)]
    fresh_rows = np.repeat(fresh, 2, axis=1)
    for m, a, b, E in mk:
        if not present[m]:
            continue
        win_up = resolved.get(slugs[m])
        if win_up is None and np.isfinite(V[a]) and np.isfinite(V[b]):
            win_up = V[b] >= V[a]
        if win_up is None:
            continue
        w = 2 * m + (0 if win_up else 1)
        t_known = rtds_known.get(E, math.nan)
        row = {"slug": slugs[m], "gagnant": "Up" if win_up else "Down",
               "ecart_pb": 1e4 * (V[b] - V[a]) / V[a] if np.isfinite(V[a]) and np.isfinite(V[b]) else math.nan,
               "connu_via_rtds_avant_E_s": E - t_known if np.isfinite(t_known) else math.nan}
        for lab, tq in (("m10", E - 10.0), ("m3", E - 3.0), ("rtds", t_known if np.isfinite(t_known) else E - 1.7),
                        ("E", float(E)), ("p2", E + 2.0)):
            j = int(np.searchsorted(rx, tq * 1e9, side="right") - 1)
            if j < 0 or tq * 1e9 > last_rx[m] or not fresh_rows[j, w] or not np.isfinite(best[j, w]):
                row[f"ask_{lab}"], row[f"taille_{lab}"], row[f"marge_{lab}"] = math.nan, 0.0, math.nan
                continue
            row[f"ask_{lab}"] = float(best[j, w])
            row[f"taille_{lab}"] = float(sz[j, w, 0])
            row[f"marge_{lab}"] = float(1.0 - pa.taker_cost(best[j, w]))
        t0 = t_known if np.isfinite(t_known) else E - 1.7
        rows = (rx / 1e9 >= t0) & fresh_rows[:, w] & np.isfinite(best[:, w])
        if rows.any():
            mg = 1.0 - pa.taker_cost(best[rows, w])
            prof = np.where(mg > 0, mg, 0.0) * sz[rows, w, 0]
            row.update(marge_max_apres_connu=float(mg.max()), ms_marge_pos_apres_connu=float(dt_ms[rows][mg > 0].sum()),
                       profit_max_etat_apres_connu=float(prof.max()))
        else:
            row.update(marge_max_apres_connu=math.nan, ms_marge_pos_apres_connu=0.0, profit_max_etat_apres_connu=0.0)
        out["snipe"].append(row)
    # frise de l'épisode le plus rentable (pour la figure d'exemple)
    if out["episodes"]:
        e = max(out["episodes"], key=lambda r: r["profit_max"])
        i0 = int(np.searchsorted(rx, e["rx0"]))
        k = int(seg_id[i0])
        cols = setups[k][4]
        v = setups[k][3][vid[i0]]
        legs_i = [cols[j] for j in np.flatnonzero(v > 1e-12)]
        half = 150_000_000                     # ± 150 ms autour de la détection
        lo = max(0, int(np.searchsorted(rx, e["rx0"] - half)) - 1)
        hi = int(np.searchsorted(rx, e["rx0"] + half, side="right"))
        Cw = np.where(np.isfinite(C[lo:hi][:, legs_i]), C[lo:hi][:, legs_i], np.nan)
        out["timeline"] = {"t_ms": np.maximum((rx[lo:hi] - e["rx0"]) / 1e6, -half / 1e6), "legs": [names[j] for j in legs_i],
                           "asks": best[lo:hi][:, legs_i], "cost": Cw.sum(axis=1), "episode": e}
    info["secondes"] = time.time() - t_start
    return out


# ---------------------------------------------------------------------------
# Résumés
# ---------------------------------------------------------------------------
def history_summary(ep: pd.DataFrame, groups: pd.DataFrame, inc: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for v, var in VARIANTS.items():
        g = groups[groups["variante"] == v]
        e = ep[ep["variante"] == v] if len(ep) else pd.DataFrame(columns=["marge_debut", "duree_s", "phase"])
        i = inc[inc["variante"] == v]
        r = {"variante": v, "prix_estime": var["label"], "fenetres": int(len(g)),
             "fenetres_gt_0": int((g["marge_max"] > 1e-9).sum()), "fenetres_gt_1c": int((g["marge_max"] > 0.01).sum()),
             "fenetres_gt_2c": int((g["marge_max"] > 0.02).sum()), "episodes": int(len(e)),
             "episodes_apres_cloture_2": int((e["phase"] == PHASES[3]).sum()) if len(e) else 0,
             "marge_mediane": float(e["marge_debut"].median()) if len(e) else math.nan,
             "duree_mediane_s": float(e["duree_s"].median()) if len(e) else math.nan,
             "duree_p90_s": float(e["duree_s"].quantile(0.9)) if len(e) else math.nan,
             "duree_max_s": float(e["duree_s"].max()) if len(e) else math.nan,
             "part_meme_bloc": float((e["ecart_ages_s"] == 0).mean()) if len(e) else math.nan}
        for n in SIZES:
            r[f"profit_{n}"] = float(e[f"profit_{n}"].sum()) if len(e) else 0.0
            r[f"profit_{n}_sans_limite"] = float(e[f"profit_{n}_sans_limite"].sum()) if len(e) else 0.0
        both = i["secondes_deux_cotes"].sum()
        r["part_up_down_lt_1"] = float(i["secondes_somme_lt_1"].sum() / both) if both else math.nan
        r["part_up_down_cout_lt_1"] = float(i["secondes_cout_lt_1"].sum() / both) if both else math.nan
        rows.append(r)
    return pd.DataFrame(rows)


def snipe_bins(sn: pd.DataFrame) -> pd.DataFrame:
    if not len(sn):
        return pd.DataFrame()
    d = sn.copy()
    d["abs_pb"] = d["ecart_pb"].abs()
    bins = [0, 0.1, 0.5, 1, 5, np.inf]
    labels = ["< 0,1 pb", "0,1 – 0,5 pb", "0,5 – 1 pb", "1 – 5 pb", "≥ 5 pb"]
    d["ecart"] = pd.cut(d["abs_pb"], bins=bins, labels=labels, right=False)
    rows = []
    for lab in labels:
        g = d[d["ecart"] == lab]
        rows.append({"ecart_final": lab, "marches": int(len(g)),
                     "marches_gagnant_achete_apres": int((g["n_marge_pos_apres"] > 0).sum()),
                     "profit_apres": float(g["profit_apres"].sum()), "parts_apres": float(g["parts_marge_pos_apres"].sum()),
                     "prix_min_median_apres": float(g.loc[g["n_marge_pos_apres"] > 0, "prix_min_apres"].median())
                     if (g["n_marge_pos_apres"] > 0).any() else math.nan,
                     "marches_gagnant_achete_avant": int((g["n_marge_pos_avant"] > 0).sum()),
                     "profit_avant": float(g["profit_avant"].sum())})
    tot = {"ecart_final": "Tous", "marches": int(len(d)),
           "marches_gagnant_achete_apres": int((d["n_marge_pos_apres"] > 0).sum()), "profit_apres": float(d["profit_apres"].sum()),
           "parts_apres": float(d["parts_marge_pos_apres"].sum()),
           "prix_min_median_apres": float(d.loc[d["n_marge_pos_apres"] > 0, "prix_min_apres"].median()),
           "marches_gagnant_achete_avant": int((d["n_marge_pos_avant"] > 0).sum()), "profit_avant": float(d["profit_avant"].sum())}
    return pd.DataFrame(rows + [tot])


# ---------------------------------------------------------------------------
# 4. Figures
# ---------------------------------------------------------------------------
def _tx(text: str) -> str:
    """Échappe les « $ » (sinon interprétés comme des formules par matplotlib)."""
    return str(text).replace("$", r"\$")


def _fr_log_axis(axis, nd_small: int = 2) -> None:
    """Graduations d'un axe logarithmique au format français (0,1 ; 1 ; 10 ; 1 000)."""
    from matplotlib.ticker import FuncFormatter

    axis.set_major_formatter(FuncFormatter(lambda v, _: fr(v, 0) if v >= 1 else fr(v, max(1, int(round(-math.log10(v)))))))


def _fig(W: float, title: str, sub: str, body_h: float):
    from tradebot.report import BG, _draw_header, _header, _pyplot

    plt = _pyplot()
    t, s, hh = _header(W, _tx(title), _tx(sub))
    H = hh + body_h
    fig, ax = plt.subplots(figsize=(W, H), facecolor=BG)
    fig.subplots_adjust(left=0.09, right=0.97, top=1 - (hh + 0.15) / H, bottom=0.8 / H)
    _draw_header(fig, t, s)
    return fig, ax


def plot_history_phases(ep: pd.DataFrame, n_groups: int, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    A = ep[ep["variante"] == "A"]
    B = ep[ep["variante"] == "B"]
    Cc = ep[ep["variante"] == "C"]
    # ordre validé (scripts/validate_palette.js du skill dataviz, mode clair) : bleu, aqua, jaune, violet, orange
    series = [("A", "A : > 0", A, 0.0, C_BLUE), ("A", "A : > 1 c", A, 0.01, C_AQUA), ("A", "A : > 2 c", A, 0.02, C_YELLOW),
              ("B", "B (+ 1 c) : > 0", B, 0.0, C_VIOLET), ("C", "C (max du bloc, < 2 s) : > 0", Cc, 0.0, C_ORANGE)]
    counts = {lab: [int((d[(d["phase"] == ph)]["marge_debut"] > th + 1e-12).sum()) for ph in PHASES] for _, lab, d, th, _ in series}
    tot_a = int((A["marge_debut"] > 0).sum())
    last = counts["A : > 0"][3]
    share_last = last / tot_a if tot_a else math.nan
    tot_b = int((B["marge_debut"] > 0).sum())
    tot_c = int((Cc["marge_debut"] > 0).sum())
    title = (f"Trades historiques : {fr(tot_a, 0)} « arbitrages » apparents en 21 jours, {fr(100 * share_last, 0)} % après la clôture "
             f"de la 2e 5m et aucun avant ; {fr(tot_b, 0)} en payant 1 c de plus, {fr(tot_c, 0)} avec l'estimation robuste")
    sub = (f"{n_groups} fenêtres 15m BTC (04/09 → 24/09/2026). Prix estimé : (A) dernier achat preneur du jeton il y a moins de 5 s, "
           "(B) le même + 1 c, (C) prix maximal du dernier bloc de moins de 2 s (datation au bloc ≈ 2 s). Marge = 1 $ − coût minimal "
           "(frais compris) d'un portefeuille qui paie au moins 1 $ quelle que soit l'issue. Nombre d'épisodes (secondes consécutives) "
           "par phase et par seuil de marge au début de l'épisode.")
    fig, ax = _fig(11.0, title, sub, 4.6)
    _style_axes(ax)
    x = np.arange(len(PHASES))
    w = 0.16
    for j, (_, lab, _, _, col) in enumerate(series):
        vals = counts[lab]
        bars = ax.bar(x + (j - 2) * w, vals, width=w * 0.9, color=col, label=lab)
        for bx, v in zip(bars, vals):
            if v:
                ax.text(bx.get_x() + bx.get_width() / 2, v, f"{fr(v, 0)}", ha="center", va="bottom", fontsize=7.5, color=TEXT)
    for k in range(len(PHASES)):
        if not any(counts[lab][k] for lab in counts):
            ax.text(k, 0, "aucun épisode", ha="center", va="bottom", fontsize=8.5, color=TEXT_2)
    ax.set_xticks(x)
    ax.set_xticklabels(["avant S", "S → clôture #1", "clôture #1 → #2", "clôture #2 → fin"], color=TEXT_2)
    ax.set_ylabel("épisodes (21 jours)", color=TEXT_2, fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=TEXT, ncol=2)
    ax.set_ylim(0, max(1, max(max(v) for v in counts.values())) * 1.18)
    _save(fig, path)
    return title


def plot_live_lifetime(ep: pd.DataFrame, n_groups: int, hours: float, path: Path) -> str:
    from tradebot.report import BG, GRID, TEXT, TEXT_2, _save, _style_axes

    d = ep.copy()
    n = len(d)
    med = float(d["duree_vie_ms"].median()) if n else math.nan
    mx = float(d["duree_vie_ms"].max()) if n else math.nan
    size_med = float(d["taille_min_meilleur_niveau"].median()) if n else math.nan
    title = (f"Carnet réel : {n} violations en {fr(hours, 1)} h, durée de vie médiane {fr(med, 2 if med < 10 else 0)} ms "
             f"(max {fr(mx, 2 if mx < 10 else 0)} ms) et {fr(size_med, 0)} parts sur la jambe la plus mince : "
             "trop bref et trop petit pour être saisi") if n else \
        f"Carnet réel : aucune violation en {fr(hours, 1)} h de carnets 5m + 15m enregistrés ensemble"
    sub = (f"{n_groups} fenêtres 15m BTC dont les quatre carnets (15m + trois 5m) ont été enregistrés en même temps "
           "(WebSocket, horloge de réception). Chaque point : une violation (coût minimal d'un paiement garanti de 1 $ < 1 $ "
           "au meilleur ask, frais compris) ; axe horizontal logarithmique. Couleur : phase de la fenêtre 15m.")
    fig, ax = _fig(11.0, title, sub, 4.4)
    _style_axes(ax, xgrid=True)
    cols = {PHASES[1]: C_BLUE, PHASES[2]: C_AQUA, PHASES[3]: C_ORANGE, PHASES[0]: C_VIOLET}
    for ph, g in (d.groupby("phase") if n else []):
        ax.scatter(np.maximum(g["duree_vie_ms"], 0.01), 100 * g["marge_debut"], s=40, color=cols.get(ph, C_YELLOW),
                   label=f"{ph} ({len(g)})", alpha=0.85, edgecolor=BG, linewidth=1.0)
        for r in g.itertuples():
            ax.annotate(f"{fr(r.taille_min_meilleur_niveau, 0)} part(s)", (max(r.duree_vie_ms, 0.01), 100 * r.marge_debut),
                        xytext=(8, -14 if r.Index % 2 else 8), textcoords="offset points", fontsize=8, color=TEXT)
    ax.set_xscale("log")
    ax.set_xlim(0.05, 5000)
    _fr_log_axis(ax.xaxis)
    ax.set_xlabel("durée de vie de la violation (ms, horloge de réception)", color=TEXT_2, fontsize=9)
    ax.set_ylabel(_tx("marge au début (cents par lot de 1 $)"), color=TEXT_2, fontsize=9)
    ytop = max(1.0, float(100 * d["marge_debut"].max()) * 1.3) if n else 1.0
    ax.set_ylim(0, ytop)
    for xv, lab in ((1, "1 ms"), (100, "100 ms : retard réseau typique"), (1000, "1 s")):
        ax.axvline(xv, color=TEXT_2, lw=0.8)
        ax.text(xv * 1.08, ytop * 0.97, lab, color=TEXT_2, fontsize=8, va="top")
    if n:
        ax.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=TEXT, bbox_to_anchor=(0.0, 0.9))
    _save(fig, path)
    return title


def plot_latency(lat: pd.DataFrame, n_eps: int, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    x = lat["latence_ms"].to_numpy()
    y = lat["profit_total"].to_numpy()
    y0 = y[0] if len(y) else math.nan
    title = (f"Avec 100 ms de retard ou plus, le profit garanti tombe de {usd(y0, 3)} à {usd(float(y[1]) if len(y) > 1 else math.nan, 3)} : "
             "les violations se referment avant qu'un ordre puisse arriver") if y0 > 0 else \
        "Aucun profit garanti exécutable sur le carnet réel"
    sub = (f"{n_eps} violations du carnet réel ; plan = lot de {LIVE_PLAN_SETS} parts au meilleur prix disponible à la détection "
           "(programme linéaire, 3 niveaux). À l'arrivée de l'ordre (détection + retard), chaque jambe n'est remplie qu'aux prix "
           "≤ prix limite ; le lot couvert est limité par la jambe la plus pauvre. Profit garanti total (frais compris), "
           "parts orphelines non comptées.")
    fig, ax = _fig(11.0, title, sub, 4.2)
    _style_axes(ax)
    ax.plot(x, y, color=C_BLUE, lw=2.2, marker="o")
    for xi, yi, fi in zip(x, y, lat["survie"].to_numpy()):
        ax.annotate(_tx(f"{usd(yi, 3)}\nlots complets : {fr(100 * fi, 0)} %"), (xi, yi), xytext=(0, 10), textcoords="offset points",
                    ha="center", fontsize=8, color=TEXT)
    ax.set_xscale("symlog", linthresh=100)
    ax.set_xticks(list(LATENCIES_MS))
    ax.set_xticklabels([f"{v} ms" for v in LATENCIES_MS], color=TEXT_2)
    ax.set_ylabel(_tx("profit garanti total ($)"), color=TEXT_2, fontsize=9)
    ax.set_ylim(0, max(1e-6, float(np.nanmax(y)) if len(y) else 1) * 1.35)
    ax.set_xlim(-40, 5000)
    _save(fig, path)
    return title


def plot_margin_distribution(hist_hist: dict, hist_live: dict, path: Path) -> str:
    from tradebot.report import GRID, TEXT, TEXT_2, _save, _style_axes

    lo_edges = MARGIN_BINS[:-1]
    ph = PHASES[3]
    hh = np.asarray(hist_hist.get(("A", ph), np.zeros(len(lo_edges))), dtype=float)
    hc = np.asarray(hist_hist.get(("C", ph), np.zeros(len(lo_edges))), dtype=float)
    hl = np.asarray(hist_live.get(ph, np.zeros(len(lo_edges))), dtype=float)

    def surv(h):                      # part du temps où la marge est >= bord bas de chaque classe
        tot = h.sum()
        return (np.cumsum(h[::-1])[::-1] / tot) if tot else np.full(len(h), np.nan)

    sh, sc, sl = surv(hh), surv(hc), surv(hl)
    i0 = int(np.searchsorted(lo_edges, 0.0))
    ms_pos = float(hl[i0:].sum())
    hours = float(hl.sum()) / 3.6e6
    title = (f"Après la clôture de la 2e 5m, la couverture 15m + 5m#3 du carnet réel n'a été gagnante que {fr(ms_pos, 1)} ms "
             f"sur {fr(hours, 1)} h ; l'estimation par les trades le suggère {fr(100 * sh[i0], 2)} % du temps (variante A)")
    sub = ("Part du temps où la marge de la meilleure couverture inter-marchés (15m Up + 5m#3 Down si V2 ≥ V0, sinon 15m Down "
           "+ 5m#3 Up) dépasse x cents, phase « clôture #2 → fin ». Marge = 1 $ − coût frais compris ; marge > 0 = arbitrage. "
           "Carnet réel au meilleur ask pondéré par la durée de chaque état ; trades : chaque seconde. Échelle verticale logarithmique.")
    fig, ax = _fig(11.0, title, sub, 4.4)
    _style_axes(ax)
    for y, lab, col in ((sh, "trades, A : dernier achat < 5 s", C_ORANGE), (sc, "trades, C : max du bloc < 2 s", C_YELLOW),
                        (sl, "carnet réel (26/09)", C_BLUE)):
        yy = np.where(y > 0, 100 * y, np.nan)
        ax.step(lo_edges, yy, where="post", color=col, lw=2.0, label=lab)
    ax.set_yscale("log")
    ax.axvline(0, color=TEXT_2, lw=1.0)
    ax.text(0.4, 60, "arbitrage →", color=TEXT_2, fontsize=8.5, va="top")
    ax.set_xlabel(_tx("marge x (cents par lot de 1 $)"), color=TEXT_2, fontsize=9)
    ax.set_ylabel("part du temps avec marge ≥ x (%)", color=TEXT_2, fontsize=9)
    ax.legend(loc="lower left", frameon=False, fontsize=8.5, labelcolor=TEXT)
    ax.set_xlim(-30, 10)
    ax.set_ylim(1e-5, 150)
    from matplotlib.ticker import FuncFormatter, LogLocator

    ax.yaxis.set_major_locator(LogLocator(base=10))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fr(v, 0) if v >= 1 else fr(v, max(1, int(round(-math.log10(v)))))))
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, which="major")
    _save(fig, path)
    return title


def plot_example(tl: dict, path: Path) -> str:
    from tradebot.report import TEXT, TEXT_2, _save, _style_axes

    e = tl["episode"]
    t = tl["t_ms"]
    title = (f"Exemple réel : {e['portefeuille']} coûte {cents(1 - e['marge_debut'], 2)} pour un paiement garanti de 1 $ "
             f"pendant {fr(e['duree_vie_ms'], 2)} ms seulement ({pd.Timestamp(e['rx0'], unit='ns').strftime('%d/%m %H:%M:%S')} UTC)")
    sub = (f"Meilleurs asks des jambes et coût total du lot couvrant (prix + frais preneur), ± 150 ms autour de la détection (t = 0), "
           "horloge de réception. "
           f"Phase : {e['phase']}. Profit au meilleur niveau pour {LIVE_PLAN_SETS} parts : {usd(e['profit_100'])} ; "
           f"tout le carnet (3 niveaux) : {usd(e['profit_max'])}.")
    fig, ax = _fig(11.0, title, sub, 4.4)
    _style_axes(ax)
    cols = [C_BLUE, C_AQUA, C_VIOLET, C_YELLOW]
    for j, name in enumerate(tl["legs"]):
        ax.step(t, 100 * tl["asks"][:, j], where="post", color=cols[j % 4], lw=1.6, label=f"ask {name}")
    ax.step(t, 100 * tl["cost"], where="post", color=C_ORANGE, lw=2.2, label="coût du lot (frais compris)")
    ax.axhline(100, color=TEXT_2, lw=1.0, ls=(0, (3, 3)))
    ax.text(t.min() if len(t) else 0, 100.5, _tx("paiement garanti 1 $"), color=TEXT_2, fontsize=8.5, va="bottom")
    ax.axvline(0, color=TEXT_2, lw=0.9)
    ax.set_xlabel("ms depuis la détection", color=TEXT_2, fontsize=9)
    ax.set_ylabel("cents", color=TEXT_2, fontsize=9)
    ax.legend(loc="center left", frameon=False, fontsize=8.5, labelcolor=TEXT)
    top = float(np.nanmax(100 * tl["cost"])) if np.isfinite(tl["cost"]).any() else 100.0
    ax.set_ylim(0, min(160.0, max(110.0, top * 1.03)))
    _save(fig, path)
    return title


# ---------------------------------------------------------------------------
# 5. Programme principal
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(REPORTS_DIR / "polymarket" / "arbitrage"))
    ap.add_argument("--live-dir", default=str(bk.LIVE_DIR))
    ap.add_argument("--days", nargs="*", default=DAYS)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-live-groups", type=int, default=0, help="0 = tous")
    ap.add_argument("--no-network", action="store_true", help="ne pas lire les métadonnées 4h sur gamma")
    ap.add_argument("--skip-history", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timer = Timer()
    t_all = time.time()
    ctx: dict = {"generated": pd.Timestamp.now(tz="UTC").strftime("%d/%m/%Y %H:%M UTC"), "titles": {}}

    # 1. chaîne
    with timer("1. chaîne K/F (event_meta + 4h gamma) et contrôles logiques"):
        meta = load_meta()
        m4 = pd.DataFrame(columns=["slug", "price_to_beat", "final_price"])
        if not args.no_network:
            try:
                m4 = fetch_4h_meta("btc", "2026-08-14", "2026-09-25")
            except Exception as exc:  # noqa: BLE001
                log.warning("4h gamma indisponible : %r", exc)
        ctx["n_4h"] = int(np.isfinite(m4["final_price"].astype(float)).sum()) if len(m4) else 0
        chain = []
        for asset in ("btc", "eth", "sol"):
            sub = meta[meta["slug"].str.startswith(f"{asset}-")]
            if asset == "btc" and len(m4):
                sub = pd.concat([sub, m4], ignore_index=True)
            if len(sub):
                c = pa.chain_checks(sub)
                c.insert(0, "actif", asset.upper())
                chain.append(c)
        chain = pd.concat(chain, ignore_index=True)
        chain = chain[chain["n"] > 0]
        chain.to_csv(out / "chaine_verification.csv", index=False, float_format="%.6g")
        lv = level_map(meta, "btc")
        starts = [S for S in range(int(pd.Timestamp("2026-09-04", tz="UTC").timestamp()),
                                   int(pd.Timestamp("2026-09-25", tz="UTC").timestamp()), 900)]
        outcomes = official_outcomes("2026-09-04", "2026-09-25")
        logic, consec = logic_checks(lv, outcomes, starts)
        logic.to_csv(out / "controles_logiques.csv", index=False)
        consec.to_csv(out / "5m_consecutives.csv", index=False, float_format="%.6g")
        ctx.update(chain=chain, logic=logic, consec=consec, n_meta=len(meta))

    # 2. historique
    hist_hist: dict = {}
    ep = pd.DataFrame()
    groups = pd.DataFrame()
    incons = pd.DataFrame()
    snipe_h = pd.DataFrame()
    if not args.skip_history:
        with timer(f"2. historique trades ({len(args.days)} jours, {args.workers} processus)"):
            res = []
            if args.workers > 1:
                with ProcessPoolExecutor(max_workers=args.workers) as ex:
                    futs = [ex.submit(history_day, d, lv) for d in args.days]
                    for f in futs:
                        res.append(f.result())
            else:
                res = [history_day(d, lv) for d in args.days]
            ep = pd.DataFrame([r for x in res for r in x["episodes"]])
            groups = pd.DataFrame([r for x in res for r in x["groups"]])
            incons = pd.DataFrame([r for x in res for r in x["incons"]])
            snipe_h = pd.DataFrame([r for x in res for r in x["snipe"]])
            for x in res:
                for k, h in x["hist"].items():
                    hist_hist[k] = hist_hist.get(k, 0) + h
            ctx["n_trades"] = int(sum(x["n_trades"] for x in res))
            ctx["day_secs"] = [x["secondes"] for x in res]
        ep.to_csv(out / "historique_episodes.csv", index=False, float_format="%.6g")
        groups.to_csv(out / "historique_fenetres.csv", index=False, float_format="%.6g")
        inc = incons.groupby(["variante", "marche", "phase"], as_index=False)[
            ["secondes_deux_cotes", "secondes_somme_lt_1", "secondes_cout_lt_1"]].sum()
        inc["part_somme_lt_1"] = inc["secondes_somme_lt_1"] / inc["secondes_deux_cotes"].replace(0, np.nan)
        inc["part_cout_lt_1"] = inc["secondes_cout_lt_1"] / inc["secondes_deux_cotes"].replace(0, np.nan)
        inc.to_csv(out / "historique_up_plus_down.csv", index=False, float_format="%.6g")
        snipe_h.to_csv(out / "historique_gagnant_apres_cloture.csv", index=False, float_format="%.6g")
        hrows = [{"variante": k[0], "phase": k[1], "marge_c_bas": lo, "marge_c_haut": hi, "secondes": int(c)}
                 for k, h in hist_hist.items() for lo, hi, c in zip(MARGIN_BINS[:-1], MARGIN_BINS[1:], h) if c]
        pd.DataFrame(hrows).to_csv(out / "historique_distribution_marge.csv", index=False, float_format="%.6g")
        hsum = history_summary(ep, groups, inc)
        hsum.to_csv(out / "historique_resume.csv", index=False, float_format="%.6g")
        sbins = snipe_bins(snipe_h)
        sbins.to_csv(out / "historique_gagnant_par_ecart.csv", index=False, float_format="%.6g")
        ctx.update(ep=ep, groups=groups, incons=inc, snipe_h=snipe_h, hsum=hsum, sbins=sbins)

    # 3. carnet réel
    live_dir = Path(args.live_dir)
    with timer("3a. Chainlink RTDS (niveaux V en temps réel)"):
        rv, rrx = load_rtds()
        llv, resolved = live_level_map(live_dir)
        rt_rows = []
        rtds_v, rtds_known = {}, {}
        if rv:
            t_lo, t_hi = min(rv), max(rv)
            for T in range((t_lo // 300 + 1) * 300, t_hi + 1, 300):
                cand = {off: rtds_level(rv, rrx, T, offset=off) for off in RTDS_OFFSETS}
                if all(c[2] == 0 for c in cand.values()):
                    continue
                g = llv.get(T, math.nan)
                errs = {off: (c[0] - g) for off, c in cand.items()}
                exact = [off for off, e in errs.items() if np.isfinite(e) and abs(e) < 1e-6]
                off_m = exact[0] if exact else math.nan
                v, rxk, npts = cand[TWAP_OFFSET_S]
                if exact:
                    v, rxk, _ = cand[off_m]
                vals = [cand[o][0] for o in (2, 3)]
                rt_rows.append({"T": T, "heure": pd.Timestamp(T, unit="s").strftime("%H:%M"), "points": cand[TWAP_OFFSET_S][2],
                                "V_gamma": g, "ecart_decalage_3s": errs[3], "ecart_decalage_2s": errs[2],
                                "ecart_sans_decalage": errs[0], "decalage_exact_s": off_m,
                                "V_rtds": v if exact else math.nan,
                                "ecart_V_2s_3s": (vals[0] - vals[1]) if all(np.isfinite(vals)) else math.nan,
                                "connu_avant_T_s": T - rxk if np.isfinite(rxk) else math.nan})
                if exact:
                    rtds_v[T] = v
                    rtds_known[T] = rxk
        rt = pd.DataFrame(rt_rows)
        rt.to_csv(out / "chainlink_rtds_niveaux.csv", index=False, float_format="%.10g")
        ctx["rtds"] = rt
    groups_live = sorted({pa.parse_slug(s)[2] for s in bk.list_markets(live_dir) if s.startswith("btc-updown-15m-")})
    now = time.time()
    groups_live = [S for S in groups_live if S + 900 + 125 < now]          # fenêtres terminées (+ fin de collecte)
    if args.max_live_groups:
        groups_live = groups_live[: args.max_live_groups]
    if args.skip_live:
        groups_live = []
    with timer(f"3b. carnet réel ({len(groups_live)} fenêtres 15m, {args.workers} processus)"):
        lres = []
        if args.workers > 1:
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                futs = [ex.submit(live_group, S, str(live_dir), llv, rtds_v, rtds_known, resolved) for S in groups_live]
                for f in futs:
                    lres.append(f.result())
        else:
            lres = [live_group(S, str(live_dir), llv, rtds_v, rtds_known, resolved) for S in groups_live]
    linfo = pd.DataFrame([r["info"] for r in lres])
    lep = pd.DataFrame([e for r in lres for e in r["episodes"]])
    lsn = pd.DataFrame([e for r in lres for e in r["snipe"]])
    lcr = pd.DataFrame([e for r in lres for e in r["crossed"]])
    hist_live: dict = {}
    for r in lres:
        for k, h in r["hist"].items():
            hist_live[k] = hist_live.get(k, 0) + h
    linfo.to_csv(out / "carnet_fenetres.csv", index=False, float_format="%.6g")
    lep.to_csv(out / "carnet_episodes.csv", index=False, float_format="%.6g")
    lsn.to_csv(out / "carnet_gagnant_connu.csv", index=False, float_format="%.6g")
    lcr.to_csv(out / "carnet_up_plus_down.csv", index=False, float_format="%.6g")
    hrows = [{"phase": k, "marge_c_bas": lo, "marge_c_haut": hi, "ms": float(c)}
             for k, h in hist_live.items() for lo, hi, c in zip(MARGIN_BINS[:-1], MARGIN_BINS[1:], h) if c]
    pd.DataFrame(hrows).to_csv(out / "carnet_distribution_marge.csv", index=False, float_format="%.6g")
    lat_rows = []
    for lat in LATENCIES_MS:
        if len(lep):
            lat_rows.append({"latence_ms": lat, "episodes": len(lep), "profit_total": float(lep[f"profit_lat_{lat}"].sum()),
                             "survie": float((lep[f"fraction_lat_{lat}"] >= 0.999).mean()),
                             "fraction_moyenne": float(lep[f"fraction_lat_{lat}"].mean()),
                             "episodes_profit_pos": int((lep[f"profit_lat_{lat}"] > 1e-9).sum()),
                             "parts_orphelines": float(lep[f"orphelines_lat_{lat}"].sum())})
    lat_df = pd.DataFrame(lat_rows)
    lat_df.to_csv(out / "carnet_profit_latence.csv", index=False, float_format="%.6g")
    ctx.update(linfo=linfo, lep=lep, lsn=lsn, lcr=lcr, lat=lat_df, hist_live=hist_live, hist_hist=hist_hist)
    tls = [r["timeline"] for r in lres if r["timeline"] is not None]
    ctx["timeline"] = max(tls, key=lambda x: x["episode"]["profit_max"]) if tls else None

    # 4. figures
    with timer("4. figures"):
        if len(ep):
            ctx["titles"]["hist"] = plot_history_phases(ep, int(groups["S"].nunique()) if len(groups) else 0,
                                                        out / "historique_violations_par_phase.png")
        hours = float(linfo["ms_couverts"].sum() / 3.6e6) if len(linfo) and "ms_couverts" in linfo else 0.0
        ctx["live_hours"] = hours
        ctx["titles"]["life"] = plot_live_lifetime(lep, len(linfo), hours, out / "carnet_duree_de_vie.png")
        if len(lat_df):
            ctx["titles"]["lat"] = plot_latency(lat_df, len(lep), out / "carnet_profit_selon_latence.png")
        if hist_hist or hist_live:
            ctx["titles"]["dist"] = plot_margin_distribution(hist_hist, hist_live, out / "distribution_marge.png")
        if ctx["timeline"] is not None:
            ctx["titles"]["example"] = plot_example(ctx["timeline"], out / "carnet_exemple.png")
    ctx["elapsed"] = time.time() - t_all
    timer.rows.append({"etape": "total", "secondes": round(ctx["elapsed"], 1)})
    rt_df = pd.DataFrame(timer.rows)
    rt_df.to_csv(out / "runtime.csv", index=False)
    ctx["runtime"] = rt_df
    write_readme(ctx, out)
    log.info("terminé en %.0f s -> %s", ctx["elapsed"], out)
    return 0


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
def write_readme(ctx: dict, out: Path) -> None:
    (out / "README.md").write_text(render_readme(ctx), encoding="utf-8")


def _frs(x) -> str:
    """« 0.250 / 0.710 » -> « 0,250 / 0,710 » (chaînes de prix/tailles des CSV)."""
    return str(x).replace(".", ",")


def _pct(a, b, nd=1) -> str:
    return fr(100 * a / b, nd) + " %" if b else "—"


def render_readme(ctx: dict) -> str:  # noqa: C901 (rédaction)
    L_: list[str] = []
    w = L_.append
    chain, logic, consec = ctx["chain"], ctx["logic"], ctx["consec"]
    hsum = ctx.get("hsum", pd.DataFrame())
    ep = ctx.get("ep", pd.DataFrame())
    sb = ctx.get("sbins", pd.DataFrame())
    snh = ctx.get("snipe_h", pd.DataFrame())
    lep, linfo, lat, lsn, lcr, rt = ctx["lep"], ctx["linfo"], ctx["lat"], ctx["lsn"], ctx["lcr"], ctx["rtds"]
    titles = ctx["titles"]
    btc = chain[chain["actif"] == "BTC"]
    n_pairs = int(btc["n"].sum())
    n_eq = int(btc["egal"].sum())
    n_viol = int(logic["violations"].sum())
    n_logic = int(logic["n"].iloc[0]) if len(logic) else 0
    cs = consec.set_index(["5m n", "5m n+1"])["part"] if len(consec) else pd.Series(dtype=float)
    A = hsum.set_index("variante").loc["A"] if len(hsum) else None
    B = hsum.set_index("variante").loc["B"] if len(hsum) else None
    C = hsum.set_index("variante").loc["C"] if len(hsum) else None
    n_live = int(len(lep))
    hours = ctx.get("live_hours", 0.0)
    lat_i = lat.set_index("latence_ms") if len(lat) else None
    snt = sb.set_index("ecart_final") if len(sb) else None
    known_med = float(rt.loc[np.isfinite(rt["decalage_exact_s"]), "connu_avant_T_s"].median()) if len(rt) else math.nan

    w("# Arbitrages entre marchés liés : peut-on gagner à coup sûr, Up ou Down ?")
    w("")
    w(f"*Généré le {ctx['generated']} par `scripts/polymarket_arbitrage.py` (temps d'exécution total : {fr(ctx['elapsed'], 0)} s, "
      f"détail dans `runtime.csv`). Marchés BTC « Up or Down » 5 min, 15 min (et 4 h pour la chaîne). Historique : trades "
      f"preneurs du 04/09 au 24/09/2026 ; carnet réel : enregistrement WebSocket du 26/09/2026.*")
    w("")
    w("> Simulation papier, lecture seule de données publiques : aucun ordre, aucune clé. Depuis la France, Polymarket est en "
      "« close-only » : aucune de ces opérations n'y est possible. Il s'agit de répondre à une question, pas de trader.")
    w("")
    w("## 0. Réponse courte")
    w("")
    w("* **Prévoir Up ou Down à coup sûr est impossible.** Le meilleur signal trouvé ne dépasse pas ≈ 56 % de réussite à "
      "l'ouverture (`reports/polymarket/diagnostic.md`). La seule façon de gagner quel que soit le résultat est d'acheter, sur des "
      "marchés dont les issues sont liées, un ensemble de jetons qui paie au moins 1 $ dans tous les cas et coûte moins de 1 $, frais compris.")
    w(f"* **Les marchés sont bien liés, et c'est vérifié.** La 15 min et ses trois 5 min forment une chaîne de niveaux Chainlink : "
      f"{fr(n_eq, 0)} égalités exactes sur {fr(n_pairs, 0)} paires comparées pour BTC (5m, 15m et 4h). Sur les issues officielles "
      f"de {fr(n_logic, 0)} fenêtres 15m, on compte {n_viol} violation des implications logiques (par exemple « trois 5m Up ⇒ 15m Up »). "
      f"Deux 5m consécutives, elles, ne sont **pas** liées : les quatre combinaisons Up/Down arrivent chacune environ 25 % du temps.")
    w("* **Une seule combinaison peut devenir gagnante à coup sûr.** Après la clôture de la 2e 5m, la 15m et la 3e 5m parient sur le "
      "**même prix final** avec deux seuils différents. Si le niveau V2 est au-dessus du niveau d'ouverture V0, « 5m#3 Up » entraîne "
      "« 15m Up » : acheter **15m Up + 5m#3 Down** paie alors toujours 1 $. Si V2 est en dessous, c'est **15m Down + 5m#3 Up**. "
      "Plus tôt dans la fenêtre, une couverture demande trois ou quatre jambes qui coûtent environ 1,5 à 2 $ : aucun cas trouvé.")
    ep_a_size = float(ep.loc[ep["variante"] == "A", "taille_min"].median()) if len(ep) else math.nan
    if A is not None:
        w(f"* **Historique (trades, 21 jours)** : une combinaison apparemment gagnante à coup sûr apparaît dans "
          f"**{fr(A['fenetres_gt_0'], 0)} fenêtres 15m sur {fr(A['fenetres'], 0)}** ({_pct(A['fenetres_gt_0'], A['fenetres'])}). "
          f"Au-dessus de 1 c : {fr(A['fenetres_gt_1c'], 0)} fenêtres ; au-dessus de 2 c : {fr(A['fenetres_gt_2c'], 0)}. "
          f"Elle survient toujours après la clôture de #2 et dure en médiane {fr(A['duree_mediane_s'], 0)} s. Profit total sur 21 jours, "
          f"limité aux tailles réellement échangées : **{usd(A['profit_10'])} à 10 parts, {usd(A['profit_50'])} à 50 parts, "
          f"{usd(A['profit_100'])} à 100 parts**, avec {fr(ep_a_size, 0)} parts en médiane sur la jambe la plus mince. En payant "
          f"1 c de plus par jambe, on tombe à {usd(B['profit_100'])} (100 parts). Mais la même méthode « voit » un Up + Down du même "
          f"marché coûtant moins de 1 $, ce qui est impossible dans le carnet, pendant {fr(100 * A['part_up_down_lt_1'], 1)} % des "
          f"secondes ({fr(100 * A['part_up_down_cout_lt_1'], 1)} % frais compris). Ces détections sont donc largement du bruit : "
          "les trades sont datés au bloc (≈ 2 s).")
    if n_live:
        l0 = lat_i.loc[0] if lat_i is not None else None
        w(f"* **Carnet réel à la milliseconde** ({len(linfo)} fenêtres 15m, {fr(hours, 1)} h de carnets 5m et 15m enregistrés "
          f"ensemble le 26/09) : **{n_live} violations**, durée de vie médiane **{fr(lep['duree_vie_ms'].median(), 1)} ms** (maximum "
          f"{fr(lep['duree_vie_ms'].max(), 1)} ms), {fr(lep['taille_min_meilleur_niveau'].median(), 0)} parts disponibles en médiane "
          f"sur la jambe la plus mince. Profit garanti si l'on était servi instantanément : {usd(l0['profit_total'])} au total. "
          f"Avec **100 ms** de retard : {usd(lat_i.loc[100, 'profit_total'])} ; avec 300 ms : {usd(lat_i.loc[300, 'profit_total'])}. "
          f"**Rien n'est exécutable.**")
    else:
        w(f"* **Carnet réel à la milliseconde** ({len(linfo)} fenêtres 15m, {fr(hours, 1)} h de carnets 5m et 15m enregistrés "
          "ensemble le 26/09) : **aucune violation** au meilleur ask, frais compris.")
    if snt is not None and len(snh):
        tot = snt.loc["Tous"]
        near = snt.loc["< 0,1 pb"]
        w(f"* **Le seul « coup sûr » qui rapporte : l'issue déjà connue.** Le TWAP final ne dépend que des points Chainlink "
          f"horodatés jusqu'à **2 à 3 s avant la clôture**. On le recalcule exactement depuis le flux RTDS (écart < 1e-6 $ quand "
          f"aucun point ne manque), et il est connu environ **{fr(known_med, 1)} s avant la fin**. Sur 21 jours, des preneurs ont "
          f"acheté le gagnant après la clôture avec une marge "
          f"positive sur **{fr(tot['marches_gagnant_achete_apres'], 0)} marchés sur {fr(tot['marches'], 0)}**, pour "
          f"**{usd(tot['profit_apres'], 0)}** au total. Sur les quasi-égalités (écart final < 0,1 pb), le gagnant restait à vendre "
          f"sous 1 $ après la clôture dans {fr(near['marches_gagnant_achete_apres'], 0)} marchés sur {fr(near['marches'], 0)} "
          f"({usd(near['profit_apres'], 0)}), parfois à quelques centimes. C'est une course de vitesse (délai médian ≈ 0 à 2 s "
          "après la clôture), réservée aux robots qui lisent Chainlink en direct.")
    ex = ctx.get("timeline")
    if ex is not None:
        e = ex["episode"]
        w(f"* **Exemple réel** ({pd.Timestamp(e['rx0'], unit='ns').strftime('%d/%m %H:%M:%S.%f')[:-3]} UTC, {e['phase']}) : "
          f"{e['portefeuille']} aux prix {_frs(e['prix_jambes'])} ; coût frais compris {cents(1 - e['marge_debut'], 2)} pour un paiement "
          f"garanti de 1 $, soit **{cents(e['marge_debut'], 2)} de gain sûr par lot**. Mais la jambe la plus mince ne portait que "
          f"{fr(e['taille_min_meilleur_niveau'], 0)} part(s) (profit maximal {usd(e['profit_max'])}) et la situation a disparu en "
          f"**{fr(e['duree_vie_ms'], 2)} ms**.")
    elif len(ep):
        e = ep[ep["variante"] == "C"].sort_values("profit_100", ascending=False).head(1)
        if len(e):
            e = e.iloc[0]
            w(f"* **Exemple (trades)** : {e['slug15']}, {e['t0_rel_s']} s après l'ouverture ({e['phase']}) : {e['portefeuille']} "
              f"aux prix {_frs(e['prix_jambes'])} → marge {cents(e['marge_debut'], 1)} par lot, {usd(e['profit_100'])} pour 100 parts.")
    w("* **Verdict.** Oui, il existe des instants où une combinaison gagne à coup sûr. Mais entre marchés liés, ces instants sont "
      "rares, minuscules (quelques parts, quelques centimes) et durent moins d'une seconde : ils ne sont pas exécutables. Le vrai "
      "gain sûr, acheter le gagnant d'une quasi-égalité dans les 2 s qui entourent la clôture, suppose de recalculer le TWAP "
      "Chainlink en direct et de battre des robots déjà présents. Ce n'est pas une stratégie accessible depuis la France (close-only).")
    w("")

    # 1. chaîne
    w("## 1. La chaîne de niveaux (vérification)")
    w("")
    w("Chaque marché compare deux TWAP Chainlink sur 60 s : « Up » si `F ≥ K` (l'égalité va à Up). Pour une 15m `[S, S+900)` "
      "et ses trois 5m, on a V0 = K(15m) = K(5m#1), V1 = F(5m#1) = K(5m#2), V2 = F(5m#2) = K(5m#3) et V3 = F(5m#3) = F(15m). "
      "La 4h (alignée sur l'heure de New York, soit 00h, 04h… UTC en septembre) commence et finit sur les mêmes niveaux que ses 16 fenêtres 15m. "
      f"Sources : caches `event_meta` ({fr(ctx['n_meta'], 0)} marchés BTC/ETH/SOL 5m et 15m, du 14/08 au 24/09) et `eventMetadata` "
      f"des {ctx['n_4h']} fenêtres 4h BTC lues sur gamma (lecture seule, en mémoire).")
    w("")
    w(to_markdown(chain[["actif", "relation", "n", "egal", "taux_egal", "ecart_max"]].rename(columns={
        "taux_egal": "taux d'égalité exacte", "ecart_max": "écart max ($)", "egal": "égalités"}),
        {"taux d'égalité exacte": "1%", "écart max ($)": 6}))
    w("")
    w("Implications logiques, contrôlées sur les **issues officielles** (`outcomePrices`, BTC, 04/09 → 24/09) :")
    w("")
    w(to_markdown(logic))
    w("")
    if len(consec):
        w(f"Deux 5m consécutives ne sont soumises à **aucune contrainte logique** : elles partagent un niveau (F de l'une = K de "
          f"l'autre), mais « V1 ≥ V0 » et « V2 ≥ V1 » peuvent se combiner librement. Les quatre combinaisons arrivent : Up→Up "
          f"{fr(100 * cs.get(('Up', 'Up'), np.nan), 1)} %, Up→Down {fr(100 * cs.get(('Up', 'Down'), np.nan), 1)} %, Down→Up "
          f"{fr(100 * cs.get(('Down', 'Up'), np.nan), 1)} %, Down→Down {fr(100 * cs.get(('Down', 'Down'), np.nan), 1)} % "
          f"(n = {fr(consec['n'].sum(), 0)}, `5m_consecutives.csv`). Aucun arbitrage n'est possible entre elles.")
        w("")

    # 2. méthode
    w("## 2. Méthode : issues possibles et programme linéaire")
    w("")
    w("À un instant t, on connaît certains niveaux (V0 dès l'ouverture, V1 à la clôture de #1, V2 à la clôture de #2). "
      "`possible_outcomes` énumère tous les vecteurs d'issues encore possibles pour les marchés ouverts : les niveaux inconnus "
      "sont libres, on parcourt tous leurs ordres relatifs (égalités comprises) entre eux et par rapport aux niveaux connus. "
      "Avant la clôture de #1, il reste 14 vecteurs sur 16 : « tous Up ⇒ 15m Up » et « tous Down ⇒ 15m Down ». Entre #1 et #2, "
      "il en reste 7, et 3 après #2.")
    w("")
    w("`solve_arbitrage` résout ensuite le programme linéaire (`scipy.optimize.linprog`, HiGHS). Variables : les quantités x ≥ 0 "
      "de chaque jeton, par niveau de prix et dans la limite des tailles. Objectif : maximiser le paiement minimal garanti moins "
      "le coût, avec un coût par part égal à p + 0,07·p·(1−p). Il y a arbitrage si l'optimum est strictement positif. Pour balayer "
      "des millions d'instants, on énumère une fois les **sommets** du polyèdre des portefeuilles couvrants {x ≥ 0 : A·x ≥ 1}. "
      "Le coût minimal d'un paiement garanti de 1 $ est alors un simple minimum de produits (`min_cover_cost`), égal exactement à "
      "l'optimum du programme linéaire (vérifié par les tests sur des prix tirés au hasard). Le programme complet, avec niveaux et "
      "tailles, n'est résolu qu'au début de chaque épisode. Tests : `tests/test_polymarket_arbitrage.py` (violation évidente "
      "trouvée, prix cohérents sans arbitrage, frais qui annulent une violation de 1 c, tailles et niveaux, branchement de la 4h).")
    w("")
    w("Sommets (portefeuilles couvrants minimaux) par phase : « Up + Down » d'un même marché (toujours ≥ 1 $ dans un carnet "
      "unifié) ; avant la clôture de #1, 15m Up + trois 5m Down et 15m Down + trois 5m Up ; entre #1 et #2, 15m Up + 5m#2 Down + "
      "5m#3 Down si V1 ≥ V0 (sinon 15m Down + 5m#2 Up + 5m#3 Up) ; après #2, **15m Up + 5m#3 Down** si V2 ≥ V0 (sinon **15m Down + 5m#3 Up**).")
    w("")

    # 3. historique
    if A is not None:
        w("## 3. Historique 04/09 → 24/09 (trades preneurs) : une détection, pas une preuve")
        w("")
        w(f"{fr(ctx.get('n_trades', 0), 0)} trades preneurs BTC 5m et 15m. Pour chaque fenêtre 15m et ses trois 5m, on estime à chaque seconde "
          "le prix d'achat de chacun des 8 jetons à partir du dernier achat preneur de ce jeton. Les niveaux connus viennent des "
          "`finalPrice` : V1 est connu dès la clôture de #1 (en temps réel, il le serait via Chainlink, voir § 4). Trois variantes "
          "d'estimation : (A) dernier achat de moins de 5 s, comme demandé ; (B) la même + 1 c, prudente ; (C) prix le plus élevé "
          "payé dans le dernier bloc, de moins de 2 s. La variante C est robuste à l'ordre inconnu des trades dans un même bloc. "
          "Quand l'estimation donne Up + Down < 1 $ pour un même marché, ce qui est impossible dans le carnet unifié, l'estimation "
          "la plus ancienne est jetée.")
        w("")
        t = hsum[["variante", "prix_estime", "fenetres", "fenetres_gt_0", "fenetres_gt_1c", "fenetres_gt_2c", "episodes",
                  "episodes_apres_cloture_2", "duree_mediane_s", "duree_p90_s", "profit_10", "profit_50", "profit_100",
                  "profit_100_sans_limite", "part_up_down_lt_1", "part_up_down_cout_lt_1"]].rename(columns={
            "prix_estime": "prix estimé", "fenetres": "fenêtres 15m", "fenetres_gt_0": "avec opportunité > 0",
            "fenetres_gt_1c": "> 1 c", "fenetres_gt_2c": "> 2 c", "episodes": "épisodes", "episodes_apres_cloture_2": "dont après clôture #2",
            "duree_mediane_s": "durée médiane (s)", "duree_p90_s": "durée p90 (s)", "profit_10": "profit 10 parts ($)",
            "profit_50": "profit 50 parts ($)", "profit_100": "profit 100 parts ($)", "profit_100_sans_limite": "100 parts sans limite de taille ($)",
            "part_up_down_lt_1": "secondes Up+Down < 1 (impossible)", "part_up_down_cout_lt_1": "idem frais compris"})
        w(to_markdown(t, {"durée médiane (s)": 0, "durée p90 (s)": 0, "profit 10 parts ($)": 2, "profit 50 parts ($)": 2,
                          "profit 100 parts ($)": 2, "100 parts sans limite de taille ($)": 2,
                          "secondes Up+Down < 1 (impossible)": "1%", "idem frais compris": "2%"}))
        w("")
        w("*Profit « N parts » : programme linéaire au début de chaque épisode, avec un paiement garanti ≤ N $ et chaque jambe "
          "limitée à la taille échangée à ce prix dans la seconde (ce qu'un preneur a réellement obtenu). « Sans limite de taille » : "
          "N × marge. Un épisode = des secondes consécutives avec une marge > 0 dans la même phase. Sa durée reflète surtout la "
          "fenêtre de 5 s de l'estimateur.*")
        w("")
        w(f"![violations par phase](historique_violations_par_phase.png)")
        w("")
        w(f"*{titles.get('hist', '')}*")
        w("")
        if len(ep):
            pf = ep[ep["variante"] == "A"]["portefeuille"].value_counts()
            w("Portefeuilles détectés (variante A) : " + " ; ".join(f"{k} : {v}" for k, v in pf.items()) + ". Aucune détection avant "
              "la clôture de #2 : les couvertures à trois ou quatre jambes coûtent toujours plus de 1 $.")
            w("")
            top = ep[ep["variante"] == "C"].sort_values("profit_100", ascending=False).head(8)
            if len(top):
                w("Les plus gros épisodes, variante robuste C (détail complet : `historique_episodes.csv`) :")
                w("")
                top = top.assign(prix_jambes=top["prix_jambes"].map(_frs), tailles_jambes=top["tailles_jambes"].map(_frs))
                w(to_markdown(top[["slug15", "t0_rel_s", "duree_s", "portefeuille", "prix_jambes", "ages_jambes_s", "tailles_jambes",
                                   "marge_debut", "profit_100"]].rename(columns={
                    "t0_rel_s": "t (s après S)", "duree_s": "durée (s)", "prix_jambes": "prix", "ages_jambes_s": "âges (s)",
                    "tailles_jambes": "tailles", "marge_debut": "marge", "profit_100": "profit 100 parts ($)"}),
                    {"marge": "1%", "profit 100 parts ($)": 2}, code_columns=["slug15"]))
                w("")
        w("**Pourquoi c'est surtout du bruit.** Les trades sont datés au bloc (≈ 2 s) et l'ordre des trades dans un bloc est inconnu. "
          "Lors d'un mouvement brusque, le « dernier » achat d'un jeton peut précéder le mouvement et celui de l'autre le suivre. "
          "Exemple du 10/09 : dans le même bloc, la 5m#3 Down s'échange entre 0,51 et 0,91 et la 15m Up entre 0,27 et 0,50. "
          f"L'estimateur fabrique alors des « arbitrages » impossibles. La preuve : il voit Up + Down < 1 $ sur un même marché pendant "
          f"{fr(100 * A['part_up_down_lt_1'], 1)} % (A) ou {fr(100 * C['part_up_down_lt_1'], 1)} % (C) des secondes où les deux prix "
          "existent. Or le carnet réel ne le permet jamais (§ 6). Seul le carnet enregistré permet de dire si c'est exécutable.")
        w("")

    # 4. carnet réel
    w("## 4. Carnet réel à la milliseconde (26/09/2026)")
    w("")
    n_groups = int(len(linfo))
    w(f"Enregistrement WebSocket CLOB des carnets BTC 5m **et** 15m en même temps (collecteur `scripts/polymarket_live_collector.py`, "
      f"depuis 04:30 UTC, panne de 06:05 à 10:00). {n_groups} fenêtres 15m terminées sont analysées, soit {fr(hours, 1)} h pendant "
      "lesquelles la marge est calculable. On reconstruit les quatre carnets dans le repère Up (carnet unifié : ask Down = 1 − bid Up) "
      "et on garde les trois meilleurs niveaux de chaque jeton après chaque message qui les modifie. Les horloges sont celles de "
      "réception locale. V0 à V3 viennent du flux **Chainlink RTDS** quand il est enregistré (depuis 10:29 UTC), sinon de gamma "
      "(`priceToBeat` des fenêtres suivantes). Un marché dont le carnet est croisé (bid ≥ ask, état intermédiaire entre deux messages) "
      "ou dont l'enregistrement est interrompu est ignoré pendant ce temps.")
    w("")
    if len(rt):
        ok = rt[np.isfinite(rt["V_gamma"])]
        full = ok[ok["points"] == 60]
        n3 = int((ok["decalage_exact_s"] == 3).sum())
        n2 = int((ok["decalage_exact_s"] == 2).sum())
        nx = int(np.isfinite(ok["decalage_exact_s"]).sum())
        amb_pb = float((ok["ecart_V_2s_3s"].abs() / ok["V_gamma"] * 1e4).median()) if len(ok) else math.nan
        w(f"**Niveaux via Chainlink RTDS.** V(T) est reproduit **exactement** (écart < 1e-6 $) par la moyenne des 60 points "
          f"`btc/usd` du flux RTDS qui finissent 3 s avant T ({n3} fois) ou 2 s avant T ({n2} fois). Sur {len(ok)} niveaux dont "
          f"gamma donne la valeur officielle, {nx} sont reproduits ; les autres ont un point manquant dans l'enregistrement "
          f"({len(ok) - len(full)} niveaux avec moins de 60 points). Sans décalage, l'écart atteint "
          f"{fr(np.nanmax(np.abs(ok['ecart_sans_decalage'])) if len(ok) else np.nan, 2)} $. Le dernier point utile arrive "
          f"**{fr(known_med, 2)} s avant T** en médiane. Le niveau, donc l'issue d'une fenêtre qui se termine en T, est connu "
          f"juste avant la clôture. Mais la fenêtre exacte (2 s ou 3 s) n'est pas connue d'avance, et les deux calculs diffèrent "
          f"de {fr(amb_pb, 3)} pb en médiane. Une quasi-égalité plus serrée que cela reste indécidable en temps réel, tout comme "
          "un point manquant. Détail : `chainlink_rtds_niveaux.csv`.")
        w("")
    if n_live:
        w(f"**{n_live} violations** au meilleur ask, frais compris, toutes inter-marchés (les carnets croisés sont exclus) :")
        w("")
        lep_md = lep.assign(prix_jambes=lep["prix_jambes"].map(_frs), tailles_meilleur_niveau=lep["tailles_meilleur_niveau"].map(_frs),
                            V_sources=lep["V_sources"].str.replace(",", ", "))
        w(to_markdown(lep_md[["slug15", "phase", "t0_rel_s", "duree_vie_ms", "portefeuille", "prix_jambes", "tailles_meilleur_niveau",
                           "marge_debut", "profit_max", "V_sources"]].rename(columns={
            "t0_rel_s": "t (s après S)", "duree_vie_ms": "durée de vie (ms)", "prix_jambes": "asks", "tailles_meilleur_niveau": "tailles",
            "marge_debut": "marge", "profit_max": "profit max ($)", "V_sources": "source des V"}),
            {"t (s après S)": 3, "durée de vie (ms)": 2, "marge": "2%", "profit max ($)": 3}, code_columns=["slug15"], max_rows=30))
        w("")
    else:
        w("**Aucune violation** inter-marchés au meilleur ask, frais compris, sur la période enregistrée.")
        w("")
    w("![durée de vie](carnet_duree_de_vie.png)")
    w("")
    w(f"*{titles.get('life', '')}*")
    w("")
    if len(lat):
        w(f"**Exécution avec retard.** À la détection, on planifie un lot de {LIVE_PLAN_SETS} parts au meilleur prix, sur trois "
          "niveaux au plus. Les ordres arrivent 0, 100, 300, 1 000 ou 3 000 ms plus tard et ne sont remplis qu'aux prix inférieurs ou "
          "égaux au prix limite détecté. Chaque jambe est confrontée au carnet à l'instant d'arrivée, et la jambe la plus pauvre fixe "
          "la part du lot couverte. Les parts achetées en trop sur une jambe restent « orphelines » : non couvertes, donc non comptées.")
        w("")
        w(to_markdown(lat.rename(columns={"latence_ms": "retard (ms)", "episodes": "violations", "profit_total": "profit garanti total ($)",
                                          "survie": "lots complets", "fraction_moyenne": "part moyenne du lot",
                                          "episodes_profit_pos": "violations encore rentables", "parts_orphelines": "parts orphelines"}),
                      {"profit garanti total ($)": 3, "lots complets": "0%", "part moyenne du lot": 2, "parts orphelines": 1}))
        w("")
        w("![profit selon la latence](carnet_profit_selon_latence.png)")
        w("")
        w(f"*{titles.get('lat', '')}*")
        w("")
    if "example" in titles:
        w("![exemple](carnet_exemple.png)")
        w("")
        w(f"*{titles['example']}*")
        w("")
    if "dist" in titles:
        w("![distribution de la marge](distribution_marge.png)")
        w("")
        w(f"*{titles['dist']}*")
        w("")

    # 5. issue déjà connue
    w("## 5. Issue déjà connue : acheter le gagnant autour de la clôture")
    w("")
    w("Ce n'est pas un arbitrage entre marchés, mais c'est le seul autre moyen de « gagner à coup sûr ». Une fois le TWAP final "
      "figé (2 à 3 s avant la clôture, § 4), le gagnant paie 1 $ avec certitude. L'acheter à p < 1 − frais rapporte "
      "1 − p − 0,07·p·(1−p).")
    w("")
    if len(sb):
        w("Historique : trades preneurs datés d'un bloc ≥ clôture E, selon l'écart final |F/K − 1|. Les colonnes « E−2…E−1 s » "
          "comptent les blocs datés E−2 s et E−1 s. Le TWAP y est déjà figé, mais son dernier point n'est publié sur RTDS que "
          "vers E − 1,7 s : ces achats ne sont que partiellement « sûrs » et ne sont pas comptés dans le résumé.")
        w("")
        w(to_markdown(sb.rename(columns={"ecart_final": "écart final", "marches": "marchés",
                                         "marches_gagnant_achete_apres": "gagnant acheté après E (marge > 0)",
                                         "profit_apres": "profit des preneurs après E ($)", "parts_apres": "parts",
                                         "prix_min_median_apres": "prix minimal médian", "marches_gagnant_achete_avant": "idem en E−2…E−1 s",
                                         "profit_avant": "profit E−2…E−1 s ($)"}),
                      {"profit des preneurs après E ($)": 0, "parts": 0, "prix minimal médian": 3, "profit E−2…E−1 s ($)": 0}))
        w("")
        top = snh.sort_values("profit_apres", ascending=False).head(6)
        w("Plus gros cas (`historique_gagnant_apres_cloture.csv`) : " + " ; ".join(
            f"`{r.slug}` (écart {fr(r.ecart_pb, 3, signed=True)} pb, gagnant acheté dès {fr(r.prix_min_apres, 3)}, délai médian "
            f"{fr(r.delai_median_s_apres, 0)} s, {usd(r.profit_apres, 0)})" for r in top.itertuples()) + ".")
        w("")
        w("Quand l'écart final est minuscule, le marché, qui raisonne sur le prix spot, ne sait pas qui a gagné au moment de la "
          "clôture. Celui qui recalcule le TWAP Chainlink sait. Certains cas avec un délai de plusieurs minutes (jusqu'à 40 min) "
          "portent en plus un risque de résolution : le marché hésitait encore. Les montants sont concentrés sur quelques fenêtres.")
        w("")
    if len(lsn):
        d = lsn.copy()
        cols = [c for c in ["slug", "gagnant", "ecart_pb", "connu_via_rtds_avant_E_s", "ask_m10", "marge_m10", "ask_m3", "ask_rtds", "ask_E",
                            "marge_max_apres_connu", "profit_max_etat_apres_connu"] if c in d.columns]
        near = d[d["ecart_pb"].abs() < 0.5] if "ecart_pb" in d else d
        n_any = int((d.get("marge_max_apres_connu", pd.Series(dtype=float)) > 0).sum())
        w(f"Carnet réel : sur {len(d)} marchés, le gagnant était encore à vendre avec une marge positive après l'instant où l'issue "
          f"est connue (RTDS, ou E − 1,7 s à défaut) dans **{n_any}** cas. Quasi-égalités (|écart| < 0,5 pb) :")
        w("")
        w(to_markdown(near[cols].rename(columns={"ecart_pb": "écart (pb)", "connu_via_rtds_avant_E_s": "connu avant E (s)",
                                                 "ask_m10": "ask gagnant E−10 s", "marge_m10": "marge E−10 s", "ask_m3": "ask E−3 s",
                                                 "ask_rtds": "ask quand connu", "ask_E": "ask à E",
                                                 "marge_max_apres_connu": "marge max après connu",
                                                 "profit_max_etat_apres_connu": "profit max ($)"}),
                      {"écart (pb)": "+3", "connu avant E (s)": 2, "ask gagnant E−10 s": 3, "marge E−10 s": "1%", "ask E−3 s": 3,
                       "ask quand connu": 3, "ask à E": 3, "marge max après connu": "1%", "profit max ($)": 2}, code_columns=["slug"]))
        w("")
        w("*— = aucun ask du gagnant (personne ne le vend) ou marché plus enregistré. À E−10 s, le TWAP n'est pas encore figé : "
          "acheter alors n'est pas un coup sûr.*")
        w("")

    # 6. autres relations
    w("## 6. Autres relations testées")
    w("")
    if len(lcr):
        tot_states = int(lcr["etats_deux_cotes"].sum())
        lt1 = int(lcr["etats_somme_lt_1"].sum())
        ms_lt1 = float(lcr["ms_somme_lt_1"].sum())
        w(f"* **Up + Down du même marché** : dans le carnet unifié, ask Up + ask Down = 1 + écart ≥ 1 $. Vérifié sur le carnet "
          f"reconstruit : médiane sur les états {fr(lcr['somme_mediane'].median(), 3)} $. Seuls "
          f"{fr(lt1, 0)} états sur {fr(tot_states, 0)} ({_pct(lt1, tot_states, 3)}) passent sous 1 $, pour {fr(ms_lt1 / 1000, 1)} s "
          "au total. Ce sont tous des carnets croisés, donc des états intermédiaires de la reconstruction : messages entre deux "
          "mises à jour, ou niveaux hors de la bande de ± 0,10 que le collecteur ne suit pas. Le moteur d'appariement ne peut pas "
          "être croisé. Détail : `carnet_up_plus_down.csv`.")
    if A is not None:
        w(f"* Dans les trades historiques, la même relation paraît violée pendant {fr(100 * A['part_up_down_lt_1'], 1)} % des "
          f"secondes (variante A). Frais compris, c'est {fr(100 * A['part_up_down_cout_lt_1'], 2)} %. C'est la mesure du bruit de "
          "l'estimation par les trades (`historique_up_plus_down.csv`).")
    w("* **5m consécutives** : aucune contrainte logique (§ 1), donc aucun arbitrage possible.")
    w("* **4h** : la chaîne est vérifiée (K et F de la 4h égaux à ceux de ses 15m de début et de fin, 100 %). La 4h ne se "
      "branche sur une 15m que pendant la **dernière 15m** de sa fenêtre (6 fois par jour). Après la clôture de #2, 4h, 15m et 5m#3 "
      "sont alors trois paris sur V3 avec trois seuils, et leurs prix doivent être rangés dans l'ordre des seuils. Le programme "
      "linéaire gère ce cas (test `test_4h_ladder_arbitrage`), mais ni trades 4h ni carnet 4h ne sont enregistrés : non mesuré ici.")
    w("")

    # 7. limites
    w("## 7. Limites")
    w("")
    w("* **Historique = détection.** Prix estimés par des trades datés au bloc, sans carnet. Une « opportunité » historique n'est "
      "pas une preuve d'exécution. Les tailles utilisées sont celles réellement échangées, pas la profondeur disponible.")
    w(f"* **Carnet réel = court échantillon** ({fr(hours, 1)} h, une seule journée, avec une panne de 06:05 à 10:00). Les "
      "`price_change` sont filtrés à ± 0,10 du milieu par le collecteur. Lors d'un saut de plus de 10 c, des niveaux éloignés "
      "peuvent rester périmés jusqu'à l'instantané suivant. Ces états sont presque toujours croisés, donc exclus, mais une "
      "violation fantôme de quelques ms ne peut pas être totalement écartée. Cela renforce la conclusion : rien d'exploitable.")
    w("* Retard mesuré sur l'horloge de réception locale. Un ordre réel ajoute l'aller vers le serveur, la mise en file et le "
      "bloc de règlement. Les résultats avec retard sont donc optimistes.")
    w("* Frais : barème `crypto_fees_v2` (0,07·p·(1−p) par part, preneur), sans arrondi. Le gain du gagnant connu suppose une "
      "résolution conforme à la règle (vérifiée à 100 % sur 8 064 marchés) et un flux RTDS complet.")
    w("* 4h non mesurée (pas de données de prix). ETH/SOL : seule la chaîne est vérifiée.")
    w("")

    # 8. fichiers
    w("## 8. Fichiers et temps d'exécution")
    w("")
    files = [
        ("chaine_verification.csv", "égalités K/F par relation et par actif (5m, 15m, 4h)"),
        ("controles_logiques.csv", "implications logiques sur les issues officielles"),
        ("5m_consecutives.csv", "table 2×2 des issues de deux 5m consécutives"),
        ("historique_resume.csv", "résumé par variante d'estimation (fenêtres, épisodes, profits, bruit Up+Down)"),
        ("historique_episodes.csv", "chaque épisode détecté dans les trades : phase, durée, portefeuille, prix, âges, tailles, profits 10/50/100"),
        ("historique_fenetres.csv", "marge maximale par fenêtre 15m et par variante"),
        ("historique_up_plus_down.csv", "secondes où Up + Down < 1 dans l'estimation (bruit)"),
        ("historique_distribution_marge.csv", "distribution de la marge par phase (secondes)"),
        ("historique_gagnant_apres_cloture.csv", "achats preneurs du gagnant après la clôture (et 2 s avant), par marché"),
        ("historique_gagnant_par_ecart.csv", "idem agrégé par écart final"),
        ("chainlink_rtds_niveaux.csv", "niveaux V recalculés depuis Chainlink RTDS contre gamma, avance à la connaissance"),
        ("carnet_fenetres.csv", "fenêtres 15m du carnet réel : états, sources des V, marge maximale"),
        ("carnet_episodes.csv", "violations du carnet réel : durée de vie (ms), tailles, profits, exécution avec retard"),
        ("carnet_profit_latence.csv", "profit garanti selon le retard"),
        ("carnet_distribution_marge.csv", "distribution de la marge (ms) par phase"),
        ("carnet_up_plus_down.csv", "contrôle Up + Down ≥ 1 par marché"),
        ("carnet_gagnant_connu.csv", "ask du gagnant autour de la clôture (E−10 s, E−3 s, connu via RTDS, E, E+2 s)"),
    ]
    w(to_markdown(pd.DataFrame(files, columns=["fichier", "contenu"]), code_columns=["fichier"]))
    w("")
    w(to_markdown(ctx["runtime"], {"secondes": 1}))
    w("")
    w("Relancer : `. .venv/bin/activate && python scripts/polymarket_arbitrage.py` (≈ 2 min d'historique avec 2 processus, puis ≈ 20 s par "
      "fenêtre 15m de carnet). Le collecteur peut tourner en parallèle.")
    w("")
    return "\n".join(L_)


if __name__ == "__main__":
    sys.exit(main())
