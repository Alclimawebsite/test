"""La formule exacte de la probabilité « Up » d'un marché Polymarket Up or Down (règle TWAP-60).

Règle officielle (vérifiée sur 8 063 marchés, 100 %) : « Up » ssi F >= K, avec

    K = moyenne du prix sur [S − L, S]   (le « prix à battre », L = 60 s)
    F = moyenne du prix sur [E − L, E]   (le prix final)

S = ouverture, E = clôture, D = E − S (300 s ou 900 s). Si le log-prix suit une marche
aléatoire sans tendance de volatilité σ (par √seconde), F − K est gaussien et

    P(Up | information à t) = Φ( m(t) / s(t) )

où la moyenne m et l'écart-type s dépendent de la phase (r = S − t, τ = E − t) :

1. t <= S − L (avant que la moyenne de départ commence) :
       m = 0,  s² = σ² (D − L/3)                      -> P = 0,5 : personne ne sait rien.
2. S − L < t <= S (la moyenne de départ est en cours ; a = L − r secondes écoulées) :
       m = (a/L) · (p_t − Ā)      Ā = moyenne déjà réalisée sur [S − L, t]
       s² = σ² (D − 2L/3 + r − r²/L + r³/(3L²))
   C'est le « TWAP partiel » : si le prix actuel est au-dessus de la moyenne en cours, le
   prix à battre finira en dessous du prix actuel -> avantage à « Up ».
3. S < t <= E − L (K connu) :
       m = p_t − K,  s² = σ² (τ − 2L/3)
4. E − L < t <= E (la moyenne finale est en cours ; B = somme déjà réalisée / L) :
       m = B + (τ/L) p_t − K,  s² = σ² τ³/(3L²)

Démonstration : on écrit F − K = ∫ g(u) dW(u) (intégration par parties d'une moyenne de
mouvement brownien) avec g = 1 − (S − u)/L sur [t, S], 1 sur [S, E − L], (E − u)/L sur
[E − L, E] ; la variance vaut σ² ∫ g². Vérifié par Monte-Carlo dans
tests/test_polymarket_formula.py.

Tout est en log-prix. σ se mesure sur les hauts/bas (Parkinson), sur les rendements 1 s
(EWMA) ou avec TimesFM ; les égalités (F = K) vont à « Up » et sont négligeables.

Décision (preneur) : acheter « Up » au prix a coûte a + 0,07·a·(1 − a) par part
(frais crypto_fees_v2) ; l'espérance est P − a − 0,07·a·(1 − a). Symétrique pour « Down ».
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

TWAP_WINDOW_S = 60.0
FEE_RATE = 0.07


@dataclass(frozen=True)
class FairValue:
    prob_up: float      # P(Up) selon la formule
    mean: float         # m(t), en log-prix
    sd: float           # s(t), en log-prix
    phase: int          # 1 à 4 (voir le module)


def _variance_factor(t: float, S: float, E: float, L: float, dt: float | None) -> float:
    """Var(F − K | t) / σ².

    dt = None : formules continues du module. dt > 0 : moyennes de points échantillonnés
    toutes les dt secondes sur (S − L, S] et (E − L, E] (Chainlink publie chaque seconde) ;
    calcul exact Σ_k g_k² · dt avec g_k = poids des points futurs touchés par l'incrément k.
    Les deux coïncident quand dt -> 0 ; l'écart ne compte que dans les dernières secondes.
    """
    D = E - S
    if dt is None:
        if t <= S - L:
            return D - L / 3.0
        if t <= S:
            r = S - t
            return D - 2 * L / 3.0 + r - r * r / L + r ** 3 / (3 * L * L)
        tau = max(E - t, 0.0)
        if t <= E - L:
            return tau - 2 * L / 3.0
        return tau ** 3 / (3 * L * L)
    n = int(round(L / dt))
    start_pts = S - L + dt * np.arange(1, n + 1)
    end_pts = E - L + dt * np.arange(1, n + 1)
    steps_end = t + dt * np.arange(1, int(math.floor((E - t) / dt + 1e-9)) + 1)
    if steps_end.size == 0:
        return 0.0
    # g_k = (1/n)·#{points de fin >= u_k} − (1/n)·#{points de départ > t et >= u_k}
    g_end = (n - np.searchsorted(end_pts, steps_end - 1e-9, side="left")) / n
    start_future = start_pts[start_pts > t + 1e-9]
    g_start = (start_future.size - np.searchsorted(start_future, steps_end - 1e-9, side="left")) / n
    g = g_end - g_start
    return float(np.sum(g * g) * dt)


def twap_diff_moments(t: float, S: float, E: float, log_price: float, sigma_per_s: float, *,
                      start_avg: float | None = None, price_to_beat: float | None = None,
                      end_partial_sum: float | None = None, L: float = TWAP_WINDOW_S,
                      dt: float | None = 1.0) -> tuple[float, float, int]:
    """Moyenne et écart-type de F − K sachant l'information à t (log-prix, secondes).

    start_avg : moyenne du log-prix sur (S − L, t] (phase 2).
    price_to_beat : log(K) (phases 3 et 4).
    end_partial_sum : (1/L) · somme réalisée sur (E − L, t] (phase 4), c.-à-d.
        moyenne réalisée × (t − (E − L))/L.
    dt : pas d'échantillonnage des moyennes (1 s = Chainlink) ; None = limite continue.
    """
    D = E - S
    if D <= L:
        raise ValueError("la fenêtre doit durer plus que la moyenne de L secondes")
    if sigma_per_s < 0:
        raise ValueError("sigma doit être >= 0")
    sd = sigma_per_s * math.sqrt(max(_variance_factor(t, S, E, L, dt), 0.0))
    if t <= S - L:
        return 0.0, sd, 1
    if t <= S:
        if start_avg is None:
            raise ValueError("phase 2 : start_avg (moyenne réalisée sur (S − L, t]) requis")
        a = L - (S - t)
        return (a / L) * (log_price - start_avg), sd, 2
    if price_to_beat is None:
        raise ValueError("phases 3-4 : price_to_beat (log K) requis")
    if t <= E - L:
        return log_price - price_to_beat, sd, 3
    if end_partial_sum is None:
        raise ValueError("phase 4 : end_partial_sum requis")
    tau = max(E - t, 0.0)
    return end_partial_sum + (tau / L) * log_price - price_to_beat, sd, 4


def fair_prob_up(t: float, S: float, E: float, log_price: float, sigma_per_s: float, **kw) -> FairValue:
    """P(Up) = Φ(m/s) ; égalité (s = 0) : 1 si m >= 0, sinon 0 (les égalités vont à « Up »)."""
    mean, sd, phase = twap_diff_moments(t, S, E, log_price, sigma_per_s, **kw)
    if sd <= 0:
        p = 1.0 if mean >= 0 else 0.0
    else:
        p = float(norm.cdf(mean / sd))
    return FairValue(prob_up=p, mean=mean, sd=sd, phase=phase)


def parkinson_sigma_per_s(high: np.ndarray, low: np.ndarray, bar_seconds: float = 60.0) -> float:
    """σ par √seconde à partir des hauts/bas de bougies (Parkinson 1980) :
    σ²_barre = (ln(H/L))² / (4 ln 2), moyenne sur les bougies fournies."""
    h, lo = np.asarray(high, float), np.asarray(low, float)
    ok = (h > 0) & (lo > 0) & np.isfinite(h) & np.isfinite(lo)
    if not ok.any():
        return float("nan")
    var_bar = np.mean(np.log(h[ok] / lo[ok]) ** 2) / (4 * math.log(2))
    return math.sqrt(var_bar / bar_seconds)


def ewma_sigma_per_s(log_prices: np.ndarray, dt_seconds: float = 1.0, halflife_s: float = 600.0) -> float:
    """σ par √seconde : EWMA des carrés des rendements (pas dt), demi-vie en secondes."""
    x = np.asarray(log_prices, float)
    r = np.diff(x)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan")
    lam = 0.5 ** (dt_seconds / halflife_s)
    w = lam ** np.arange(r.size - 1, -1, -1)
    return math.sqrt(float(np.sum(w * r * r) / np.sum(w)) / dt_seconds)


def taker_cost(ask: float | np.ndarray, fee_rate: float = FEE_RATE) -> float | np.ndarray:
    """Coût d'une part achetée au prix ask, frais preneur inclus."""
    return ask + fee_rate * ask * (1 - ask)


def taker_edge(prob_up: float, ask_up: float, ask_down: float, fee_rate: float = FEE_RATE) -> dict:
    """Espérance par part d'un achat preneur de chaque côté, et le côté à acheter (ou aucun)."""
    ev_up = prob_up - taker_cost(ask_up, fee_rate)
    ev_down = (1 - prob_up) - taker_cost(ask_down, fee_rate)
    side = None
    if max(ev_up, ev_down) > 0:
        side = "up" if ev_up >= ev_down else "down"
    return {"ev_up": ev_up, "ev_down": ev_down, "side": side}
