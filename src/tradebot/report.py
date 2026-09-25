"""Rapports : tableaux CSV, Markdown (sans ``tabulate``) et graphiques PNG sous ``reports/``.

Chaque étude écrit dans son dossier ``reports/<étude>/`` (``config.REPORTS_DIR``) :
``etat_des_lieux`` pour ``tradebot study``, ``timesfm`` pour ``tradebot timesfm-backtest``.
Ce module ne charge aucune donnée et ne fait aucun calcul statistique lourd : il met en
forme ce que produisent ``evaluation.py`` et ``backtest.py`` (le CLI orchestre).

Style commun des graphiques
---------------------------
* fond ``#fcfcfb``, texte ``#0b0b0b`` (titres) / ``#52514e`` (secondaire), grille ``#e4e3de`` ;
* **une seule échelle Y** par graphique (petits multiples à axe Y partagé si besoin) ;
* un **titre court qui dit la conclusion** (calculée à partir des données), un sous-titre qui
  dit ce qui est mesuré ;
* IC : palette **divergente** bleu ``#2a78d6`` (IC < 0, retour à la moyenne) — gris neutre
  ``#f0efec`` (0) — orange ``#eb6834`` (IC > 0, continuation), échelle symétrique ;
* modèles : palette catégorielle à **ordre fixe** (chaque modèle garde sa couleur), validée
  pour le daltonisme sur les paires adjacentes ; les tableaux Markdown servent de vue
  accessible (couleurs de faible contraste : aqua, jaune, magenta) ;
* barres fines partant de zéro ; « accuracy − 50 % » en points plutôt qu'une accuracy sur un
  axe tronqué ; bande grise = bruit d'échantillonnage (± 1,96 × 0,5 / √n_eff).

Nombres en français dans le Markdown (virgule décimale, espace fine insécable pour les
milliers) ; les CSV gardent le format machine (point décimal, pleine précision).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from .config import REPORTS_DIR

__all__ = [
    "BG", "TEXT", "TEXT_2", "GRID", "BLUE", "ORANGE", "NEUTRAL", "MODEL_COLORS",
    "COLUMN_DOCS",
    "report_dir", "write_csv", "write_text", "write_json", "fmt_number", "to_markdown",
    "columns_doc_table", "family_summary", "top_bottom", "ic_title", "verdict_vs_baselines",
    "plot_ic_heatmap", "plot_family_bars", "plot_excess_accuracy",
]

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

BG = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3de"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
NEUTRAL = "#f0efec"
NOISE = "#e9e8e3"          # bande de bruit (derrière les barres)

# Couleur fixe par modèle (la couleur suit l'entité, jamais son rang). Les deux ordres
# utilisés ensemble ont été validés (scripts/validate_palette.js du skill dataviz, mode clair) :
# [timesfm, reversal_h, momentum_h, always_up, majority_prev_day] et
# [logit, hgb, reversal_h, momentum_h, always_up, majority_prev_day].
MODEL_COLORS: dict[str, str] = {
    "timesfm": BLUE,
    "timesfm_cal": BLUE,
    "logit": BLUE,
    "hgb": "#4a3aa7",
    "reversal_h": ORANGE,
    "momentum_h": "#1baf7a",
    "always_up": "#eda100",
    "majority_prev_day": "#e87ba4",
}
_FALLBACK_COLORS = ("#008300", "#e34948")

MODEL_LABELS: dict[str, str] = {
    "timesfm": "TimesFM",
    "timesfm_cal": "TimesFM recalibré",
    "logit": "logit (combiné)",
    "hgb": "gradient boosting (combiné)",
    "reversal_h": "retournement h",
    "momentum_h": "momentum h",
    "always_up": "toujours hausse",
    "majority_prev_day": "majorité de la veille",
}

# ---------------------------------------------------------------------------
# Dictionnaire des colonnes (README générés)
# ---------------------------------------------------------------------------

COLUMN_DOCS: dict[str, str] = {
    # --- scores par actif (evaluation.indicator_scores) --------------------------------
    "ticker": "Actif (paire Binance contre USDT).",
    "indicator": "Nom de l'indicateur (voir `catalogue_indicateurs.csv` pour sa description).",
    "horizon": "Horizon h en barres de 1 minute : on prévoit le signe de log(close[t+h] / close[t]).",
    "n": "Nombre de barres où l'indicateur et le rendement futur sont tous deux définis.",
    "ic_spearman": "IC (coefficient d'information) : corrélation de Spearman entre l'indicateur en t "
                   "et le rendement futur à h barres, sur toute la période. Négatif = l'indicateur "
                   "annonce un retournement ; positif = une continuation. |IC| de 0,05 est déjà « fort » "
                   "à 1 minute.",
    "ic_daily_mean": "Moyenne des IC calculés jour par jour (jours UTC d'au moins 100 barres).",
    "ic_daily_t": "t de Student des IC journaliers : moyenne / (écart type / √nombre de jours). "
                  "Robuste au chevauchement des cibles à l'intérieur d'une journée.",
    "n_days": "Nombre de jours retenus pour l'IC journalier.",
    "frac_months_same_sign": "Part des mois dont l'IC journalier moyen a le même signe que l'IC global "
                             "(stabilité ; viser ≥ 0,7).",
    "ic_nw_t": "t de l'IC corrigé de Newey-West (retards = h) : tient compte du chevauchement des cibles.",
    "ic_nw_t_2h": "Idem avec 2h retards (plus prudent, recommandé par la méthodologie).",
    "p_value": "p-valeur bilatérale de `ic_nw_t`.",
    "t_cons": "t le plus prudent parmi `ic_nw_t`, `ic_nw_t_2h` et `ic_daily_t` (0 si leurs signes "
              "divergent). C'est celui qu'il faut regarder ; viser |t| > 3.",
    "p_value_cons": "p-valeur bilatérale de `t_cons`.",
    "ic_train": "IC sur la partie apprentissage (les `train_frac` premiers pourcents de la période, "
                "moins h barres de purge).",
    "ic_test": "IC sur la partie test (après la coupure) : l'IC d'apprentissage se maintient-il ?",
    "train_sign": "Signe de `ic_train` (+1 / −1) : sens dans lequel on utilise l'indicateur sur le test.",
    "median_train": "Médiane de l'indicateur sur l'apprentissage (seuil de décision sur le test).",
    "hit_rate_oos": "Hors échantillon (test) : part des barres où « hausse si (x − médiane) × signe > 0 » "
                    "donne la bonne direction. 0,5 = hasard.",
    "auc_oos": "AUC hors échantillon de signe × indicateur contre la direction réalisée (0,5 = hasard).",
    "up_rate_oos": "Part de hausses sur le test (taux de base).",
    "n_oos": "Barres de test dont la direction est définie (hors égalités et trous).",
    "n_eff_oos": "Taille effective du test : n_oos / h (les cibles de barres voisines se chevauchent).",
    "p_hit": "p-valeur binomiale unilatérale de `hit_rate_oos` > 0,5 sur n_eff_oos essais (prudente).",
    # --- agrégat entre actifs (evaluation.aggregate_scores) ----------------------------
    "family": "Famille de l'indicateur (tendance, momentum, volatilite, volume, flux, microstructure, "
              "statistique, calendrier, marche, derives).",
    "n_tickers": "Nombre d'actifs où l'IC est défini.",
    "ic_mean": "IC moyen entre actifs (critère de classement).",
    "ic_std": "Écart type de l'IC entre actifs.",
    "ic_t_cross": "t inter-actifs : ic_mean / (ic_std / √n_tickers) (défini dès 2 actifs, peu de degrés "
                  "de liberté).",
    "frac_same_sign": "Part des actifs dont l'IC a le signe de `ic_mean` (viser au moins 4 actifs sur 6).",
    "ic_daily_t_mean": "Moyenne entre actifs du t journalier.",
    "ic_train_mean": "IC moyen sur les parties apprentissage seulement : c'est lui qui sert à choisir "
                     "des covariables TimesFM sans regarder le test.",
    "ic_test_mean": "IC moyen sur les parties test.",
    "hit_rate_oos_mean": "Hit-rate hors échantillon moyen entre actifs.",
    "auc_oos_mean": "AUC hors échantillon moyenne entre actifs.",
    "z_combined": "Z de Stouffer : somme des t prudents (`t_cons`) des actifs / √nombre d'actifs.",
    "p_combined": "p-valeur bilatérale de `z_combined` (optimiste : les cryptos sont corrélées entre elles).",
    "q_value": "q-valeur de Benjamini-Hochberg sur toutes les lignes (indicateurs × horizons) : "
               "taux de fausses découvertes attendu si l'on retient cette ligne et les meilleures.",
    "q_value_by": "q-valeur de Benjamini-Yekutieli : valable même si les tests sont dépendants "
                  "(indicateurs très corrélés). Plus sévère ; à privilégier.",
    # --- synthèse par famille (report.family_summary) ----------------------------------
    "n_indicators": "Nombre d'indicateurs de la famille présents dans l'agrégat.",
    "n_pairs": "Nombre de couples (indicateur, horizon).",
    "median_abs_ic": "Médiane de |ic_mean| sur les couples de la famille.",
    "max_abs_ic": "Plus grand |ic_mean| de la famille.",
    "best_indicator": "Indicateur qui atteint `max_abs_ic`.",
    "best_horizon": "Horizon de ce meilleur couple.",
    "best_ic": "ic_mean (signé) de ce meilleur couple.",
    "frac_q_by_05": "Part des couples de la famille avec q_value_by < 0,05.",
    "mean_hit_rate_oos": "Moyenne de `hit_rate_oos_mean` sur la famille.",
    "mean_auc_oos": "Moyenne de `auc_oos_mean` sur la famille.",
    # --- modèles combinés (evaluation.direction_summary) --------------------------------
    "model": "Modèle : `logit` et `hgb` (gradient boosting) combinent tous les indicateurs en "
             "walk-forward purgé ; `reversal_h`, `momentum_h`, `always_up`, `majority_prev_day` "
             "sont les baselines naïves évaluées sur exactement les mêmes barres ; `timesfm` = "
             "TimesFM zero-shot, `timesfm_cal` = sa probabilité recalibrée (isotonique).",
    "n_eff": "Taille effective : n divisé par le chevauchement des cibles (ceil(h / pas entre "
             "origines)).",
    "accuracy": "Part des directions correctement prévues (hausse prévue si P(hausse) > 0,5).",
    "balanced_accuracy": "Moyenne des taux de réussite sur les hausses et sur les baisses (insensible "
                         "au taux de base).",
    "auc": "AUC de la probabilité de hausse (0,5 = aucun pouvoir de classement).",
    "brier": "Score de Brier : moyenne de (P(hausse) − y)². 0,25 = toujours 0,5 ; plus bas = mieux.",
    "log_loss": "Log-loss (entropie croisée). ln 2 ≈ 0,693 = toujours 0,5 ; plus bas = mieux.",
    "bss": "Brier skill score contre la climatologie hors échantillon (taux de hausse du test) : "
           "> 0 = mieux que la climatologie.",
    "acc_w": "Accuracy pondérée par |rendement| : c'est elle qui compte pour gagner de l'argent.",
    "up_rate": "Part de hausses réalisées.",
    "p_binom": "p-valeur binomiale unilatérale (accuracy > 0,5) sur n_eff essais.",
    # --- métriques TimesFM (backtest.direction_metrics) ---------------------------------
    "sample": "Échantillon : `toutes` les origines, ou `2e moitié` (recalibration apprise sur la 1re).",
    "h_eff": "Chevauchement des cibles en nombre d'origines : ceil(h / stride).",
    "pt_stat": "Statistique de Pesaran-Timmermann (1992) : la direction prévue est-elle indépendante "
               "de la direction réalisée ? > 1,64 ≈ significatif à 5 % (unilatéral, sans correction).",
    "pt_pvalue": "p-valeur unilatérale de `pt_stat`.",
    "base_rate": "Part de hausses réalisées (taux de base).",
    "excess_accuracy": "accuracy − max(taux de base, 1 − taux de base) : gain sur la meilleure "
                       "prévision constante.",
    "pt_hac_t": "PT robuste à l'autocorrélation : t Newey-West (retards 2 × h_eff) de la régression "
                "y = a + b × prévision. Viser t > 3.",
    "pt_hac_pvalue": "p-valeur unilatérale de `pt_hac_t`.",
    "dm_brier_vs_reversal_t": "Diebold-Mariano sur le Brier contre `reversal_h` (t Newey-West) : "
                              "t < −2 = le modèle fait mieux que le retournement.",
    # --- diagnostics TimesFM -------------------------------------------------------------
    "coverage_q10_q90": "Part des rendements réalisés dans l'intervalle prévu [q10, q90] (cible 0,80).",
    "rho_past_60": "Corrélation de Spearman entre P(hausse) et le rendement des 60 dernières minutes "
                   "(fortement négative = TimesFM agit comme un signal de retour à la moyenne).",
    "ic_p_up": "IC de Spearman entre P(hausse) et le rendement réalisé.",
    "ic_partial": "Même IC après neutralisation (sur les rangs) du rendement des 60 dernières minutes "
                  "et des h dernières barres : ce que TimesFM apporte au-delà du retournement.",
    "median_abs_pred_bps": "|médiane prévue − dernier prix| médian, en points de base.",
    "median_abs_ret_bps": "|rendement réalisé| médian, en points de base.",
    "frac_pred_gt_cost": "Part des origines où |médiane prévue| dépasse le coût aller-retour.",
    # --- mini-stratégie (backtest.toy_strategy) ----------------------------------------
    "signal": "Probabilité utilisée pour trader (`timesfm`, `timesfm_cal`, ou `reversal_h` en référence).",
    "h": "Horizon (durée de détention en barres).",
    "threshold": "Seuil : long si P(hausse) ≥ seuil, short si ≤ 1 − seuil.",
    "cost_bps": "Coût aller-retour déduit de chaque trade (points de base ; 10 = futures taker).",
    "n_signals": "Origines où le seuil est franchi.",
    "n_trades": "Trades effectivement ouverts (non chevauchants : un seul à la fois).",
    "n_long": "Trades acheteurs.",
    "n_short": "Trades vendeurs.",
    "hit_rate": "Part des trades gagnants avant frais.",
    "gross_bps_mean": "Gain brut moyen par trade (pb).",
    "net_bps_mean": "Gain net moyen par trade, frais déduits (pb).",
    "net_bps_se": "Erreur type de `net_bps_mean` (pb) : IC à 95 % ≈ ± 2 × se.",
    "gross_bps_sum": "Gain brut cumulé (pb).",
    "net_bps_sum": "Gain net cumulé (pb).",
    # --- prévisions TimesFM ------------------------------------------------------------
    "time": "Origine t : horodatage d'ouverture de la dernière barre connue (sa clôture est le "
            "dernier prix du contexte).",
    "last_close": "close[t], dernier prix du contexte.",
    "pred_median": "Médiane prévue du close à t + h (en prix).",
    "p_up": "P(close[t+h] > close[t]) déduite des déciles TimesFM (brute, non recalibrée).",
    "p_up_cal": "P(hausse) recalibrée (isotonique apprise sur la 1re moitié des origines) ; "
                "vide sur la 1re moitié.",
    "ret": "Rendement réalisé log(close[t+h] / close[t]) (vide si trou de données).",
    "y_true": "Direction réalisée : 1 hausse, 0 baisse, vide si égalité.",
    "pred_ret": "Médiane prévue en log-rendement.",
    "q10_ret": "Décile 10 % prévu en log-rendement.",
    "q90_ret": "Décile 90 % prévu en log-rendement.",
    "past_ret_60": "Rendement des 60 minutes précédant t (log).",
    "reversal_h": "Baseline « retournement » : 0,52 si le rendement des h dernières barres est "
                  "négatif (on parie contre le mouvement), 0,48 s'il est positif, 0,5 sinon. "
                  "C'est le vrai rival à 5–15 min.",
    "momentum_h": "Baseline « momentum » : l'inverse du retournement (0,52 après une hausse).",
    "always_up": "Baseline « toujours hausse » (0,52) : mesure l'avantage dû au seul taux de base.",
    "majority_prev_day": "Baseline « majorité de la veille » : direction majoritaire des labels à "
                         "h barres du jour UTC précédent (connue à minuit : causale).",
    # --- données (donnees.csv) ----------------------------------------------------------
    "n_bars": "Barres de 1 minute chargées (clôturées).",
    "start": "Première barre (ouverture, UTC).",
    "end": "Dernière barre (ouverture, UTC).",
    "missing_bars": "Barres absentes de la grille régulière (pannes de l'exchange) : jamais "
                    "inventées ; les cibles qui les traversent valent NaN.",
    "split_time": "Première barre de la partie test (coupure à `train_frac`).",
    "n_skipped": "Indicateurs ignorés faute de colonnes (ex. force relative pour BTC lui-même).",
    "benchmark": "Référence de marché pour la force relative (BTCUSDT pour les altcoins).",
    "deriv_coverage": "Part des barres avec un open interest disponible (dérivés futures).",
    # --- plis des modèles combinés (combined_folds.csv) --------------------------------
    "fold": "Numéro du pli du walk-forward (0 = le plus ancien).",
    "n_train": "Lignes d'apprentissage du pli (après purge et éclaircissement).",
    "train_start": "Première ligne d'apprentissage.",
    "train_end": "Dernière ligne d'apprentissage (au moins h barres avant le test : purge).",
    "train_end_row": "Position de `train_end` dans les données.",
    "test_start": "Première ligne de test du pli.",
    "test_end": "Dernière ligne de test du pli.",
    "test_start_row": "Position de `test_start` dans les données.",
    # --- temps (timings.csv) ----------------------------------------------------------
    "step": "Étape chronométrée.",
    "seconds": "Durée de l'étape en secondes (horloge murale, machine partagée : ordre de grandeur).",
    # --- runs TimesFM (runs.csv) --------------------------------------------------------
    "run": "Identifiant du run : actif_backend_covariables_pas_contexte_transformation.",
    "generated_at": "Date du run (UTC).",
    "command": "Commande exacte.",
    "backend": "`timesfm3` (licence non commerciale) ou `timesfm2p5` (Apache-2.0).",
    "covariates": "Mode de covariables : `none`, `topK` (K meilleurs |ic_train_mean|), `all`.",
    "covariate_names": "Indicateurs passés en covariables past-only (séparés par `;`).",
    "stride": "Pas entre origines, en barres (grille alignée sur l'horloge UTC).",
    "context_len": "Longueur du contexte TimesFM, en barres.",
    "transform": "Espace du contexte : `logprice`, `price` ou `cumret` (toujours centré en float64).",
    "symmetric": "Moyenne symétrique (prévision exactement antisymétrique : pas de biais haussier).",
    "max_origins": "Plafond du nombre d'origines (réparties régulièrement sur la période).",
    "n_candidates": "Origines possibles sur la période avant plafond.",
    "n_excluded_gaps": "Origines écartées car leur contexte contient un trou de plus de 3 barres.",
    "n_origins": "Origines effectivement prévues.",
    "first_origin": "Première origine.",
    "last_origin": "Dernière origine.",
    "test_part_start": "Début de la partie test de l'actif (coupure à `train_frac`).",
    "days": "Jours d'historique chargés.",
    "train_frac": "Part d'apprentissage : les origines sont prises après.",
    "walk_forward_s": "Durée du walk-forward TimesFM (s).",
    "origins_per_s": "Débit : origines prévues par seconde (CPU, 3 horizons en un appel).",
}

# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------


def report_dir(study: str, root: Path | str | None = None) -> Path:
    """``<root>/<study>`` (root = ``config.REPORTS_DIR`` par défaut), créé si besoin."""
    path = Path(root if root is not None else REPORTS_DIR) / study
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_csv(df: pd.DataFrame, path: Path | str, index: bool = False) -> Path:
    """CSV UTF-8, point décimal, pleine précision (lisible par pandas et un tableur)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=index)
    return path


def write_text(path: Path | str, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _json_default(o):
    if isinstance(o, (pd.Timestamp, pd.Timedelta)):
        return str(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.ndarray, tuple, set)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    return str(o)


def _finite_or_none(obj):
    """Remplace récursivement les flottants non finis par None (JSON strict)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Mapping):
        return {k: _finite_or_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_or_none(v) for v in obj]
    return obj


def write_json(path: Path | str, obj) -> Path:
    """JSON UTF-8 indenté ; NaN/inf -> null, horodatages et types numpy convertis."""
    return write_text(path, json.dumps(_finite_or_none(obj), indent=2, ensure_ascii=False,
                                       default=_json_default, allow_nan=False))


# ---------------------------------------------------------------------------
# Markdown sans tabulate
# ---------------------------------------------------------------------------

_NNBSP = "\u202f"   # espace fine insécable (séparateur de milliers)


def fmt_number(v, decimals: int = 3, *, pct: bool = False, signed: bool = False,
               na: str = "—") -> str:
    """Nombre au format français : ``0,052``, ``-0,052``, ``12 345`` ; ``pct`` : ``52,1 %``."""
    if v is None:
        return na
    if isinstance(v, (bool, np.bool_)):
        return "oui" if v else "non"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(x):
        return na
    if pct:
        x *= 100.0
    s = f"{x:+,.{decimals}f}" if signed else f"{x:,.{decimals}f}"
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", _NNBSP)
    if s.startswith("-"):
        s = "−" + s[1:]
    return s + (" %" if pct else "")


def _fmt_cell(v, spec, na: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return na
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return na
        return v.strftime("%Y-%m-%d %H:%M") + (" UTC" if v.tzinfo is not None else "")
    if callable(spec):
        return str(spec(v))
    if isinstance(v, (bool, np.bool_)):
        return "oui" if v else "non"
    if isinstance(v, (int, np.integer)):
        return fmt_number(v, 0, na=na)
    if isinstance(v, (float, np.floating)):
        if spec is None:
            spec = 3
        if isinstance(spec, str):
            if spec.endswith("%"):
                return fmt_number(v, int(spec[:-1] or 1), pct=True, na=na)
            if spec.startswith("+"):
                return fmt_number(v, int(spec[1:] or 3), signed=True, na=na)
            return fmt_number(v, int(spec), na=na)
        return fmt_number(v, int(spec), na=na)
    try:
        if pd.isna(v):
            return na
    except (TypeError, ValueError):
        pass
    return str(v)


def to_markdown(df: pd.DataFrame, formats: Mapping[str, object] | int | None = None, *,
                index: bool = False, na: str = "—", max_rows: int | None = None,
                code_columns: Iterable[str] = ()) -> str:
    """Tableau Markdown (GitHub) sans dépendance à ``tabulate``.

    ``formats`` : nombre de décimales commun, ou dict colonne -> spécification : un entier
    (décimales), ``"1%"`` (pourcentage à 1 décimale), ``"+3"`` (signe forcé, 3 décimales)
    ou une fonction ``valeur -> str``. Entiers avec séparateur de milliers, NaN -> ``na``,
    horodatages ``AAAA-MM-JJ HH:MM UTC``. ``code_columns`` : colonnes affichées en `code`.
    Les ``|`` des cellules sont échappés.
    """
    d = df.reset_index() if index else df
    if max_rows is not None:
        d = d.head(max_rows)
    cols = [str(c) for c in d.columns]
    code = set(code_columns)
    if isinstance(formats, int):
        default, per_col = formats, {}
    else:
        default, per_col = None, dict(formats or {})
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for row in d.itertuples(index=False, name=None):
        cells = []
        for c, v in zip(cols, row):
            s = _fmt_cell(v, per_col.get(c, default), na)
            if c in code and s != na:
                s = f"`{s}`"
            cells.append(s.replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def columns_doc_table(columns: Iterable[str], docs: Mapping[str, str] = COLUMN_DOCS) -> str:
    """Tableau Markdown « colonne | signification » pour les colonnes données."""
    lines = ["| colonne | signification |", "|---|---|"]
    for c in columns:
        doc = docs.get(c, "—").replace("|", "\\|")
        lines.append(f"| `{c}` | {doc} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Synthèses
# ---------------------------------------------------------------------------

FAMILY_COLUMNS: tuple[str, ...] = (
    "family", "n_indicators", "n_pairs", "median_abs_ic", "max_abs_ic", "best_indicator",
    "best_horizon", "best_ic", "frac_q_by_05", "mean_hit_rate_oos", "mean_auc_oos",
)


def family_summary(agg: pd.DataFrame) -> pd.DataFrame:
    """Synthèse par famille d'indicateurs (colonnes :data:`FAMILY_COLUMNS`), triée par
    ``max_abs_ic`` décroissant. ``agg`` : sortie de ``evaluation.aggregate_scores``."""
    d = agg[np.isfinite(pd.to_numeric(agg["ic_mean"], errors="coerce"))].copy()
    if d.empty:
        return pd.DataFrame(columns=list(FAMILY_COLUMNS))
    d["family"] = d["family"].fillna("inconnue")
    d["abs_ic"] = d["ic_mean"].abs()
    qby = pd.to_numeric(d.get("q_value_by", pd.Series(np.nan, index=d.index)), errors="coerce")
    d["_sig"] = (qby < 0.05).astype(float)
    rows = []
    for fam, g in d.groupby("family", sort=False):
        best = g.loc[g["abs_ic"].idxmax()]
        rows.append(dict(
            family=fam, n_indicators=int(g["indicator"].nunique()), n_pairs=int(len(g)),
            median_abs_ic=float(g["abs_ic"].median()), max_abs_ic=float(best["abs_ic"]),
            best_indicator=best["indicator"], best_horizon=int(best["horizon"]),
            best_ic=float(best["ic_mean"]), frac_q_by_05=float(g["_sig"].mean()),
            mean_hit_rate_oos=float(pd.to_numeric(g["hit_rate_oos_mean"], errors="coerce").mean()),
            mean_auc_oos=float(pd.to_numeric(g["auc_oos_mean"], errors="coerce").mean())))
    out = pd.DataFrame(rows, columns=list(FAMILY_COLUMNS))
    return out.sort_values("max_abs_ic", ascending=False, kind="stable").reset_index(drop=True)


def top_bottom(agg: pd.DataFrame, k: int = 20) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(les ``k`` couples (indicateur, horizon) de plus grand |ic_mean|, les ``k`` de plus petit).

    Le « bas » ne garde que les couples mesurés sur le nombre maximal d'actifs (sinon un
    indicateur absent de la plupart des actifs y figurerait par manque de données).
    """
    d = agg[np.isfinite(pd.to_numeric(agg["ic_mean"], errors="coerce"))].copy()
    d["_abs"] = d["ic_mean"].abs()
    top = d.sort_values("_abs", ascending=False, kind="stable").head(k)
    full = d[d["n_tickers"] == d["n_tickers"].max()] if len(d) else d
    bottom = full.sort_values("_abs", ascending=True, kind="stable").head(k)
    return (top.drop(columns="_abs").reset_index(drop=True),
            bottom.drop(columns="_abs").reset_index(drop=True))


def ic_title(ic_values: Sequence[float]) -> str:
    """Titre-conclusion d'un ensemble d'IC signés (les plus forts) : sens dominant et force."""
    v = np.asarray([x for x in ic_values if x is not None and np.isfinite(x)], dtype=float)
    if v.size == 0:
        return "Aucun IC calculable"
    neg = float(np.mean(v < 0))
    if neg >= 0.7:
        kind = "Retournement dominant"
    elif neg <= 0.3:
        kind = "Continuation dominante"
    else:
        kind = "Continuation et retournement mêlés"
    mx = float(np.max(np.abs(v)))
    force = "très faible" if mx < 0.02 else "faible" if mx < 0.1 else "notable"
    return f"{kind}, |IC| max {fmt_number(mx, 3)} : signal {force}"


def verdict_vs_baselines(metrics: pd.DataFrame, models: Sequence[str], baselines: Sequence[str],
                         label: str, *, model_col: str = "model",
                         facet_col: str | None = None) -> str:
    """Titre-conclusion : le meilleur de ``models`` bat-il la meilleure baseline ?

    Par (facette, horizon) : écart d'accuracy entre le meilleur modèle et la meilleure
    baseline, jugé contre le bruit d'une différence de proportions
    ``1,96 × √2 × 0,5 / √n_eff`` (ordre de grandeur, pas un test formel).
    """
    d = metrics[np.isfinite(pd.to_numeric(metrics["accuracy"], errors="coerce"))]
    keys = ["horizon"] if facet_col is None else [facet_col, "horizon"]
    beats, worse, n_effs, cells = [], [], [], 0
    for key, g in d.groupby(keys, sort=True):
        gm = g[g[model_col].isin(models)]
        gb = g[g[model_col].isin(baselines)]
        if gm.empty or gb.empty:
            continue
        cells += 1
        am, ab = float(gm["accuracy"].max()), float(gb["accuracy"].max())
        ne = float(pd.to_numeric(gm["n_eff"], errors="coerce").min())
        n_effs.append(ne)
        noise = 1.96 * math.sqrt(2.0) * 0.5 / math.sqrt(max(ne, 1.0))
        h = key[-1] if isinstance(key, tuple) else key
        if am - ab > noise:
            beats.append(int(h))
        elif ab - am > noise:
            worse.append(int(h))
    if cells == 0:
        return f"{label} : rien à comparer"
    hs = lambda xs: "/".join(str(x) for x in sorted(set(xs)))  # noqa: E731
    if beats and not worse and len(beats) == cells:
        return f"{label} bat les baselines partout"
    if beats and not worse:
        return f"{label} bat les baselines à h = {hs(beats)} seulement"
    if worse and not beats:
        return f"{label} fait moins bien qu'une baseline à h = {hs(worse)}"
    if beats and worse:
        return f"{label} : mieux à h = {hs(beats)}, moins bien à h = {hs(worse)}"
    ne = int(round(float(np.median(n_effs)))) if n_effs else 0
    return f"{label} ≈ baselines : écarts dans le bruit (n_eff ≈ {fmt_number(ne, 0)})"


# ---------------------------------------------------------------------------
# Graphiques
# ---------------------------------------------------------------------------


def _pyplot():
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    return plt


def _style_axes(ax, *, ygrid: bool = True, xgrid: bool = False) -> None:
    ax.set_facecolor(BG)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=9, length=0)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    if xgrid:
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


_TITLE_PT, _SUB_PT = 13.0, 9.5
_PAD_IN = 0.14


def _wrap(text: str, width_in: float, size_pt: float, bold: bool = False) -> str:
    """Coupe ``text`` en lignes tenant dans ``width_in`` pouces (largeur moyenne d'un caractère
    ≈ 0,55 em, 0,6 em en gras)."""
    import textwrap

    chars = max(20, int(width_in * 72.0 / (size_pt * (0.6 if bold else 0.53))))
    # typographie française : pas de coupure avant « : » / « ; » ni après « ≈ » / « ± »
    for a, b in ((" :", "\u00a0:"), (" ;", "\u00a0;"), ("≈ ", "≈\u00a0"), ("± ", "±\u00a0")):
        text = text.replace(a, b)
    return textwrap.fill(text, chars, break_long_words=False)


def _header(width_in: float, title: str, subtitle: str | None) -> tuple[str, str | None, float]:
    """(titre coupé, sous-titre coupé, hauteur de l'en-tête en pouces)."""
    t = _wrap(title, width_in - 2 * _PAD_IN, _TITLE_PT, bold=True)
    h = _PAD_IN + (t.count("\n") + 1) * _TITLE_PT * 1.3 / 72.0
    s = None
    if subtitle:
        s = _wrap(subtitle, width_in - 2 * _PAD_IN, _SUB_PT)
        h += 0.06 + (s.count("\n") + 1) * _SUB_PT * 1.35 / 72.0
    return t, s, h + 0.08


def _draw_header(fig, title: str, subtitle: str | None) -> None:
    W, H = fig.get_figwidth(), fig.get_figheight()
    x = _PAD_IN / W
    fig.text(x, 1.0 - _PAD_IN / H, title, ha="left", va="top", fontsize=_TITLE_PT,
             fontweight="bold", color=TEXT, linespacing=1.15)
    if subtitle:
        y = _PAD_IN + (title.count("\n") + 1) * _TITLE_PT * 1.3 / 72.0 + 0.06
        fig.text(x, 1.0 - y / H, subtitle, ha="left", va="top", fontsize=_SUB_PT, color=TEXT_2,
                 linespacing=1.2)


def _ink_on(rgba) -> str:
    """Couleur de texte lisible sur un fond ``rgba`` (luminance relative WCAG)."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(x)) for x in rgba[:3])
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    # contraste avec le blanc (1,05 / (L + 0,05)) contre le texte sombre ((L + 0,05) / 0,054)
    return "#ffffff" if 1.05 / (lum + 0.05) > (lum + 0.05) / 0.054 else TEXT


def _save(fig, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=BG)
    import matplotlib.pyplot as plt

    plt.close(fig)
    return path


def plot_ic_heatmap(agg: pd.DataFrame, path: Path | str, top: int = 40, *,
                    title: str | None = None, subtitle: str | None = None) -> Path:
    """Carte de chaleur IC moyen, indicateur × horizon, des ``top`` indicateurs de plus grand
    |ic_mean| (sur l'ensemble des horizons). Divergente bleu / gris / orange, échelle
    symétrique ; valeur écrite dans chaque case, ``*`` si q_value_by < 0,05."""
    plt = _pyplot()
    from matplotlib.colors import LinearSegmentedColormap

    d = agg[np.isfinite(pd.to_numeric(agg["ic_mean"], errors="coerce"))]
    if d.empty:
        raise ValueError("aucun IC à représenter")
    horizons = sorted(int(h) for h in d["horizon"].unique())
    wide = d.pivot_table(index="indicator", columns="horizon", values="ic_mean", aggfunc="first")
    wide = wide.reindex(columns=horizons)
    qby = (d.pivot_table(index="indicator", columns="horizon", values="q_value_by", aggfunc="first")
           .reindex(index=wide.index, columns=horizons) if "q_value_by" in d.columns else None)
    strength = wide.abs().max(axis=1).sort_values(ascending=False, kind="stable")
    rows = list(strength.index[:top])
    M = wide.loc[rows].to_numpy(dtype=float)
    Q = qby.loc[rows].to_numpy(dtype=float) if qby is not None else np.full(M.shape, np.nan)
    fam = (agg.drop_duplicates("indicator").set_index("indicator")["family"]
           if "family" in agg.columns else pd.Series(dtype=object))
    vmax = float(np.nanmax(np.abs(M))) if np.isfinite(M).any() else 1e-3
    vmax = max(vmax, 1e-6)
    signed_best = [float(wide.loc[r].iloc[int(np.nanargmax(np.abs(wide.loc[r].to_numpy())))])
                   for r in rows]
    W = 8.2
    t, s, head = _header(W, title or ic_title(signed_best),
                         subtitle or f"Top {len(rows)} indicateurs par |IC moyen| entre actifs ; "
                                     "* : q de Benjamini-Yekutieli < 5 %")
    n = len(rows)
    body = 0.27 * n
    ticks_in, bottom_in = 0.4, 0.95
    H = head + ticks_in + body + bottom_in
    fig = plt.figure(figsize=(W, H), facecolor=BG)
    _draw_header(fig, t, s)
    ax = fig.add_axes((0.36, bottom_in / H, 0.30, body / H))
    cmap = LinearSegmentedColormap.from_list("ic_div", [BLUE, NEUTRAL, ORANGE])
    mesh = ax.pcolormesh(np.ma.masked_invalid(M), cmap=cmap, vmin=-vmax, vmax=vmax,
                         edgecolors=BG, linewidth=2)
    ax.set_facecolor(BG)
    ax.invert_yaxis()
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0, colors=TEXT_2, labelsize=8.5)
    ax.set_xticks(np.arange(len(horizons)) + 0.5, [f"h = {h}" for h in horizons])
    ax.xaxis.tick_top()
    ax.set_yticks(np.arange(n) + 0.5, rows)
    for i in range(n):
        for j in range(len(horizons)):
            v = M[i, j]
            if not np.isfinite(v):
                continue
            star = "*" if np.isfinite(Q[i, j]) and Q[i, j] < 0.05 else ""
            ax.text(j + 0.5, i + 0.5, fmt_number(v, 3, signed=True) + star, ha="center",
                    va="center", fontsize=7.5, color=_ink_on(cmap((v + vmax) / (2 * vmax))))
        f = fam.get(rows[i], "")
        ax.text(len(horizons) + 0.12, i + 0.5, "" if pd.isna(f) else str(f), ha="left",
                va="center", fontsize=8, color=TEXT_2, clip_on=False)
    cax = fig.add_axes((0.36, 0.45 / H, 0.30, 0.11 / H))
    cb = fig.colorbar(mesh, cax=cax, orientation="horizontal")
    cb.outline.set_visible(False)
    ticks = [-vmax, 0.0, vmax]
    cb.set_ticks(ticks, labels=[fmt_number(x, 3, signed=x != 0) for x in ticks])
    cb.ax.tick_params(labelsize=8, colors=TEXT_2, length=0)
    cb.set_label("IC de Spearman moyen (bleu : retournement, orange : continuation)",
                 fontsize=8.5, color=TEXT_2)
    return _save(fig, path)


def plot_family_bars(fam: pd.DataFrame, path: Path | str, *, title: str | None = None,
                     subtitle: str | None = None) -> Path:
    """Barres horizontales : plus grand |IC moyen| par famille, meilleur indicateur en
    étiquette directe (une seule série, pas de légende)."""
    plt = _pyplot()
    d = fam.dropna(subset=["max_abs_ic"]).sort_values("max_abs_ic", ascending=True)
    if d.empty:
        raise ValueError("aucune famille à représenter")
    best = d.iloc[-1]
    W = 8.2
    t, s, head = _header(W, title or (f"La famille « {best['family']} » porte le signal le plus "
                                      f"fort (|IC| {fmt_number(best['max_abs_ic'], 3)})"),
                         subtitle or "Meilleur couple (indicateur, horizon) de chaque famille, "
                                     "IC moyen entre actifs")
    n = len(d)
    body, bottom_in = 0.42 * n, 0.6
    H = head + 0.15 + body + bottom_in
    fig = plt.figure(figsize=(W, H), facecolor=BG)
    _draw_header(fig, t, s)
    ax = fig.add_axes((0.2, bottom_in / H, 0.62, body / H))
    _style_axes(ax, ygrid=False, xgrid=True)
    y = np.arange(n)
    ax.barh(y, d["max_abs_ic"], height=0.5, color=BLUE, edgecolor=BG, linewidth=1)
    ax.set_yticks(y, d["family"])
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_xlim(0, float(d["max_abs_ic"].max()) * 1.45)
    ax.set_xlabel("plus grand |IC moyen| de la famille", color=TEXT_2, fontsize=9)
    for yi, (_, r) in zip(y, d.iterrows()):
        ax.text(r["max_abs_ic"], yi, f"  {r['best_indicator']} (h = {int(r['best_horizon'])}, "
                f"{fmt_number(r['best_ic'], 3, signed=True)})", va="center", ha="left",
                fontsize=8, color=TEXT_2)
    return _save(fig, path)


def _model_color(m: str, fallback: list) -> str:
    if m in MODEL_COLORS:
        return MODEL_COLORS[m]
    if not fallback:
        return TEXT_2
    return fallback.pop(0)


def plot_excess_accuracy(metrics: pd.DataFrame, path: Path | str, *, models: Sequence[str],
                         model_col: str = "model", facet_col: str | None = None,
                         label_models: Sequence[str] = (), title: str | None = None,
                         subtitle: str | None = None) -> Path:
    """Barres groupées « accuracy − 50 % » (points) par horizon, un groupe de barres par modèle
    dans l'ordre fixe ``models``. Petits multiples par ``facet_col`` avec **axe Y partagé**.
    Bande grise : ± 1,96 × 0,5 / √n_eff (n_eff minimal des modèles de l'horizon) autour de 0.
    Étiquettes directes seulement pour ``label_models``."""
    plt = _pyplot()
    d = metrics[metrics[model_col].isin(models)].copy()
    d["accuracy"] = pd.to_numeric(d["accuracy"], errors="coerce")
    d = d[np.isfinite(d["accuracy"])]
    if d.empty:
        raise ValueError("aucune accuracy à représenter")
    present = [m for m in models if m in set(d[model_col])]
    facets = [None] if facet_col is None else list(dict.fromkeys(d[facet_col]))
    horizons = sorted(int(h) for h in d["horizon"].unique())
    nf, k = len(facets), len(present)
    W = min(max(8.0, 1.0 + nf * (0.6 + 0.3 * k * len(horizons))), 16.0)
    t, s, head = _header(W, title or "Accuracy par horizon",
                         subtitle or "Barres : accuracy − 50 % ; bande grise : bruit "
                                     "d'échantillonnage (± 1,96 × 0,5 / √n_eff)")
    legend_in, facet_in, body, bottom_in = 0.35, 0.3 if nf > 1 else 0.1, 3.0, 0.45
    H = head + legend_in + facet_in + body + bottom_in
    fig, axes = plt.subplots(1, nf, sharey=True, figsize=(W, H), facecolor=BG, squeeze=False)
    fig.subplots_adjust(left=0.75 / W, right=1 - 0.12 / W, bottom=bottom_in / H,
                        top=(bottom_in + body) / H, wspace=0.08)
    _draw_header(fig, t, s)
    fb = list(_FALLBACK_COLORS)
    colors = {m: _model_color(m, fb) for m in present}
    ax_w_px = (W - 0.9) / nf * 150
    bw = min(0.78 / max(k, 1), 24.0 / max(ax_w_px / max(len(horizons), 1), 1.0))
    ymax = 0.0
    for fi, facet in enumerate(facets):
        ax = axes[0, fi]
        _style_axes(ax)
        g = d if facet is None else d[d[facet_col] == facet]
        pending: list[tuple[int, np.ndarray, np.ndarray]] = []
        allvals: list[np.ndarray] = []
        for xi, h in enumerate(horizons):
            gh = g[g["horizon"] == h]
            ne = pd.to_numeric(gh.get("n_eff"), errors="coerce").min() if len(gh) else np.nan
            if np.isfinite(ne) and ne > 0:
                noise = 1.96 * 50.0 / math.sqrt(ne)
                ax.add_patch(plt.Rectangle((xi - 0.45, -noise), 0.9, 2 * noise, color=NOISE,
                                           linewidth=0, zorder=0))
                ymax = max(ymax, noise)
        for j, m in enumerate(present):
            gm = g[g[model_col] == m].set_index("horizon")
            xs = np.arange(len(horizons)) + (j - (k - 1) / 2.0) * bw
            vals = np.array([(float(gm.loc[h, "accuracy"]) - 0.5) * 100.0 if h in gm.index
                             else np.nan for h in horizons])
            ax.bar(xs, vals, width=bw, color=colors[m], edgecolor=BG, linewidth=1, zorder=2,
                   label=MODEL_LABELS.get(m, m) if fi == 0 else None)
            ymax = max(ymax, float(np.nanmax(np.abs(vals))) if np.isfinite(vals).any() else 0.0)
            allvals.append(vals)
            if m in label_models:
                pending.append((j, xs, vals))
        # étiquettes directes (modèles choisis seulement) : au-dessus des barres voisines que le
        # texte (plus large qu'une barre) survole, et décalées si elles se chevauchent
        step = 0.12 * max(ymax, 1.0)
        placed: dict[int, list[float]] = {}
        V = np.array(allvals)
        for j, xs, vals in pending:
            for xi, (x, v) in enumerate(zip(xs, vals)):
                if not np.isfinite(v):
                    continue
                sgn = 1.0 if v >= 0 else -1.0
                near = V[max(j - 1, 0):j + 2, xi]
                near = near[np.isfinite(near) & (np.sign(near) == sgn)]
                base = sgn * max(float(np.max(np.abs(near))) if near.size else 0.0, abs(v))
                y = base + sgn * 0.3 * step
                while any(abs(y - q) < step for q in placed.get(xi, [])):
                    y += sgn * step
                placed.setdefault(xi, []).append(y)
                ax.text(x, y, fmt_number(v, 1, signed=True), ha="center",
                        va="bottom" if v >= 0 else "top", fontsize=7.5, color=TEXT)
                ymax = max(ymax, abs(y) + step)
        ax.axhline(0, color=TEXT_2, linewidth=0.8, zorder=1)
        ax.set_xticks(np.arange(len(horizons)), [f"h = {h}" for h in horizons])
        ax.set_xlim(-0.6, len(horizons) - 0.4)
        if facet is not None:
            ax.set_title(str(facet), fontsize=10, color=TEXT, loc="left", pad=6)
    lim = max(ymax * 1.2, 1.0)
    axes[0, 0].set_ylim(-lim, lim)
    axes[0, 0].set_ylabel("accuracy − 50 % (points)", color=TEXT_2, fontsize=9)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.1 / W, 1 - head / H),
               ncol=min(k, 6), frameon=False, fontsize=8.5, labelcolor=TEXT_2,
               handlelength=1.0, columnspacing=1.2, borderaxespad=0.0)
    return _save(fig, path)


# ---------------------------------------------------------------------------
# README générés
# ---------------------------------------------------------------------------

DATA_COLUMNS: tuple[str, ...] = ("ticker", "n_bars", "start", "end", "missing_bars", "split_time",
                                 "n_indicators", "n_skipped", "benchmark", "deriv_coverage")
FOLD_COLUMNS: tuple[str, ...] = ("ticker", "model", "horizon", "fold", "n_train", "train_start",
                                 "train_end", "train_end_row", "test_start", "test_end",
                                 "test_start_row")
METRIC_COLUMNS: tuple[str, ...] = (
    "model", "sample", "horizon", "h_eff", "n", "n_eff", "accuracy", "balanced_accuracy", "auc",
    "brier", "log_loss", "p_binom", "pt_stat", "pt_pvalue", "base_rate", "excess_accuracy",
    "pt_hac_t", "pt_hac_pvalue", "acc_w", "dm_brier_vs_reversal_t")
DIAG_COLUMNS: tuple[str, ...] = ("horizon", "n", "coverage_q10_q90", "rho_past_60", "ic_p_up",
                                 "ic_partial", "median_abs_pred_bps", "median_abs_ret_bps",
                                 "frac_pred_gt_cost")
STRATEGY_COLUMNS: tuple[str, ...] = (
    "signal", "h", "threshold", "cost_bps", "n_signals", "n_trades", "n_long", "n_short",
    "hit_rate", "gross_bps_mean", "net_bps_mean", "net_bps_se", "gross_bps_sum", "net_bps_sum")
PRED_FILE_COLUMNS: tuple[str, ...] = (
    "time", "horizon", "last_close", "pred_median", "p_up", "p_up_cal", "ret", "y_true",
    "pred_ret", "q10_ret", "q90_ret", "reversal_h", "momentum_h", "always_up",
    "majority_prev_day", "past_ret_60")
RUN_COLUMNS: tuple[str, ...] = (
    "run", "ticker", "generated_at", "command", "backend", "covariates", "covariate_names",
    "stride", "context_len", "transform", "symmetric", "max_origins", "n_candidates",
    "n_excluded_gaps", "n_origins", "first_origin", "last_origin", "test_part_start", "days",
    "train_frac", "cost_bps", "threshold", "walk_forward_s", "origins_per_s")


def min_detectable_accuracy(n_eff: float, alpha: float = 0.05, power: float = 0.8) -> float:
    """Accuracy vraie détectable (binomial unilatéral, puissance ``power``) avec ``n_eff`` essais."""
    from scipy import stats

    if not n_eff or not np.isfinite(n_eff) or n_eff <= 0:
        return float("nan")
    z = stats.norm.isf(alpha) + stats.norm.isf(1 - power)
    return 0.5 + z * 0.5 / math.sqrt(n_eff)


def _fig(figures: Mapping[str, str], key: str, alt: str) -> str:
    return f"![{alt}]({figures[key]})\n" if key in figures else ""


def render_study_readme(*, meta: Mapping, data_table: pd.DataFrame, agg: pd.DataFrame,
                        top: pd.DataFrame, bottom: pd.DataFrame, fam: pd.DataFrame,
                        combined: pd.DataFrame | None, timings: pd.DataFrame,
                        figures: Mapping[str, str]) -> str:
    """README de ``reports/etat_des_lieux/`` : synthèse chiffrée, tableaux, figures, dictionnaire
    des colonnes, temps de calcul, méthode et limites (en français)."""
    from .evaluation import AGGREGATE_COLUMNS, SCORE_COLUMNS, SUMMARY_COLUMNS

    p = meta.get("params", {})
    tickers = list(p.get("tickers", []))
    horizons = list(p.get("horizons", []))
    ok = agg[np.isfinite(pd.to_numeric(agg["ic_mean"], errors="coerce"))]
    n_pairs = len(ok)
    n_trials = int(pd.to_numeric(ok["n_tickers"], errors="coerce").sum())
    qby = pd.to_numeric(ok["q_value_by"], errors="coerce")
    z = pd.to_numeric(ok["z_combined"], errors="coerce")
    n_sig = int(((qby < 0.05) & (z.abs() > 3)).sum())
    L: list[str] = []
    L.append(f"# État des lieux des indicateurs — direction à {', '.join(map(str, horizons))} minutes")
    L.append("")
    L.append(f"*Généré le {meta.get('generated_at', '')} par `{meta.get('command') or 'tradebot study'}` "
             f"(durée totale : {fmt_number(meta.get('total_seconds'), 0)} s ; commit "
             f"`{meta.get('git', {}).get('commit', '?')}`"
             f"{' + modifications locales' if meta.get('git', {}).get('dirty') else ''}). "
             f"Actifs : {', '.join(tickers)} ; {p.get('days')} jours de barres {p.get('interval')} ; "
             f"apprentissage = {fmt_number(p.get('train_frac'), 2)} premiers de la période.*")
    L.append("")
    hr_all = pd.to_numeric(ok["hit_rate_oos_mean"], errors="coerce")
    best_hr = (f", alors que le meilleur hit-rate hors échantillon mesuré ici est de "
               f"{fmt_number(hr_all.max(), 1, pct=True)}") if hr_all.notna().any() else ""
    L.append("> Recherche uniquement. Un indicateur « significatif » n'est pas une stratégie "
             "rentable : à 1 minute, couvrir 10 pb de frais aller-retour demande 72 à 89 % de "
             f"bonnes directions (docs/research/methodologie.md § 0){best_hr}.")
    L.append("")
    L.append("## 0. En bref")
    L.append("")
    if n_pairs:
        best = top.iloc[0]
        neg_share = float(np.mean(pd.to_numeric(top["ic_mean"], errors="coerce") < 0)) if len(top) else float("nan")
        L.append(f"* **{n_pairs} couples (indicateur, horizon)** mesurés sur {len(tickers)} actif(s), "
                 f"soit **{fmt_number(n_trials, 0)} tests** : il faut une correction pour tests multiples "
                 "(q-valeurs BH et BY) et viser |t| > 3.")
        L.append(f"* Plus fort signal : **`{best['indicator']}`** ({best['family']}) à h = "
                 f"{int(best['horizon'])} : IC moyen {fmt_number(best['ic_mean'], 3, signed=True)}, "
                 f"z de Stouffer {fmt_number(best['z_combined'], 1, signed=True)}, q BY "
                 f"{fmt_number(best['q_value_by'], 3)}, hit-rate hors échantillon "
                 f"{fmt_number(best['hit_rate_oos_mean'], 1, pct=True)}, AUC "
                 f"{fmt_number(best['auc_oos_mean'], 3)}.")
        L.append(f"* Sens : {fmt_number(neg_share, 0, pct=True)} des 20 plus forts |IC| sont négatifs "
                 "(IC < 0 : l'indicateur annonce un **retournement** ; > 0 : une continuation).")
        L.append(f"* **{n_sig} couples sur {n_pairs}** ont à la fois q BY < 5 % et |z| > 3. Le z combine "
                 "les actifs comme s'ils étaient indépendants (optimiste : les cryptos sont corrélées).")
        if len(top):
            stab = pd.to_numeric(top["frac_same_sign"], errors="coerce")
            tr = pd.to_numeric(top["ic_train_mean"], errors="coerce")
            te = pd.to_numeric(top["ic_test_mean"], errors="coerce")
            same = np.sign(tr) == np.sign(te)
            L.append(f"* Stabilité du top 20 : même signe sur tous les actifs pour "
                     f"{int((stab == 1).sum())}/{len(top)} ; même signe entre apprentissage et test "
                     f"pour {int(same.sum())}/{len(top)}.")
        hr = pd.to_numeric(ok["hit_rate_oos_mean"], errors="coerce")
        if hr.notna().any():
            b = ok.loc[hr.idxmax()]
            L.append(f"* Meilleur hit-rate hors échantillon : {fmt_number(hr.max(), 1, pct=True)} "
                     f"(`{b['indicator']}`, h = {int(b['horizon'])}) ; médiane des couples : "
                     f"{fmt_number(hr.median(), 1, pct=True)}.")
    if combined is not None and len(combined):
        c = combined.copy()
        for ticker in tickers:
            ct = c[c["ticker"] == ticker]
            if ct.empty:
                continue
            parts = []
            for h in horizons:
                ch = ct[ct["horizon"] == h]
                mod = ch[ch["model"].isin(["logit", "hgb"])]
                rev = ch[ch["model"] == "reversal_h"]
                if mod.empty or mod["accuracy"].isna().all():
                    continue
                bm = mod.loc[mod["accuracy"].idxmax()]
                parts.append(f"h = {h} : {bm['model']} {fmt_number(bm['accuracy'], 1, pct=True)} "
                             f"(AUC {fmt_number(bm['auc'], 3)}) contre retournement "
                             f"{fmt_number(rev['accuracy'].iloc[0] if len(rev) else np.nan, 1, pct=True)}")
            if parts:
                L.append(f"* Modèle combiné, {ticker} : " + " ; ".join(parts) + ".")
    L.append("")
    L.append("## 1. Données")
    L.append("")
    L.append(to_markdown(data_table[[c for c in DATA_COLUMNS if c in data_table.columns]],
                         {"deriv_coverage": "1%"}))
    L.append("")
    L.append("## 2. Les indicateurs les plus informatifs")
    L.append("")
    L.append(_fig(figures, "ic_heatmap", "Carte de chaleur des IC"))
    show = ["indicator", "family", "horizon", "ic_mean", "frac_same_sign", "ic_train_mean",
            "ic_test_mean", "hit_rate_oos_mean", "auc_oos_mean", "z_combined", "q_value_by"]
    fmts = {"ic_mean": "+3", "ic_train_mean": "+3", "ic_test_mean": "+3", "frac_same_sign": 2,
            "hit_rate_oos_mean": "1%", "auc_oos_mean": 3, "z_combined": "+1", "q_value_by": 3}
    L.append("Les 20 couples (indicateur, horizon) de plus grand |IC moyen| (`top_20.csv`) :")
    L.append("")
    L.append(to_markdown(top[[c for c in show if c in top.columns]], fmts, code_columns=["indicator"]))
    L.append("")
    L.append("## 3. Les moins informatifs")
    L.append("")
    L.append("Les 20 couples de plus petit |IC moyen| parmi ceux mesurés sur tous les actifs "
             "(`bottom_20.csv`) : ces indicateurs n'apportent rien seuls à ces horizons.")
    L.append("")
    L.append(to_markdown(bottom[[c for c in show if c in bottom.columns]], fmts,
                         code_columns=["indicator"]))
    L.append("")
    L.append("## 4. Synthèse par famille")
    L.append("")
    L.append(_fig(figures, "familles", "Meilleur IC par famille"))
    L.append(to_markdown(fam, {"median_abs_ic": 3, "max_abs_ic": 3, "best_ic": "+3",
                               "frac_q_by_05": "0%", "mean_hit_rate_oos": "1%",
                               "mean_auc_oos": 3}, code_columns=["best_indicator"]))
    L.append("")
    L.append("## 5. Modèles combinés (walk-forward purgé) contre baselines")
    L.append("")
    if combined is None or not len(combined):
        L.append("Non calculés (`--no-combined`).")
    else:
        L.append(f"Tous les indicateurs de l'actif, `logit` (régression logistique régularisée) et "
                 f"`hgb` (gradient boosting), walk-forward croissant en {p.get('n_splits')} plis purgés "
                 f"de h barres, une origine toutes les {p.get('row_step')} barres ; baselines sur les "
                 "mêmes barres. Détail : `combined_summary.csv` ; bornes des plis : `combined_folds.csv`.")
        L.append("")
        L.append(_fig(figures, "modeles_combines", "Modèles combinés contre baselines"))
        wide = []
        for (tk, mdl), g in combined.groupby(["ticker", "model"], sort=False):
            row = {"ticker": tk, "model": mdl}
            for h in horizons:
                gh = g[g["horizon"] == h]
                row[f"acc h{h}"] = gh["accuracy"].iloc[0] if len(gh) else np.nan
            for h in horizons:
                gh = g[g["horizon"] == h]
                row[f"AUC h{h}"] = gh["auc"].iloc[0] if len(gh) else np.nan
            for h in horizons:
                gh = g[g["horizon"] == h]
                row[f"acc_w h{h}"] = gh["acc_w"].iloc[0] if len(gh) else np.nan
            row["n_eff (h max)"] = g.loc[g["horizon"] == max(horizons), "n_eff"].max()
            wide.append(row)
        wide = pd.DataFrame(wide)
        f2 = {c: ("1%" if c.startswith("acc") else 3) for c in wide.columns
              if c.startswith(("acc", "AUC"))}
        f2["n_eff (h max)"] = 0
        L.append(to_markdown(wide, f2, code_columns=["model"]))
        L.append("")
        L.append("`acc` : accuracy ; `acc_w` : accuracy pondérée par |rendement| (ce qui compte pour "
                 "le P&L). Les baselines binaires valent 0,52 / 0,48 : leur AUC égale leur balanced "
                 "accuracy.")
    L.append("")
    L.append("## 6. Fichiers")
    L.append("")
    files = [
        ("scores_<ACTIF>.csv", "scores de chaque indicateur × horizon pour un actif (`indicator_scores`)"),
        ("aggregate.csv", "agrégat entre actifs, trié par |ic_mean| (`aggregate_scores`) ; sert à "
                          "choisir les covariables TimesFM (`ic_train_mean`)"),
        ("top_20.csv, bottom_20.csv", "extraits de l'agrégat (plus grands / plus petits |ic_mean|)"),
        ("familles.csv", "synthèse par famille"),
        ("combined_summary.csv", "métriques hors échantillon des modèles combinés et des baselines"),
        ("combined_folds.csv", "bornes de chaque pli (vérification de la purge)"),
        ("catalogue_indicateurs.csv", "nom, famille et description de chaque indicateur"),
        ("donnees.csv", "couverture des données par actif"),
        ("timings.csv", "temps de chaque étape"),
        ("run.json", "paramètres, dates de coupure, versions, commit"),
        ("ic_heatmap.png, familles.png, modeles_combines.png", "figures"),
    ]
    L.append("| fichier | contenu |\n|---|---|")
    L.extend(f"| `{f}` | {d} |" for f, d in files)
    L.append("")
    L.append("## 7. Dictionnaire des colonnes")
    L.append("")
    L.append("### scores_<ACTIF>.csv")
    L.append("")
    L.append(columns_doc_table(["ticker", *SCORE_COLUMNS]))
    L.append("")
    L.append("### aggregate.csv, top_20.csv, bottom_20.csv")
    L.append("")
    L.append(columns_doc_table(AGGREGATE_COLUMNS))
    L.append("")
    L.append("### familles.csv")
    L.append("")
    L.append(columns_doc_table(FAMILY_COLUMNS))
    L.append("")
    L.append("### combined_summary.csv")
    L.append("")
    L.append(columns_doc_table(["ticker", "model", *SUMMARY_COLUMNS]))
    L.append("")
    L.append("### combined_folds.csv")
    L.append("")
    L.append(columns_doc_table(FOLD_COLUMNS))
    L.append("")
    L.append("### donnees.csv, timings.csv")
    L.append("")
    L.append(columns_doc_table([*DATA_COLUMNS, "step", "seconds"]))
    L.append("")
    L.append("## 8. Temps de calcul")
    L.append("")
    t = timings.copy()
    t["ticker"] = t["ticker"].replace("", "(global)")
    piv = t.pivot_table(index="ticker", columns="step", values="seconds", aggfunc="sum",
                        sort=False).reset_index()
    L.append(to_markdown(piv, 1))
    L.append("")
    L.append("## 9. Méthode et limites")
    L.append("")
    L.append("* **Coupure apprentissage / test** : `split = round(train_frac × n)` barres ; "
             "apprentissage = barres < split − h (purge de h barres : leurs cibles chevauchent le "
             "test), test = barres ≥ split. Signe et médiane de chaque indicateur appris sur "
             "l'apprentissage seulement.")
    L.append("* **Chevauchement** : à h barres, les cibles de barres voisines partagent h − 1 minutes ; "
             "t de Newey-West (h et 2h retards), t par jours et binomial sur n / h en tiennent compte.")
    L.append("* **Tests multiples** : Benjamini-Hochberg et Benjamini-Yekutieli sur toutes les lignes ; "
             "Stouffer suppose des actifs indépendants (optimiste).")
    L.append("* **Pas de coffre-fort** : la méthodologie (§ 4.4) réserve les 30 derniers jours à une "
             "validation finale unique ; cette commande utilise toute la période demandée.")
    L.append("* **Modèles combinés** : NaN imputés par la médiane de l'apprentissage du pli ; "
             f"apprentissage éclairci au-delà de {p.get('max_train_rows')} lignes ; arrêt précoce "
             "chronologique pour `hgb`.")
    L.append("* **Dérivés** alignés avec un décalage de publication prudent (+5 min) ; le funding du "
             "mois en cours n'existe pas en fichiers bulk (NaN).")
    return "\n".join(L) + "\n"


def _read_run_csv(out_dir: Path, run: str, kind: str) -> pd.DataFrame | None:
    p = out_dir / f"{run}_{kind}.csv"
    return pd.read_csv(p) if p.exists() else None


def render_timesfm_readme(out_dir: Path | str, runs: pd.DataFrame) -> str:
    """README de ``reports/timesfm/`` : un paragraphe par run (lu dans ``{run}_*.csv``)."""
    out_dir = Path(out_dir)
    runs = runs.copy()
    for c in ("first_origin", "last_origin", "test_part_start"):
        if c in runs.columns:
            runs[c] = pd.to_datetime(runs[c], utc=True, errors="coerce")
    L: list[str] = []
    L.append("# Backtest TimesFM (zero-shot) contre baselines — direction à 5, 10 et 15 minutes")
    L.append("")
    stamp = pd.Timestamp.now(tz="UTC").floor("s")
    L.append(f"*Mis à jour le {stamp:%Y-%m-%d %H:%M} UTC ; {len(runs)} run(s). Chaque "
             "`python -m tradebot timesfm-backtest` ajoute ou remplace ses runs.*")
    L.append("")
    L.append("> **Licence** : les poids de TimesFM 3.0 (`timesfm3`) sont sous licence non commerciale "
             "(`timesfm-non-commercial-license-v1.0`) : recherche uniquement, aucun trading réel. "
             "TimesFM 2.5 (`timesfm2p5`) est sous Apache-2.0.")
    L.append(">")
    L.append("> **Puissance** : quelques centaines d'origines ne permettent de détecter qu'une "
             "accuracy vraie de 57 % ou plus ; trois horizons testés = trois essais. Ne rien conclure "
             "d'un écart de quelques points.")
    L.append("")
    L.append("## 0. En bref")
    L.append("")
    details: list[str] = []
    for _, r in runs.iterrows():
        run = str(r["run"])
        m = _read_run_csv(out_dir, run, "metrics")
        dg = _read_run_csv(out_dir, run, "diagnostics")
        st = _read_run_csv(out_dir, run, "strategy")
        if m is None:
            continue
        allm = m[m["sample"] == "toutes"]
        verdict = verdict_vs_baselines(allm, ["timesfm"], ["reversal_h", "momentum_h", "always_up",
                                                           "majority_prev_day"], "TimesFM")
        acc = allm[allm["model"] == "timesfm"].set_index("horizon")
        rev = allm[allm["model"] == "reversal_h"].set_index("horizon")
        hs = sorted(acc.index)
        ne = float(acc["n_eff"].min()) if len(acc) else float("nan")
        bits = " ; ".join(f"h = {h} : {fmt_number(acc.loc[h, 'accuracy'], 1, pct=True)} contre "
                          f"{fmt_number(rev.loc[h, 'accuracy'] if h in rev.index else np.nan, 1, pct=True)}"
                          for h in hs)
        L.append(f"* **{run}** ({int(r.get('n_origins', 0))} origines) — {verdict}. Accuracy TimesFM "
                 f"contre retournement : {bits}. Avec n_eff ≈ {fmt_number(ne, 0)} (h = {max(hs)}), "
                 f"seule une accuracy vraie ≥ {fmt_number(min_detectable_accuracy(ne), 1, pct=True)} "
                 "serait détectable (puissance 80 %).")
        if dg is not None and len(dg):
            L.append("  ρ(P(hausse), rendement des 60 min passées) : "
                     + " / ".join(fmt_number(v, 2, signed=True) for v in dg["rho_past_60"])
                     + " ; IC partiel (hors retournement) : "
                     + " / ".join(fmt_number(v, 3, signed=True) for v in dg["ic_partial"]) + ".")
        if st is not None and len(st):
            s0 = st[st["signal"] == "timesfm"]
            if len(s0):
                L.append("  Mini-stratégie (P brute, seuil "
                         f"{fmt_number(s0['threshold'].iloc[0], 2)}, {fmt_number(s0['cost_bps'].iloc[0], 0)} pb) : "
                         + " / ".join(f"{fmt_number(v, 1, signed=True)} pb" for v in s0["net_bps_mean"])
                         + " nets par trade.")

        D: list[str] = [f"### {run}", ""]
        cmd = r.get("command")
        if isinstance(cmd, str) and cmd:
            D.append(f"Commande : `{cmd}`  ")
        cov = r.get("covariate_names")
        D.append(f"Backend `{r.get('backend')}`, contexte {r.get('context_len')} ({r.get('transform')}), "
                 f"pas {r.get('stride')} min, origines du {_fmt_cell(r.get('first_origin'), None, '—')} "
                 f"au {_fmt_cell(r.get('last_origin'), None, '—')} "
                 f"({r.get('n_origins')} sur {r.get('n_candidates')} candidates, "
                 f"{r.get('n_excluded_gaps')} écartées pour trou), covariables : "
                 f"{'aucune' if not isinstance(cov, str) or not cov else cov.replace(';', ', ')}. "
                 f"Walk-forward : {fmt_number(r.get('walk_forward_s'), 0)} s "
                 f"({fmt_number(r.get('origins_per_s'), 1)} origines/s).")
        D.append("")
        png = out_dir / f"{run}_accuracy.png"
        if png.exists():
            D.append(f"![Accuracy {run}]({png.name})")
            D.append("")
        cols = ["model", "sample", "horizon", "n", "n_eff", "accuracy", "balanced_accuracy", "auc",
                "brier", "log_loss", "acc_w", "p_binom", "pt_hac_t", "dm_brier_vs_reversal_t"]
        D.append(to_markdown(m[[c for c in cols if c in m.columns]],
                             {"n_eff": 0, "accuracy": "1%", "balanced_accuracy": "1%", "auc": 3,
                              "brier": 4, "log_loss": 4, "acc_w": "1%", "p_binom": 3,
                              "pt_hac_t": "+2", "dm_brier_vs_reversal_t": "+2"},
                             code_columns=["model"]))
        D.append("")
        if dg is not None and len(dg):
            D.append(to_markdown(dg, {"coverage_q10_q90": 3, "rho_past_60": "+2", "ic_p_up": "+3",
                                      "ic_partial": "+3", "median_abs_pred_bps": 1,
                                      "median_abs_ret_bps": 1, "frac_pred_gt_cost": "1%"}))
            D.append("")
        if st is not None and len(st):
            sc = ["signal", "h", "threshold", "cost_bps", "n_trades", "hit_rate", "gross_bps_mean",
                  "net_bps_mean", "net_bps_se"]
            D.append(to_markdown(st[[c for c in sc if c in st.columns]],
                                 {"threshold": 2, "cost_bps": 0, "hit_rate": "1%",
                                  "gross_bps_mean": "+2", "net_bps_mean": "+2", "net_bps_se": 2},
                                 code_columns=["signal"]))
            D.append("")
        details += D
    L.append("")
    L.append("## 1. Runs")
    L.append("")
    rc = ["run", "ticker", "backend", "covariates", "stride", "context_len", "n_origins",
          "first_origin", "last_origin", "walk_forward_s", "origins_per_s"]
    L.append(to_markdown(runs[[c for c in rc if c in runs.columns]],
                         {"walk_forward_s": 0, "origins_per_s": 1}, code_columns=["run"]))
    L.append("")
    L.append("## 2. Détail par run")
    L.append("")
    L.append("`timesfm` : P(hausse) brute sur toutes les origines ; `timesfm_cal` : recalibrée "
             "(isotonique) sur la 2e moitié des origines, apprise sur la 1re (seulement si elle "
             "compte au moins 300 labels par horizon) ; baselines sur les mêmes origines. Stratégie : `reversal_h` au seuil 0,5 (trade à chaque signal) sert de "
             "référence de coût.")
    L.append("")
    L.extend(details)
    L.append("## 3. Fichiers")
    L.append("")
    L.append("| fichier | contenu |\n|---|---|")
    for f, d in (("runs.csv", "un run par ligne (paramètres, période, débit)"),
                 ("<run>_predictions.csv", "une ligne par (origine, horizon) : prévision TimesFM, "
                                           "baselines, rendement réalisé"),
                 ("<run>_metrics.csv", "métriques de direction par modèle × horizon"),
                 ("<run>_diagnostics.csv", "calibration des déciles, lien avec le rendement passé"),
                 ("<run>_strategy.csv", "mini-stratégie non chevauchante avec frais"),
                 ("<run>_accuracy.png", "accuracy − 50 % de TimesFM et des baselines"),
                 ("timings_last_run.csv", "temps des étapes du dernier appel")):
        L.append(f"| `{f}` | {d} |")
    L.append("")
    L.append("## 4. Dictionnaire des colonnes")
    L.append("")
    for title, cols in (("runs.csv", RUN_COLUMNS), ("<run>_predictions.csv", PRED_FILE_COLUMNS),
                        ("<run>_metrics.csv", METRIC_COLUMNS),
                        ("<run>_diagnostics.csv", DIAG_COLUMNS),
                        ("<run>_strategy.csv", STRATEGY_COLUMNS)):
        L.append(f"### {title}")
        L.append("")
        L.append(columns_doc_table(cols))
        L.append("")
    L.append("## 5. Méthode et limites")
    L.append("")
    L.append("* Origines sur une grille alignée sur l'horloge UTC tous les `stride` minutes, dans la "
             "partie **test** (après `train_frac`) ; contexte = les `context_len` dernières clôtures "
             "jusqu'à t inclus, centré en float64 ; un seul appel à horizon 15, lu aux pas 5, 10, 15.")
    L.append("* Covariables `top` : choisies par |ic_train_mean| de `reports/etat_des_lieux/aggregate.csv` "
             "(apprentissage seulement) ; les origines commencent après la fin de l'apprentissage de "
             "cette étude.")
    L.append("* `n_eff = n / ceil(h / stride)` ; PT HAC avec 2 × h_eff retards ; Diebold-Mariano sur le "
             "Brier contre le retournement.")
    L.append("* La P(hausse) brute est trop confiante : la stratégie au seuil 0,55 n'a de sens qu'avec "
             "`timesfm_cal` (recalibrée sur une période antérieure).")
    L.append("* Frais seuls (pas de spread ni de glissement) : le coût réel est plus élevé.")
    return "\n".join(L) + "\n"
