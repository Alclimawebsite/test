"""Arbitrages entre marchés Polymarket « Up or Down » logiquement liés (BTC 5m / 15m / 4h).

Prédire le sens d'une fenêtre à coup sûr est impossible ; en revanche, plusieurs marchés portent
sur la **même chaîne de niveaux Chainlink** et leurs issues sont logiquement liées. Un portefeuille
de jetons achetés sur ces marchés peut alors payer au moins 1 $ quelle que soit l'issue : c'est
un arbitrage si son coût (frais preneur compris) est inférieur à ce paiement minimal garanti.

Chaîne de niveaux
-----------------
Chaque marché compare deux niveaux TWAP60 Chainlink : « Up » ⇔ ``F >= K`` (égalité -> Up), où
``K`` (``priceToBeat``) est le TWAP60 à l'ouverture et ``F`` (``finalPrice``) le TWAP60 à la
clôture. Vérifié sur les données : ``F(n) == K(n+1)`` pour des fenêtres consécutives, le ``K``
d'une 5m et d'une 15m qui commencent au même instant est identique, et le ``F`` de la 3e 5m est
celui de la 15m. Pour une fenêtre 15m ``[S, S+900)`` :

``V0 = K(15m) = K(5m#1)``, ``V1 = F(5m#1) = K(5m#2)``, ``V2 = F(5m#2) = K(5m#3)``,
``V3 = F(5m#3) = F(15m)``.

Un marché est une :class:`Leg` ``(start, end)`` sur ces nœuds : Up ⇔ ``V[end] >= V[start]``. Une
4h (alignée sur l'heure de New York, 16 fenêtres 15m) se branche sur la dernière 15m de sa
fenêtre : ``Leg("4h", K4H, 3)`` avec ``K4H`` un nœud dont la valeur est connue depuis longtemps.

Méthode
-------
1. :func:`possible_outcomes` énumère les vecteurs d'issues **possibles** étant donné les niveaux
   déjà connus (ordre faible quelconque des niveaux inconnus, placés entre ou sur les niveaux
   connus) ;
2. :func:`payoff_matrix` en déduit la matrice de paiement des jetons (Up et Down de chaque marché) ;
3. :func:`solve_arbitrage` résout le programme linéaire (``scipy.optimize.linprog``, HiGHS) :
   acheter des quantités ``x >= 0`` de jetons aux ask disponibles (niveaux et tailles limités)
   pour maximiser ``paiement minimal garanti − coût`` (frais preneur ``rate·p·(1−p)`` par part
   compris). Arbitrage ⇔ optimum > 0 ;
4. pour balayer des millions d'instants, :func:`cover_vertices` énumère une fois pour toutes les
   sommets du polyèdre des portefeuilles « couvrants » ``{x >= 0 : A x >= 1}`` ; le coût minimal
   d'un paiement garanti de 1 $ au meilleur ask est alors le minimum d'un produit matriciel
   (:func:`min_cover_cost`), **exactement** égal à l'optimum du programme linéaire sans limite
   de taille (le minimum d'un coût positif sur ce polyèdre est atteint en un sommet).

Lecture seule / simulation papier : aucun ordre, aucune clé.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import pandas as pd

__all__ = [
    "FEE_RATE", "FEE_EXPONENT", "FIVE_MIN", "K4H", "Leg", "GROUP_LEGS", "GROUP_LEGS_4H", "ArbResult",
    "fee_per_share", "taker_cost", "possible_outcomes", "payoff_matrix", "token_names", "cover_vertices",
    "min_cover_cost", "solve_arbitrage", "group_state", "phase_label", "episodes", "last_trade_grid",
    "parse_slug", "chain_checks", "live_group_states",
]

FEE_RATE = 0.07            # barème crypto_fees_v2 (depuis fin mars 2026)
FEE_EXPONENT = 1.0
FIVE_MIN = 300
K4H = 4                    # nœud du niveau d'ouverture de la 4h (connu dans la dernière 15m)
_TOL = 1e-9


# ---------------------------------------------------------------------------
# Marchés et frais
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    """Marché binaire sur la chaîne : Up ⇔ ``V[end] >= V[start]``."""

    name: str
    start: int
    end: int


# groupe 15m : la 15m et ses trois 5m (nœuds 0..3 aux instants S, S+300, S+600, S+900)
GROUP_LEGS: tuple[Leg, ...] = (Leg("15m", 0, 3), Leg("5m#1", 0, 1), Leg("5m#2", 1, 2), Leg("5m#3", 2, 3))
# dernière 15m d'une 4h : la 4h compare V3 à son propre niveau d'ouverture (nœud K4H)
GROUP_LEGS_4H: tuple[Leg, ...] = GROUP_LEGS + (Leg("4h", K4H, 3),)


def fee_per_share(price, rate: float = FEE_RATE, exponent: float = FEE_EXPONENT):
    """Frais preneur par part : ``rate · (p (1 − p)) ** exponent`` (sans arrondi)."""
    p = np.asarray(price, dtype="float64")
    with np.errstate(invalid="ignore"):
        fee = rate * np.power(np.clip(p * (1.0 - p), 0.0, None), exponent)
    return float(fee) if np.ndim(fee) == 0 else fee


def taker_cost(price, rate: float = FEE_RATE, exponent: float = FEE_EXPONENT):
    """Coût d'une part achetée au prix ``price`` en preneur, frais compris."""
    p = np.asarray(price, dtype="float64")
    c = p + fee_per_share(p, rate, exponent)
    return float(c) if np.ndim(c) == 0 else c


def token_names(legs: Sequence[Leg]) -> list[str]:
    """Noms des jetons dans l'ordre des colonnes de :func:`payoff_matrix`."""
    return [f"{l.name} {side}" for l in legs for side in ("Up", "Down")]


# ---------------------------------------------------------------------------
# Issues possibles et matrice de paiement
# ---------------------------------------------------------------------------
def _rank_pattern(nodes: Sequence[int], known: Mapping[int, float]) -> tuple[tuple[int, int], ...]:
    levels = sorted({float(known[n]) for n in nodes if n in known})
    return tuple((n, levels.index(float(known[n]))) for n in nodes if n in known)


@lru_cache(maxsize=4096)
def _outcomes_cached(legs: tuple[Leg, ...], pattern: tuple[tuple[int, int], ...]) -> np.ndarray:
    nodes = sorted({n for l in legs for n in (l.start, l.end)})
    known_rank = dict(pattern)
    unknown = [n for n in nodes if n not in known_rank]
    q = (max(known_rank.values()) + 1) if known_rank else 0
    m = len(unknown)
    if m > 6:
        raise ValueError(f"trop de niveaux inconnus ({m}) pour l'énumération")
    step = m + 1                      # m emplacements distincts par intervalle : tout ordre faible
    grid = (q + 1) * step + 1
    base = {n: (r + 1) * step for n, r in known_rank.items()}
    if m:
        combos = np.array(list(itertools.product(range(grid), repeat=m)), dtype=np.int64)
    else:
        combos = np.zeros((1, 0), dtype=np.int64)
    val = {n: np.full(len(combos), v, dtype=np.int64) for n, v in base.items()}
    for k, n in enumerate(unknown):
        val[n] = combos[:, k]
    out = np.stack([val[l.end] >= val[l.start] for l in legs], axis=1)
    uniq = np.unique(out, axis=0)
    # ordre stable : tous Up d'abord
    order = np.lexsort(tuple((~uniq[:, k]).astype(int) for k in range(uniq.shape[1] - 1, -1, -1)))
    res = uniq[order]
    res.setflags(write=False)
    return res


def possible_outcomes(legs: Sequence[Leg], known: Mapping[int, float] | None = None) -> np.ndarray:
    """Vecteurs d'issues possibles (``bool``, Up = True), forme ``(n_issues, n_marchés)``.

    ``known`` : niveaux déjà connus ``{nœud: valeur}``. Les niveaux inconnus sont libres (réels
    quelconques) : on énumère tous leurs ordres faibles relatifs entre eux et par rapport aux
    niveaux connus (égalité comprise, qui va à « Up »). Seul l'ordre des niveaux connus compte,
    d'où un cache par motif de rangs.
    """
    legs = tuple(legs)
    known = dict(known or {})
    nodes = sorted({n for l in legs for n in (l.start, l.end)})
    return _outcomes_cached(legs, _rank_pattern(nodes, known))


def payoff_matrix(outcomes: np.ndarray) -> np.ndarray:
    """Paiement (0/1) de chaque jeton dans chaque issue : colonnes ``[m0 Up, m0 Down, m1 Up, …]``."""
    o = np.asarray(outcomes, dtype=bool)
    A = np.empty((o.shape[0], 2 * o.shape[1]), dtype=np.float64)
    A[:, 0::2] = o
    A[:, 1::2] = ~o
    return A


# ---------------------------------------------------------------------------
# Portefeuilles couvrants (sommets) et coût minimal vectorisé
# ---------------------------------------------------------------------------
_VERTEX_CACHE: dict[bytes, np.ndarray] = {}


def cover_vertices(payoff: np.ndarray, max_systems: int = 3_000_000) -> np.ndarray:
    """Sommets du polyèdre ``{x >= 0 : A x >= 1}`` (``A`` = matrice de paiement), forme
    ``(n_sommets, n_jetons)``.

    Un sommet a un support ``T`` et ``|T|`` contraintes ``A_ω x = 1`` actives linéairement
    indépendantes : on résout tous ces systèmes (par lots) et on garde les solutions ``> 0``
    réalisables. Lève ``ValueError`` si le nombre de systèmes dépasse ``max_systems`` (utiliser
    alors :func:`solve_arbitrage` directement).
    """
    A = np.asarray(payoff, dtype=np.float64)
    key = A.tobytes() + bytes(str(A.shape), "ascii")
    if key in _VERTEX_CACHE:
        return _VERTEX_CACHE[key]
    m, n = A.shape
    total = sum(math.comb(n, k) * math.comb(m, k) for k in range(1, n + 1))
    if total > max_systems:
        raise ValueError(f"{total} systèmes à résoudre (> {max_systems})")
    found: list[np.ndarray] = []
    for k in range(1, n + 1):
        rows = np.array(list(itertools.combinations(range(m), k)), dtype=np.int64)
        if not len(rows):
            continue
        for T in itertools.combinations(range(n), k):
            sub = A[:, T]                                  # (m, k)
            M = sub[rows]                                  # (R, k, k)
            det = np.linalg.det(M)
            ok = np.abs(det) > 1e-9
            if not ok.any():
                continue
            X = np.linalg.solve(M[ok], np.ones((int(ok.sum()), k, 1)))[..., 0]   # (R', k)
            pos = (X > 1e-9).all(axis=1)
            if not pos.any():
                continue
            X = X[pos]
            feas = (sub @ X.T >= 1.0 - 1e-9).all(axis=0)
            for x in X[feas]:
                v = np.zeros(n)
                v[list(T)] = x
                found.append(v)
    if found:
        V = np.unique(np.round(np.array(found), 9), axis=0)
    else:
        V = np.zeros((0, n))
    V.setflags(write=False)
    _VERTEX_CACHE[key] = V
    return V


def min_cover_cost(costs: np.ndarray, vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Coût minimal d'un paiement garanti de 1 $ pour chaque ligne de ``costs`` (``(N, n)``,
    coût par part frais compris, ``inf`` = jeton indisponible). Renvoie ``(coût_min, sommet)``
    (``sommet = -1`` si aucun portefeuille couvrant n'est disponible)."""
    C = np.atleast_2d(np.asarray(costs, dtype=np.float64))
    V = np.asarray(vertices, dtype=np.float64)
    N = C.shape[0]
    if V.shape[0] == 0:
        return np.full(N, np.inf), np.full(N, -1, dtype=np.int64)
    finite = np.isfinite(C)
    Cf = np.where(finite, C, 0.0)
    tot = Cf @ V.T                                         # (N, nv)
    uses_missing = (~finite).astype(np.float64) @ (V.T > 0).astype(np.float64) > 0
    tot = np.where(uses_missing, np.inf, tot)
    idx = np.argmin(tot, axis=1)
    best = tot[np.arange(N), idx]
    idx = np.where(np.isfinite(best), idx, -1)
    return best, idx


# ---------------------------------------------------------------------------
# Programme linéaire avec niveaux et tailles
# ---------------------------------------------------------------------------
@dataclass
class ArbResult:
    """Résultat de :func:`solve_arbitrage` (montants en $ ; ``shares``/``avg_price`` par jeton)."""

    profit: float = 0.0                 # paiement minimal garanti − coût (frais compris)
    payout: float = 0.0                 # paiement minimal garanti z
    cost: float = 0.0
    shares: np.ndarray = field(default_factory=lambda: np.zeros(0))
    avg_price: np.ndarray = field(default_factory=lambda: np.zeros(0))
    max_price: np.ndarray = field(default_factory=lambda: np.zeros(0))   # prix limite (pire niveau utilisé)
    status: str = "ok"

    @property
    def is_arbitrage(self) -> bool:
        return self.profit > 1e-7

    @property
    def margin(self) -> float:
        """Profit par dollar de paiement garanti (« par part » d'un lot couvrant)."""
        return self.profit / self.payout if self.payout > 0 else 0.0


def _as_levels(v, n: int, fill: float) -> list[np.ndarray]:
    out = []
    for i in range(n):
        x = v[i] if v is not None else fill
        a = np.atleast_1d(np.asarray(x, dtype=np.float64)).ravel()
        out.append(a)
    return out


def solve_arbitrage(payoff: np.ndarray, prices: Sequence, sizes: Sequence | None = None, *,
                    max_sets: float | None = None, fee_rate: float = FEE_RATE,
                    fee_exponent: float = FEE_EXPONENT) -> ArbResult:
    """Maximise ``z − Σ coût·x`` sous ``A_ω · x >= z`` pour toute issue ω, ``0 <= x <= taille``.

    ``prices[i]`` : ask du jeton ``i`` (scalaire ou tableau de niveaux croissants ; NaN =
    indisponible) ; ``sizes[i]`` : taille de chaque niveau (``inf`` par défaut) ; ``max_sets`` :
    plafond du paiement garanti ``z`` (nombre de « lots » couvrants). Sans taille ni plafond, ``z``
    est plafonné à 1 (profit = marge par lot). Coût d'une part = prix + ``fee_rate·p(1−p)``.
    """
    from scipy.optimize import linprog

    A = np.asarray(payoff, dtype=np.float64)
    m, n = A.shape
    P = _as_levels(prices, n, math.nan)
    Z = _as_levels(sizes, n, math.inf) if sizes is not None else [np.full(len(p), math.inf) for p in P]
    tok, price, cap = [], [], []
    for i in range(n):
        p, s = P[i], np.broadcast_to(Z[i], P[i].shape)
        for pj, sj in zip(p, s):
            if math.isfinite(pj) and 0.0 < pj < 1.0 and sj > 0:
                tok.append(i)
                price.append(float(pj))
                cap.append(float(sj))
    res = ArbResult(shares=np.zeros(n), avg_price=np.full(n, math.nan), max_price=np.full(n, math.nan))
    if not tok:
        res.status = "vide"
        return res
    tok_a = np.array(tok)
    price_a = np.array(price)
    cost_a = taker_cost(price_a, fee_rate, fee_exponent)
    L = len(tok)
    zcap = max_sets
    if zcap is None and not np.isfinite(cap).all():
        zcap = 1.0
    # variables [x_1..x_L, z] ; minimiser Σ c x − z
    c = np.concatenate([cost_a, [-1.0]])
    W = A[:, tok_a]                                        # (m, L)
    A_ub = np.hstack([-W, np.ones((m, 1))])
    b_ub = np.zeros(m)
    bounds = [(0.0, (None if not math.isfinite(s) else s)) for s in cap] + [(0.0, zcap)]
    sol = linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if sol.status != 0:
        res.status = f"linprog:{sol.status}"
        return res
    x = np.clip(sol.x[:L], 0.0, None)
    z = float(sol.x[L])
    profit = -float(sol.fun)
    if profit <= 1e-9 or z <= 1e-12:
        return res
    for i in range(n):
        sel = (tok_a == i) & (x > 1e-9)
        if sel.any():
            res.shares[i] = x[sel].sum()
            res.avg_price[i] = float((x[sel] * price_a[sel]).sum() / x[sel].sum())
            res.max_price[i] = float(price_a[sel].max())
    res.payout = z
    res.cost = float((x * cost_a).sum())
    res.profit = res.payout - res.cost
    return res


# ---------------------------------------------------------------------------
# État d'un groupe 15m à un instant donné
# ---------------------------------------------------------------------------
def group_state(t_rel: float, legs: Sequence[Leg], values: Mapping[int, float], *,
                known_delay_s: float = 0.0) -> tuple[tuple[int, ...], dict[int, float]]:
    """Marchés encore ouverts et niveaux connus ``t_rel`` secondes après l'ouverture S de la 15m.

    Le nœud ``k`` (``k`` = 0..3) est l'instant ``S + 300 k`` : son niveau est connu à partir de
    ``S + 300 k + known_delay_s`` ; le nœud :data:`K4H` est toujours connu. Un marché est « ouvert »
    tant que son nœud de fin n'est pas atteint (après, son issue est fixée : cas traité à part).
    """
    known = {}
    for n, v in values.items():
        if n == K4H or t_rel >= FIVE_MIN * n + known_delay_s:
            if v is not None and math.isfinite(float(v)):
                known[n] = float(v)
    active = tuple(i for i, l in enumerate(legs) if t_rel < FIVE_MIN * l.end)
    return active, known


def phase_label(t_rel: float) -> str:
    """Phase d'un groupe 15m (secondes depuis S)."""
    if t_rel < 0:
        return "avant S"
    if t_rel < 300:
        return "avant clôture #1"
    if t_rel < 600:
        return "entre #1 et #2"
    if t_rel < 900:
        return "après clôture #2"
    return "après 15m"


# ---------------------------------------------------------------------------
# Épisodes et prix issus des trades
# ---------------------------------------------------------------------------
def episodes(t: np.ndarray, flag: np.ndarray, max_gap: float, keys: np.ndarray | None = None) -> list[tuple[int, int]]:
    """Plages ``[i0, i1]`` (inclusives) d'indices consécutifs où ``flag`` est vrai, fusionnées si
    l'écart de temps est ``<= max_gap`` et (si ``keys``) si la clé ne change pas."""
    t = np.asarray(t)
    idx = np.flatnonzero(np.asarray(flag, dtype=bool))
    out: list[tuple[int, int]] = []
    if not len(idx):
        return out
    i0 = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        same = keys is None or keys[i] == keys[prev]
        if i == prev + 1 and t[i] - t[prev] <= max_gap and same:
            prev = i
            continue
        out.append((i0, prev))
        i0 = prev = i
    out.append((i0, prev))
    return out


def last_trade_grid(ts: np.ndarray, price: np.ndarray, size: np.ndarray, grid: np.ndarray,
                    max_age: float, mode: str = "last") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Prix du dernier trade (``ts`` triés croissants, en s) d'âge ``< max_age`` à chaque instant de
    ``grid``. Renvoie ``(prix, taille, âge)`` (NaN si aucun trade récent).

    * ``mode="last"`` : dernier trade dans l'ordre fourni ; taille = somme des trades de la même
      seconde au même prix jusqu'à lui (niveau consommé) ;
    * ``mode="block_max"`` : l'ordre des trades d'un même bloc (même seconde) est inconnu : on
      prend le prix **le plus élevé** payé dans la dernière seconde ayant des trades (estimation
      prudente de l'ask) et la taille échangée à ce prix dans cette seconde.
    """
    ts = np.asarray(ts, dtype=np.float64)
    grid = np.asarray(grid, dtype=np.float64)
    p_out = np.full(len(grid), np.nan)
    s_out = np.full(len(grid), np.nan)
    a_out = np.full(len(grid), np.nan)
    if not len(ts):
        return p_out, s_out, a_out
    price = np.asarray(price, dtype=np.float64)
    size = np.asarray(size, dtype=np.float64)
    if mode == "block_max":
        df = pd.DataFrame({"ts": ts, "p": price, "s": size})
        pmax = df.groupby("ts")["p"].transform("max")
        at = df[df["p"] == pmax].groupby("ts").agg(p=("p", "first"), s=("s", "sum"))
        ts, price, cum = at.index.to_numpy(dtype=np.float64), at["p"].to_numpy(), at["s"].to_numpy()
    elif mode == "last":
        # taille au niveau : somme des trades de même (ts, prix) jusqu'au dernier inclus
        key_same = np.r_[False, (ts[1:] == ts[:-1]) & (price[1:] == price[:-1])]
        grp = np.cumsum(~key_same)
        cum = pd.Series(size).groupby(grp).cumsum().to_numpy()
    else:
        raise ValueError(f"mode inconnu : {mode!r}")
    j = np.searchsorted(ts, grid, side="right") - 1
    ok = j >= 0
    jj = np.where(ok, j, 0)
    age = grid - ts[jj]
    ok &= age < max_age
    p_out[ok] = price[jj[ok]]
    s_out[ok] = cum[jj[ok]]
    a_out[ok] = age[ok]
    return p_out, s_out, a_out


# ---------------------------------------------------------------------------
# Vérification de la chaîne K/F
# ---------------------------------------------------------------------------
_DUR = {"5m": 300, "15m": 900, "4h": 14400}


def parse_slug(slug: str) -> tuple[str, str, int]:
    a, _, d, s = str(slug).split("-")
    return a, d, int(s)


def chain_checks(meta: pd.DataFrame) -> pd.DataFrame:
    """Égalités de la chaîne sur ``meta`` (colonnes ``slug``, ``price_to_beat``, ``final_price``).

    Une ligne par relation : ``n`` paires comparables, ``egal`` (égalité exacte des flottants),
    ``ecart_max`` (valeur absolue, $), ``ecart_max_pb`` (points de base).
    """
    m = meta.copy()
    parts = m["slug"].map(parse_slug)
    m["asset"] = [p[0] for p in parts]
    m["dur"] = [p[1] for p in parts]
    m["S"] = [p[2] for p in parts]
    m["E"] = m["S"] + m["dur"].map(_DUR)
    rows = []

    def cmp(name: str, a: pd.DataFrame, b: pd.DataFrame, on_a: str, on_b: str, col_a: str, col_b: str) -> None:
        j = a[["asset", on_a, col_a, "slug"]].rename(columns={on_a: "t", col_a: "va", "slug": "slug_a"}).merge(
            b[["asset", on_b, col_b, "slug"]].rename(columns={on_b: "t", col_b: "vb", "slug": "slug_b"}),
            on=["asset", "t"], how="inner")
        j = j[np.isfinite(j["va"].astype(float)) & np.isfinite(j["vb"].astype(float))]
        d = (j["va"] - j["vb"]).abs()
        rows.append({"relation": name, "n": int(len(j)), "egal": int((d == 0).sum()),
                     "taux_egal": float((d == 0).mean()) if len(j) else math.nan,
                     "ecart_max": float(d.max()) if len(j) else math.nan,
                     "ecart_max_pb": float((d / j["vb"].abs() * 1e4).max()) if len(j) else math.nan,
                     "exemple_ecart": (f"{j.loc[d.idxmax(), 'slug_a']} / {j.loc[d.idxmax(), 'slug_b']}"
                                       if len(j) and d.max() > 0 else "")})

    d5, d15, d4 = (m[m["dur"] == x] for x in ("5m", "15m", "4h"))
    cmp("K(5m#k) == F(5m#k−1) (5m consécutives)", d5, d5, "S", "E", "price_to_beat", "final_price")
    cmp("K(15m) == F(15m précédente)", d15, d15, "S", "E", "price_to_beat", "final_price")
    cmp("K(15m) == K(5m#1) (même début)", d15, d5, "S", "S", "price_to_beat", "price_to_beat")
    cmp("F(15m) == F(5m#3) (même fin)", d15, d5, "E", "E", "final_price", "final_price")
    cmp("K(5m#2) == F(5m#1) dans une 15m", d5[(d5["S"] % 900) == 300], d5[(d5["S"] % 900) == 0], "S", "E",
        "price_to_beat", "final_price")
    cmp("K(5m#3) == F(5m#2) dans une 15m", d5[(d5["S"] % 900) == 600], d5[(d5["S"] % 900) == 300], "S", "E",
        "price_to_beat", "final_price")
    if len(d4):
        cmp("K(4h) == F(4h précédente)", d4, d4, "S", "E", "price_to_beat", "final_price")
        cmp("K(4h) == K(15m) (même début)", d4, d15, "S", "S", "price_to_beat", "price_to_beat")
        cmp("F(4h) == F(15m) (même fin)", d4, d15, "E", "E", "final_price", "final_price")
        cmp("F(4h) == F(5m) (même fin)", d4, d5, "E", "E", "final_price", "final_price")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Carnet réel : états conjoints des carnets d'un groupe
# ---------------------------------------------------------------------------
def live_group_states(markets: Sequence, depth: int = 3) -> dict[str, np.ndarray]:
    """États conjoints des carnets de plusieurs marchés enregistrés (:class:`LiveMarket`).

    Les événements de tous les marchés sont fusionnés par heure de réception locale (``rx``,
    même horloge pour toutes les connexions). Après chaque événement de carnet, on note pour
    chaque jeton (Up puis Down de chaque marché, dans l'ordre de ``markets``) les ``depth``
    meilleurs asks (prix croissants) et leurs tailles : ask Up = asks du repère Up ; ask Down =
    ``1 − bid Up`` (carnet unifié). Renvoie ``rx`` (ns), ``ts`` (ms serveur), ``market`` (indice
    du marché modifié), ``px``/``sz`` (``(N, 2·M, depth)``, NaN / 0 si vide), ``fresh``
    (``(N, M)`` : au moins un instantané reçu) et ``crossed`` (``(N, M)`` : bid >= ask). Seuls les
    événements qui modifient les ``depth`` meilleurs niveaux d'un marché sont notés (``float32``).
    """
    import heapq

    from tradebot.polymarket_book import SCALE, Book, apply_event

    M = len(markets)
    streams = [[(e[0], k, e) for e in mk.events if e[2] in ("book", "pc")] for k, mk in enumerate(markets)]
    books = [Book() for _ in range(M)]
    cur_px = np.full((2 * M, depth), np.nan, dtype=np.float32)
    cur_sz = np.zeros((2 * M, depth), dtype=np.float32)
    crossed = np.zeros(M, dtype=bool)
    fresh = np.zeros(M, dtype=bool)
    rx_l, ts_l, mk_l, px_l, sz_l, fr_l, cr_l = [], [], [], [], [], [], []
    for rx, k, ev in heapq.merge(*streams, key=lambda x: x[0]):
        b = books[k]
        apply_event(b, ev)
        asks = heapq.nsmallest(depth, b.asks.items())
        bids = heapq.nlargest(depth, b.bids.items())
        npx = np.full((2, depth), np.nan, dtype=np.float32)
        nsz = np.zeros((2, depth), dtype=np.float32)
        for j, (p, s) in enumerate(asks):
            npx[0, j] = p / SCALE
            nsz[0, j] = s
        for j, (p, s) in enumerate(bids):
            npx[1, j] = (SCALE - p) / SCALE
            nsz[1, j] = s
        cr = bool(asks and bids and bids[0][0] >= asks[0][0])
        fr = b.n_snapshots > 0
        same = (np.array_equal(npx, cur_px[2 * k: 2 * k + 2], equal_nan=True)
                and np.array_equal(nsz, cur_sz[2 * k: 2 * k + 2]) and cr == crossed[k] and fr == fresh[k])
        if same and rx_l:
            continue                       # meilleurs niveaux inchangés : rien à noter
        cur_px[2 * k: 2 * k + 2] = npx
        cur_sz[2 * k: 2 * k + 2] = nsz
        crossed[k] = cr
        fresh[k] = fr
        rx_l.append(rx)
        ts_l.append(ev[1])
        mk_l.append(k)
        px_l.append(cur_px.copy())
        sz_l.append(cur_sz.copy())
        fr_l.append(fresh.copy())
        cr_l.append(crossed.copy())
    if not rx_l:
        return {"rx": np.zeros(0, dtype=np.int64), "ts": np.zeros(0, dtype=np.int64), "market": np.zeros(0, dtype=np.int64),
                "px": np.zeros((0, 2 * M, depth)), "sz": np.zeros((0, 2 * M, depth)),
                "fresh": np.zeros((0, M), dtype=bool), "crossed": np.zeros((0, M), dtype=bool)}
    return {"rx": np.asarray(rx_l, dtype=np.int64), "ts": np.asarray(ts_l, dtype=np.int64),
            "market": np.asarray(mk_l, dtype=np.int64), "px": np.asarray(px_l), "sz": np.asarray(sz_l),
            "fresh": np.asarray(fr_l, dtype=bool), "crossed": np.asarray(cr_l, dtype=bool)}
