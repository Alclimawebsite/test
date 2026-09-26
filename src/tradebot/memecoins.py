"""Memecoins : la « rotation » entre community coins bat-elle la simple détention ?

Hypothèse testée (fil X « Ultimate Memecoin Trading Guide », 2026) : parmi des memecoins établis,
vendre celui qui vient de faire « 3x » et acheter celui qui est « en bas de sa fourchette »
rapporterait bien plus que de garder ses positions (« Rotator : 10 k$ → 270 k$, Holder : 27 k$ »).

Données (publiques, lecture seule, aucune clé) :

* univers : tous les contrats perpétuels USDT de Binance que Binance classe « Meme »
  (``underlyingSubType``), **y compris ceux retirés de la cote** (statut ``SETTLING``) : pas de
  biais du survivant par rapport à cette cote. Le prix du perpétuel suit le prix spot à quelques
  pb près ; on ne l'utilise que comme prix (pas de levier, pas de financement) ;
* bougies quotidiennes UTC (00:00 → 23:59:59.999) de ``www.binance.com/fapi/v1/klines``
  (``fapi.binance.com`` répond 451 depuis certains serveurs) ; cache dans
  ``data/cache/memecoins/``.

Tout est **point-in-time** : une décision prise à la clôture du jour ``d`` n'utilise que des
données jusqu'à ``d`` inclus et s'exécute le jour ``d + 1`` (à l'ouverture, ≈ la clôture de
``d`` sur un marché ouvert 24 h/24, ou à la clôture de ``d + 1`` en variante prudente), avec un
coût par échange (frais + glissement) appliqué à chaque vente et à chaque achat.

Recherche sur données historiques uniquement : aucun ordre, aucune recommandation.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import kurtosis as _kurt
from scipy.stats import norm, skew as _skew, spearmanr

from tradebot.config import CACHE_DIR

FAPI = "https://www.binance.com/fapi/v1"
CACHE = CACHE_DIR / "memecoins"
DAY_MS = 86_400_000
EULER = 0.5772156649015329


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def _get_json(url: str, params: dict | None = None, tries: int = 5):
    q = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    err = None
    for k in range(tries):
        try:
            req = urllib.request.Request(q, headers={"User-Agent": "tradebot-research/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 — réseau : on réessaie
            err = exc
            time.sleep(2 ** k)
    raise RuntimeError(f"échec {q} : {err!r}")


def fetch_exchange_info(refresh: bool = False) -> dict:
    path = CACHE / "fapi_exchangeInfo.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    info = _get_json(f"{FAPI}/exchangeInfo")
    path.write_text(json.dumps(info))
    return info


def meme_universe(info: dict) -> pd.DataFrame:
    """Perpétuels USDT classés « Meme » par Binance, retirés de la cote compris."""
    rows = [{"symbol": s["symbol"], "base": s["baseAsset"], "status": s["status"],
             "onboard": pd.Timestamp(s["onboardDate"], unit="ms"),
             "tags": ",".join(s.get("underlyingSubType") or [])}
            for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and "Meme" in (s.get("underlyingSubType") or [])]
    return pd.DataFrame(rows).sort_values("onboard").reset_index(drop=True)


def fetch_daily_klines(symbol: str, refresh: bool = False, now_ms: int | None = None) -> pd.DataFrame:
    """Bougies quotidiennes complètes (la bougie du jour en cours est écartée)."""
    path = CACHE / "klines_1d" / f"{symbol}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["date"])
    now_ms = now_ms or int(time.time() * 1000)
    out, start = [], 1_546_300_800_000   # 2019-01-01
    while True:
        rows = _get_json(f"{FAPI}/klines", {"symbol": symbol, "interval": "1d", "limit": 1500, "startTime": start})
        if not rows:
            break
        out += rows
        if len(rows) < 1500:
            break
        start = rows[-1][0] + DAY_MS
        time.sleep(0.3)
    df = pd.DataFrame(out, columns=["open_ms", "open", "high", "low", "close", "volume", "close_ms",
                                    "qvol", "n_trades", "tb_base", "tb_quote", "ignore"])
    df = df[df["close_ms"] < now_ms].copy()
    df["date"] = pd.to_datetime(df["open_ms"], unit="ms").dt.normalize()
    for c in ("open", "high", "low", "close", "volume", "qvol"):
        df[c] = df[c].astype(float)
    df = df[["date", "open", "high", "low", "close", "volume", "qvol", "n_trades"]].drop_duplicates("date")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    time.sleep(0.3)
    return df


@dataclass
class Panel:
    """Tableaux jour × symbole (index : date UTC de la bougie, qui clôture à 23:59:59 UTC)."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    qvol: pd.DataFrame
    active: pd.DataFrame          # échangé ce jour-là (volume > 0, avant le retrait de la cote)
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index


def build_panel(frames: dict[str, pd.DataFrame], meta: pd.DataFrame | None = None) -> Panel:
    """Assemble les bougies ; un symbole est inactif avant sa 1re bougie, les jours sans volume et
    après son dernier jour échangé (retrait de la cote : les perpétuels « SETTLING » continuent
    d'afficher un prix figé sans volume)."""
    fields = {k: {} for k in ("open", "high", "low", "close", "qvol")}
    for sym, df in frames.items():
        d = df.set_index("date").sort_index()
        for k in fields:
            fields[k][sym] = d[k]
    tabs = {k: pd.DataFrame(v).sort_index() for k, v in fields.items()}
    idx = pd.date_range(tabs["close"].index.min(), tabs["close"].index.max(), freq="D")
    tabs = {k: v.reindex(idx) for k, v in tabs.items()}
    traded = tabs["qvol"].fillna(0) > 0
    last = traded[::-1].cummax()[::-1]          # vrai jusqu'au dernier jour échangé
    active = traded & last & tabs["close"].notna()
    for k in ("open", "high", "low", "close"):
        tabs[k] = tabs[k].where(last)            # rien après le retrait de la cote
    return Panel(tabs["open"], tabs["high"], tabs["low"], tabs["close"], tabs["qvol"], active,
                 meta if meta is not None else pd.DataFrame())


# ---------------------------------------------------------------------------
# Signaux (point-in-time : tout ce qui est calculé au jour d n'utilise que d et avant)
# ---------------------------------------------------------------------------
def rolling_low(close: pd.DataFrame, w: int) -> pd.DataFrame:
    return close.rolling(w, min_periods=w).min()


def range_position(close: pd.DataFrame, w: int) -> pd.DataFrame:
    """(prix − plus bas w j) / (plus haut w j − plus bas w j), fenêtre finissant au jour d inclus."""
    lo = close.rolling(w, min_periods=w).min()
    hi = close.rolling(w, min_periods=w).max()
    return (close - lo) / (hi - lo).where(hi > lo)


def community_flag(close: pd.DataFrame, crash: float = 0.70, rebound: float = 2.0) -> pd.DataFrame:
    """Critère « community coin » de l'auteur, point-in-time : a déjà pris au moins −70 % depuis un
    sommet puis a au moins doublé depuis le creux (état acquis définitivement ensuite)."""
    out = pd.DataFrame(False, index=close.index, columns=close.columns)
    for s in close.columns:
        x = close[s].to_numpy()
        peak, trough, crashed, ok = -np.inf, np.inf, False, False
        flags = np.zeros(x.size, dtype=bool)
        for i, v in enumerate(x):
            if not np.isfinite(v):
                flags[i] = ok
                continue
            if not ok:
                peak = max(peak, v)
                if not crashed and v <= (1 - crash) * peak:
                    crashed, trough = True, v
                if crashed:
                    trough = min(trough, v)
                    if v >= rebound * trough:
                        ok = True
            flags[i] = ok
        out[s] = flags
    return out


def delist_notice(panel: Panel, notice_days: int = 5) -> pd.DataFrame:
    """Vrai les ``notice_days`` derniers jours échangés d'une pièce retirée de la cote : Binance annonce
    ses retraits plusieurs jours à l'avance (par exemple NEIROETHUSDT : annonce le 22/09/2025, règlement
    le 26/09/2025), l'information est donc publique à ces dates."""
    act = panel.active.to_numpy(bool)
    n = act.shape[0]
    out = np.zeros_like(act)
    for j in range(act.shape[1]):
        idx = np.flatnonzero(act[:, j])
        if idx.size and idx[-1] < n - 1 and notice_days > 0:
            last = idx[-1]
            out[max(0, last - notice_days + 1): last + 1, j] = True
    return pd.DataFrame(out, index=panel.close.index, columns=panel.close.columns)


def eligibility(panel: Panel, min_age: int = 60, min_qvol: float = 5e6, qvol_win: int = 30,
                community: bool = False, notice_days: int = 5) -> pd.DataFrame:
    """Éligible au jour d : échangé ce jour, coté depuis ≥ min_age jours, volume quotidien médian
    (30 j) ≥ min_qvol USDT, pas de retrait de la cote annoncé, et, en option, critère « community coin »."""
    age = panel.active.cumsum()
    med = panel.qvol.where(panel.active).rolling(qvol_win, min_periods=qvol_win // 2).median()
    e = panel.active & (age >= min_age) & (med >= min_qvol)
    if notice_days:
        e &= ~delist_notice(panel, notice_days)
    if community:
        e &= community_flag(panel.close)
    return e.fillna(False)


# ---------------------------------------------------------------------------
# Moteur de rotation (K emplacements indépendants)
# ---------------------------------------------------------------------------
TRIGGERS = ("low", "range", "entry", "range_top")


@dataclass(frozen=True)
class RotationParams:
    mult: float = 3.0          # « a fait 3x » : voir ``trigger``
    low_win: int = 30          # fenêtre du plus bas pour trigger="low"
    range_win: int = 60        # fourchette pour « en bas de la fourchette » (et trigger "range"/"range_top")
    bottom: float = 0.20       # une rotation n'a lieu que si une pièce est sous ce seuil de sa fourchette
    k: int = 2                 # nombre de pièces détenues en même temps
    trigger: str = "low"       # "low" : clôture >= mult × plus bas des low_win j ; "range" : >= mult × plus bas
                               # des range_win j ; "entry" : >= mult × prix d'achat ; "range_top" : position
                               # >= 0,9 dans la fourchette et >= mult × plus bas de la fourchette
    target: str = "bottom"     # pièce achetée à la rotation : "bottom" (auteur), "random" (placebo), "top"
                               # (inverse, momentum), "none" (détenteur : aucune rotation)
    entry: str = "bottom"      # achats de départ (et après une sortie forcée) : "bottom", "random", "top"
    cost: float = 0.0075       # coût par échange (vente OU achat) : frais + glissement
    execution: str = "open"    # "open" : ouverture de d+1 ; "next_close" : clôture de d+1
    delist_haircut: float = 0.0  # décote si la pièce n'est plus échangée (sortie à la dernière clôture)
    notice_days: int = 5       # préavis de retrait : vente dès l'annonce (voir delist_notice)
    seed: int = 0


@dataclass
class SimResult:
    returns: pd.Series          # rendement quotidien du portefeuille (clôture à clôture)
    trades: pd.DataFrame        # une ligne par rotation / achat / sortie forcée
    holdings: pd.DataFrame      # symbole détenu par emplacement, chaque jour


def _prepare(panel: Panel, elig: pd.DataFrame, p: RotationParams):
    if p.trigger not in TRIGGERS:
        raise ValueError(f"trigger inconnu : {p.trigger}")
    C = panel.close.to_numpy(float)
    O = panel.open.to_numpy(float)
    act = panel.active.to_numpy(bool)
    E = elig.reindex_like(panel.close).fillna(False).to_numpy(bool)
    soon = delist_notice(panel, p.notice_days).to_numpy(bool)
    rpos = range_position(panel.close, p.range_win).to_numpy(float)
    win = p.low_win if p.trigger == "low" else p.range_win
    low = rolling_low(panel.close, win).to_numpy(float)
    with np.errstate(invalid="ignore"):
        trig = (C >= p.mult * low) & np.isfinite(low)
        if p.trigger == "range_top":
            trig &= rpos >= 0.9
    return C, O, act, E, soon, trig, low, rpos


def _pick(cands: np.ndarray, rpos_row: np.ndarray, rule: str, rng: np.random.Generator) -> int | None:
    if cands.size == 0:
        return None
    r = rpos_row[cands]
    ok = np.isfinite(r)
    if not ok.any():
        return None
    cands, r = cands[ok], r[ok]
    if rule == "random":
        return int(rng.choice(cands))
    if rule == "top":
        return int(cands[np.argmax(r)])
    return int(cands[np.argmin(r)])


def simulate_rotation(panel: Panel, elig: pd.DataFrame, p: RotationParams,
                      start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> SimResult:
    """Simule la rotation. Décision à la clôture du jour i, exécution au jour i + 1.

    * emplacement vide (départ, ou après une sortie forcée) : achète selon ``entry`` (par défaut la
      pièce éligible non détenue la plus basse dans sa fourchette) ; toutes les variantes (règle,
      placebo, inverse, détenteur) font donc les mêmes achats de départ ;
    * emplacement dont la pièce a « fait mult × » (``trigger``) : si une pièce éligible non détenue
      est sous ``bottom`` dans sa fourchette, on vend et on achète la cible ``target`` (la plus basse,
      une au hasard, ou la plus haute) ; sinon on garde. Déclenchements identiques pour toutes les cibles ;
    * retrait de la cote annoncé (``notice_days``) : vente le lendemain de l'annonce ; si la pièce n'est
      déjà plus échangée le lendemain, sortie à la dernière clôture (moins ``delist_haircut``).
    """
    C, O, act, E, soon, trig, low, rpos = _prepare(panel, elig, p)
    dates = panel.dates
    n, m = C.shape
    i0 = 0 if start is None else int(dates.searchsorted(start))
    i1 = n - 1 if end is None else min(n - 1, int(dates.searchsorted(end, side="right")) - 1)
    rng = np.random.default_rng(p.seed)
    slots: list[int | None] = [None] * p.k
    entry_px = np.full(p.k, np.nan)
    vals = np.full(p.k, 1.0 / p.k)
    rets = np.full(n, np.nan)
    hold = np.full((n, p.k), -1, dtype=int)
    trades = []
    c = p.cost

    def candidates(i, held):
        return np.array([x for x in np.flatnonzero(E[i]) if x not in held and act[i + 1, x]], dtype=int)

    for i in range(i0, i1):
        held = {s for s in slots if s is not None}
        actions: list[list] = []                    # [emplacement, vend, achète, motif]
        for j, s in enumerate(slots):
            if s is not None and (soon[i, s] or not act[i + 1, s]):
                actions.append([j, s, None, "retrait" if not act[i + 1, s] else "retrait annoncé"])
                held.discard(s)
        # emplacements vides ou libérés : entrée
        for j in range(p.k):
            prev = [a for a in actions if a[0] == j]
            if slots[j] is not None and not prev:
                continue
            b = _pick(candidates(i, held), rpos[i], p.entry, rng)
            if b is None:
                continue
            held.add(b)
            if prev:
                prev[0][2] = b
            else:
                actions.append([j, None, b, "entrée"])
        # rotations
        if p.target != "none":
            for j, s in enumerate(slots):
                if s is None or any(a[0] == j for a in actions):
                    continue
                fired = (C[i, s] >= p.mult * entry_px[j]) if p.trigger == "entry" else trig[i, s]
                if not fired:
                    continue
                cands = candidates(i, held)
                r = rpos[i, cands] if cands.size else np.array([])
                if not (r.size and np.nanmin(np.where(np.isfinite(r), r, np.inf)) <= p.bottom):
                    continue                        # pas de pièce en bas de fourchette : on garde
                b = _pick(cands, rpos[i], p.target, rng)
                if b is None:
                    continue
                held.discard(s)
                held.add(b)
                actions.append([j, s, b, "rotation"])
        # exécution au jour i + 1 et valorisation à la clôture de i + 1
        v0 = vals.sum()
        acted = {a[0] for a in actions}
        for j, s in enumerate(slots):
            if j in acted or s is None:
                continue
            vals[j] *= C[i + 1, s] / C[i, s]
        for j, sell, buy, why in actions:
            v = vals[j]
            if sell is not None:
                if not act[i + 1, sell]:
                    v *= (1.0 - p.delist_haircut) * (1.0 - c)       # plus échangée : dernière clôture
                elif p.execution == "open":
                    v *= O[i + 1, sell] / C[i, sell] * (1.0 - c)
                else:
                    v *= C[i + 1, sell] / C[i, sell] * (1.0 - c)
            if buy is not None:
                v *= 1.0 - c
                if p.execution == "open":
                    v *= C[i + 1, buy] / O[i + 1, buy]
                    px = O[i + 1, buy]
                else:
                    px = C[i + 1, buy]
                trades.append({"date": dates[i + 1], "slot": j,
                               "sell": None if sell is None else panel.symbols[sell], "buy": panel.symbols[buy],
                               "why": why,
                               "sell_vs_low": float(C[i, sell] / low[i, sell]) if sell is not None and np.isfinite(low[i, sell]) else np.nan,
                               "sell_vs_entry": float(C[i, sell] / entry_px[j]) if sell is not None else np.nan,
                               "buy_rpos": float(rpos[i, buy])})
                slots[j] = buy
                entry_px[j] = px
            else:
                trades.append({"date": dates[i + 1], "slot": j, "sell": panel.symbols[sell], "buy": None, "why": why,
                               "sell_vs_low": np.nan, "sell_vs_entry": float(C[i, sell] / entry_px[j]), "buy_rpos": np.nan})
                slots[j] = None
                entry_px[j] = np.nan
            vals[j] = v
        rets[i + 1] = vals.sum() / v0 - 1.0
        hold[i + 1] = [(-1 if s is None else s) for s in slots]
    idx = dates[i0 + 1: i1 + 1]
    r = pd.Series(rets[i0 + 1: i1 + 1], index=idx, name="ret")
    h = pd.DataFrame(hold[i0 + 1: i1 + 1], index=idx, columns=[f"slot{j}" for j in range(p.k)])
    h = h.apply(lambda col: col.map(lambda x: None if x < 0 else panel.symbols[x]))
    cols = ["date", "slot", "sell", "buy", "why", "sell_vs_low", "sell_vs_entry", "buy_rpos"]
    return SimResult(r, pd.DataFrame(trades, columns=cols), h)


def _weights_tranche(C, act, soon, E, pick, i0, i1, offset, rebalance_days, cost, haircut, delay):
    """Une tranche d'un portefeuille à poids : rééquilibrage aux jours i0 + offset + k·rebalance_days
    (et achat initial en i0) vers les poids ``pick(i)`` ; ``delay`` = 1 : les nouveaux poids ne
    s'appliquent qu'après le mouvement du lendemain (exécution à la clôture de J+1)."""
    n, m = C.shape
    w = np.zeros(m)
    cash = 1.0
    eq = np.full(n, np.nan)
    eq[i0] = 1.0
    for i in range(i0, i1):
        gone = (w > 0) & (soon[i] | ~act[i + 1])
        if gone.any():
            keep = np.where(act[i + 1], 1.0, 1.0 - haircut)
            cash += (w[gone] * keep[gone] * (1 - cost)).sum()
            w[gone] = 0.0
        reb = i == i0 or (i - i0 - offset) % rebalance_days == 0 and i - i0 >= offset
        tgt = None
        if reb:
            sel = pick(i)
            tot = w.sum() + cash
            tgt = np.zeros(m)
            if sel.size:
                tgt[sel] = tot / sel.size
        if tgt is not None and not delay:
            fee = cost * np.abs(tgt - w).sum()
            if tgt.sum() > 0:
                w, cash = tgt * (1 - fee / tgt.sum()), 0.0
            else:
                w, cash = tgt, w.sum() + cash - fee
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(w > 0, C[i + 1] / C[i], 1.0)
        w = w * np.nan_to_num(g, nan=1.0)
        if tgt is not None and delay:
            tot = w.sum() + cash
            sel_w = tgt / max(tgt.sum(), 1e-300) * tot if tgt.sum() > 0 else tgt
            fee = cost * np.abs(sel_w - w).sum()
            if sel_w.sum() > 0:
                w, cash = sel_w * (1 - fee / sel_w.sum()), 0.0
            else:
                w, cash = sel_w, tot - fee
        eq[i + 1] = w.sum() + cash
    return eq


def simulate_weights(panel: Panel, elig: pd.DataFrame, pick_fn, cost: float = 0.0075, rebalance_days: int = 30,
                     tranches: int = 1, delist_haircut: float = 0.0, notice_days: int = 5, execution: str = "open",
                     start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> SimResult:
    """Portefeuille équipondéré sur les pièces choisies par ``pick_fn(i, E_row, ctx)`` (indices),
    rééquilibré tous les ``rebalance_days`` jours. ``tranches`` > 1 : le capital est réparti en
    tranches rééquilibrées à des jours décalés (la date de rééquilibrage ne favorise personne)."""
    C = panel.close.to_numpy(float)
    act = panel.active.to_numpy(bool)
    E = elig.reindex_like(panel.close).fillna(False).to_numpy(bool)
    soon = delist_notice(panel, notice_days).to_numpy(bool)
    dates = panel.dates
    n = C.shape[0]
    i0 = 0 if start is None else int(dates.searchsorted(start))
    i1 = n - 1 if end is None else min(n - 1, int(dates.searchsorted(end, side="right")) - 1)
    pick = lambda i: pick_fn(i, np.flatnonzero(E[i] & act[i + 1] & ~soon[i]))  # noqa: E731
    offs = [int(round(o * rebalance_days / tranches)) for o in range(tranches)]
    eq = sum(_weights_tranche(C, act, soon, E, pick, i0, i1, o, rebalance_days, cost, delist_haircut,
                              1 if execution == "next_close" else 0) for o in offs) / len(offs)
    r = pd.Series(eq[i0 + 1: i1 + 1] / eq[i0: i1] - 1.0, index=dates[i0 + 1: i1 + 1], name="ret")
    return SimResult(r, pd.DataFrame(), pd.DataFrame())


def simulate_basket(panel: Panel, elig: pd.DataFrame, cost: float = 0.0075, rebalance_days: int = 30,
                    tranches: int = 30, delist_haircut: float = 0.0, notice_days: int = 5, execution: str = "open",
                    start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> SimResult:
    """Panier équipondéré de toutes les pièces éligibles, rééquilibré tous les ``rebalance_days``
    jours, en ``tranches`` tranches décalées (par défaut une par jour du cycle)."""
    return simulate_weights(panel, elig, lambda i, cands: cands, cost, rebalance_days, tranches,
                            delist_haircut, notice_days, execution, start, end)


def simulate_periodic(panel: Panel, elig: pd.DataFrame, k: int, every: int, rule: str = "bottom",
                      range_win: int = 60, cost: float = 0.0075, seed: int = 0, tranches: int = 1,
                      start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> SimResult:
    """Rotation systématique : tous les ``every`` jours, détenir à parts égales les ``k`` pièces
    éligibles les plus basses (``rule`` = "bottom"), les plus hautes ("top") ou prises au hasard
    ("random") dans leur fourchette de ``range_win`` jours."""
    rpos = range_position(panel.close, range_win).to_numpy(float)
    rng = np.random.default_rng(seed)

    def pick(i, cands):
        r = rpos[i, cands]
        ok = np.isfinite(r)
        cands, r = cands[ok], r[ok]
        if cands.size == 0:
            return cands
        if rule == "random":
            return rng.choice(cands, size=min(k, cands.size), replace=False)
        order = np.argsort(r if rule == "bottom" else -r, kind="stable")
        return cands[order[:k]]
    return simulate_weights(panel, elig, pick, cost, every, tranches, start=start, end=end)


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------
def perf(r: pd.Series, periods: int = 365) -> dict:
    r = r.dropna()
    if r.empty:
        return {}
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1
    years = len(r) / periods
    sd = r.std()
    return {"total_x": float(eq.iloc[-1]), "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else np.nan,
            "vol": float(sd * math.sqrt(periods)), "sharpe": float(r.mean() / sd * math.sqrt(periods)) if sd > 0 else np.nan,
            "max_dd": float(dd.min()), "days": int(len(r))}


def block_bootstrap(x: np.ndarray, stat, block: int = 30, n_boot: int = 2000, seed: int = 0) -> np.ndarray:
    """Bootstrap par blocs mobiles (circulaire) d'une série temporelle ; renvoie les statistiques."""
    x = np.asarray(x, float)
    n = x.size
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(n / block))
    out = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        out[b] = stat(x[idx])
    return out


def expected_max_sharpe(sr_trials: np.ndarray) -> float:
    """E[max] des Sharpe (par période) de N essais sans talent (Bailey & López de Prado, 2014) :
    √V[SR] · ((1 − γ) Φ⁻¹(1 − 1/N) + γ Φ⁻¹(1 − 1/(N e)))."""
    sr = np.asarray(sr_trials, float)
    sr = sr[np.isfinite(sr)]
    n = sr.size
    if n < 2:
        return 0.0
    return float(np.std(sr, ddof=1) * ((1 - EULER) * norm.ppf(1 - 1 / n) + EULER * norm.ppf(1 - 1 / (n * math.e))))


def probabilistic_sharpe(r: np.ndarray, sr0: float = 0.0) -> float:
    """PSR : P(vrai Sharpe par période > sr0), avec asymétrie et aplatissement des rendements."""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    t = r.size
    sr = r.mean() / r.std(ddof=1)
    g3, g4 = _skew(r), _kurt(r, fisher=False)
    den = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))
    return float(norm.cdf((sr - sr0) * math.sqrt(t - 1) / den))


def deflated_sharpe(r_best: np.ndarray, sr_trials: np.ndarray) -> dict:
    """DSR = PSR du meilleur essai contre le maximum attendu de N essais sans talent
    (tout en Sharpe PAR PÉRIODE, jamais annualisé)."""
    sr0 = expected_max_sharpe(sr_trials)
    return {"sr0_per_period": sr0, "dsr": probabilistic_sharpe(r_best, sr0), "n_trials": int(np.isfinite(sr_trials).sum())}


def forward_stats(panel: Panel, horizon: int) -> dict[str, pd.DataFrame]:
    """Pour chaque (jour d, pièce) : rendement de la clôture de d à la clôture de d + h, plus haut et
    plus bas atteints (clôtures) sur (d, d + h], et plus forte baisse depuis un sommet (« dd ») sur
    [d, d + h]. Une pièce retirée de la cote est figée à sa dernière clôture échangée."""
    c = panel.close.where(panel.active).ffill()     # figé à la dernière clôture échangée après un retrait
    base = c.to_numpy(float)
    stack = np.stack([c.shift(-k).to_numpy(float) for k in range(1, horizon + 1)])
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # pièces pas encore cotées : tranches vides
        ret = stack[-1] / base - 1
        mx = np.nanmax(stack, axis=0) / base - 1
        mn = np.nanmin(stack, axis=0) / base - 1
        path = np.concatenate([base[None], stack])
        peak = np.fmax.accumulate(path, axis=0)
        dd = np.nanmin(path / peak - 1, axis=0)
    # horizon incomplet en fin d'échantillon : NaN
    incomplete = np.zeros_like(base, dtype=bool)
    incomplete[-horizon:] = True
    mk = lambda a: pd.DataFrame(np.where(incomplete, np.nan, a), index=c.index, columns=c.columns)  # noqa: E731
    return {"ret": mk(ret), "max": mk(mx), "min": mk(mn), "dd": mk(dd)}


def cross_sectional_ic(signal: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame, min_n: int = 6) -> pd.Series:
    """Corrélation de Spearman, jour par jour, entre un signal et le rendement futur, sur les pièces
    éligibles."""
    out = {}
    for d in signal.index:
        m = mask.loc[d].to_numpy(bool) & np.isfinite(signal.loc[d].to_numpy(float)) & np.isfinite(fwd.loc[d].to_numpy(float))
        if m.sum() >= min_n:
            out[d] = spearmanr(signal.loc[d].to_numpy(float)[m], fwd.loc[d].to_numpy(float)[m]).statistic
    return pd.Series(out, dtype=float)


def newey_west_t(x: pd.Series, lags: int) -> float:
    """t de Student de la moyenne avec variance HAC (Newey-West, noyau de Bartlett)."""
    v = x.dropna().to_numpy(float)
    n = v.size
    if n < 3:
        return float("nan")
    e = v - v.mean()
    s = e @ e / n
    for l in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - l / (lags + 1)) * (e[l:] @ e[:-l]) / n
    return float(v.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def with_params(p: RotationParams, **kw) -> RotationParams:
    return replace(p, **kw)


def load_all(refresh: bool = False, extra: tuple[str, ...] = ("BTCUSDT", "SOLUSDT", "ETHUSDT")) -> tuple[Panel, pd.DataFrame, Panel]:
    """(panel memecoins, univers, panel de référence BTC/SOL/ETH), depuis le cache ou le réseau."""
    info = fetch_exchange_info(refresh=refresh)
    uni = meme_universe(info)
    frames = {s: fetch_daily_klines(s, refresh=refresh) for s in uni["symbol"]}
    frames = {s: f for s, f in frames.items() if len(f)}
    ref = {s: fetch_daily_klines(s, refresh=refresh) for s in extra}
    return build_panel(frames, uni.set_index("symbol")), uni, build_panel(ref)


def save_path(name: str) -> Path:
    return CACHE / name
