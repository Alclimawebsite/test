# Architecture — tradebot

Objectif de la phase 1 : **état des lieux des indicateurs** pour prévoir la
direction (hausse / baisse) des **grosses cryptos (BTC, SOL, ETH…)** à **5, 10 et
15 barres** (1 barre = 1 minute par défaut), et mesurer ce qu'apporte **Google
TimesFM** par rapport aux indicateurs classiques et à des baselines naïves.
Les actions (Yahoo Finance) restent supportées en secondaire.

```
src/tradebot/
  config.py      horizons, univers (BTCUSDT, SOLUSDT, ETHUSDT…), frais, TimesFMConfig, StudyConfig
  data.py        OHLCV Binance (bulk data.binance.vision + API data-api.binance.vision),
                 données dérivés (funding, open interest, ratios long/short), Yahoo en secondaire, cache parquet
  indicators.py  catalogue d'indicateurs strictement causaux + registre
  targets.py     rendements futurs et labels de direction à h barres (invalide si trou de données)
  evaluation.py  pouvoir prédictif de chaque indicateur (IC, hit-rate OOS, AUC, FDR) + modèles combinés
  forecaster.py  wrapper TimesFM (3.0 et 2.5), probabilité de hausse depuis les quantiles
  backtest.py    walk-forward TimesFM vs baselines, métriques de direction, mini-stratégie avec frais
  report.py      tableaux CSV/Markdown (sans tabulate) + graphiques PNG + README générés dans reports/
  cli.py         commandes `tradebot fetch | study | timesfm-backtest | predict` (argparse)
  __main__.py    `python -m tradebot …`
  polymarket.py, polymarket_wallets.py
                 (annexe) lecture seule des marchés Polymarket « Up or Down » 5 min / 15 min / 4 h,
                 pour comparer nos P(hausse) aux probabilités implicites du marché
```

Utilisation (venv du projet) :

```bash
python -m tradebot fetch --tickers BTCUSDT SOLUSDT --days 365          # cache data/cache/
python -m tradebot study --tickers BTCUSDT SOLUSDT --days 30            # -> reports/etat_des_lieux/
python -m tradebot timesfm-backtest --tickers BTCUSDT --stride 30 --max-origins 200   # -> reports/timesfm/
python -m tradebot timesfm-backtest --tickers SOLUSDT --covariates top --top-k 5 --stride 60
python -m tradebot predict --ticker SOLUSDT                              # P(hausse) 5/10/15 min
```

## Sources de données (vérifiées depuis l'environnement)

| Source | Accès | Contenu |
|---|---|---|
| `https://data.binance.vision/data/spot/monthly/klines/{SYM}/1m/{SYM}-1m-{YYYY-MM}.zip` (+ `daily/…-{YYYY-MM-DD}.zip`) | OK | klines 1m depuis 2017, sans limite de débit |
| `https://data-api.binance.vision/api/v3/klines` | OK | mêmes klines, 1000 barres/requête, pour les dernières heures |
| `https://data.binance.vision/data/futures/um/daily/metrics/{SYM}/{SYM}-metrics-{YYYY-MM-DD}.zip` | OK | open interest, ratios long/short (top traders, comptes), ratio taker long/short, pas de 5 min |
| `https://data.binance.vision/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip` | OK | taux de funding toutes les 8 h |
| `api.binance.com`, `fapi.binance.com` | **451 (restriction géographique)** | ne pas utiliser |
| Yahoo Finance (`yfinance`) | OK | actions, 1m sur 30 jours max |

Format kline Binance (12 colonnes) : `open_time, open, high, low, close, volume,
close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore`.
**Attention** : depuis 2025 les fichiers bulk *spot* horodatent en **microsecondes**
(16 chiffres) alors que l'API renvoie des millisecondes (13 chiffres) → détecter
l'unité par la magnitude. Les CSV peuvent avoir ou non une ligne d'en-tête.

## Conventions communes (contrat entre modules)

* **Format OHLCV** : `pd.DataFrame` indexé par un `DatetimeIndex` *tz-aware*
  (UTC pour la crypto ; fuseau de la place pour les actions), trié, sans doublon,
  horodaté à l'**ouverture** de la barre. Colonnes (float64) :
  `open, high, low, close, volume` et, pour la crypto, `quote_volume, trades,
  taker_buy_volume` (base) — absentes ou NaN pour les actions.
  Colonne `session` (`datetime.date`) : date UTC pour la crypto, date locale de la
  séance pour les actions (ajoutée par `data.add_session`).
* **Dérivés (crypto, optionnel)** : colonnes `funding_rate, open_interest,
  open_interest_value, ls_ratio_top, ls_ratio_accounts, taker_ls_ratio`, alignées
  *as-of* avec un **décalage de publication prudent** (une mesure horodatée `T`
  n'est utilisable qu'à partir de la barre qui s'ouvre à `T + 5 min` ; le funding
  à partir de son `calc_time`). NaN quand indisponible.
* **Causalité** : toute valeur calculée pour la barre `t` (barre fermée) n'utilise
  que les barres `<= t`. Testé automatiquement (on tronque après `t`, la valeur en
  `t` doit être identique).
* **Horizon** `h` en barres. `r_h(t) = log(close[t+h] / close[t])`, **NaN si la barre
  `t+h` n'est pas exactement `h` barres plus tard dans le temps** (trou de données,
  nuit/week-end pour les actions) — voir `targets.future_log_return`.
* **Direction** : `y = 1` si `r_h > eps`, `0` si `r_h < -eps`, NaN sinon. `eps = 0` par défaut.
* Aucun appel réseau hors de `data.py` (et du téléchargement des poids TimesFM), sauf les
  modules annexes `polymarket*.py` (API publiques Polymarket, lecture seule).
* **Coupure apprentissage / test commune** : `split = round(train_frac × n)` barres de l'actif
  (`evaluation.indicator_scores`, origines TimesFM de `timesfm-backtest`). Apprentissage =
  barres `< split − h` (purge de h barres), test = barres `≥ split`.

## API par module

### data.py
```python
def fetch_ohlcv(ticker: str, interval: str = "1m", days: int = 365,
                cache: bool = True, end: pd.Timestamp | None = None) -> pd.DataFrame
    # Crypto (config.is_crypto) : mois complets via les zips mensuels, mois en cours via les
    # zips journaliers, dernières heures via l'API klines ; Actions : yfinance (1m <= 30 jours).
    # Supprime la barre en cours (non clôturée). Cache parquet data/cache/{ticker}_{interval}.parquet
    # fusionné et incrémental (ne retélécharge pas ce qui est déjà en cache).
def fetch_derivatives(symbol: str, start, end, cache: bool = True) -> pd.DataFrame
    # metrics 5 min + funding ; index UTC = horodatage de *disponibilité* (décalage inclus)
def attach_derivatives(df: pd.DataFrame, deriv: pd.DataFrame) -> pd.DataFrame   # merge as-of causal
def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame
def add_session(df: pd.DataFrame, ticker: str | None = None) -> pd.DataFrame
def load_universe(tickers, interval="1m", days=365, cache=True,
                  with_derivatives=True) -> dict[str, pd.DataFrame]
```

### indicators.py
```python
@dataclass(frozen=True)
class IndicatorSpec:
    name: str            # identifiant unique snake_case, ex. "rsi_14"
    family: str          # "tendance" | "momentum" | "volatilite" | "volume" | "flux"
                         # | "microstructure" | "statistique" | "calendrier" | "marche" | "derives"
    description: str     # une phrase en français
    func: Callable[[pd.DataFrame], pd.Series]
    needs_volume: bool = False
    needs_benchmark: bool = False       # colonne bench_close
    needs_columns: tuple[str, ...] = () # ex. ("taker_buy_volume",) ou ("funding_rate",)

REGISTRY: dict[str, IndicatorSpec]
def compute_indicators(df, names=None, benchmark: pd.Series | None = None) -> pd.DataFrame
    # ignore les indicateurs dont les colonnes requises sont absentes / entièrement NaN
def list_indicators() -> pd.DataFrame
```
Indicateurs **normalisés** (ratios, oscillateurs bornés, z-scores roulants, unités de
volatilité) : jamais un niveau de prix brut.

### targets.py (écrit)
`bar_duration(index)`, `future_log_return(df, h, bar=None)`, `direction_label(ret, eps)`,
`make_targets(df, horizons, eps) -> ret_{h}, dir_{h}`.

### evaluation.py
```python
def indicator_scores(features, targets, horizons=(5, 10, 15), train_frac=0.6) -> pd.DataFrame
    # une ligne par (indicateur, horizon) pour UN actif (colonnes SCORE_COLUMNS) :
    # n, ic_spearman, ic_daily_mean, ic_daily_t, n_days, frac_months_same_sign,
    # ic_nw_t (Newey-West, lags=h), ic_nw_t_2h (lags=2h), p_value (de ic_nw_t),
    # t_cons / p_value_cons (le plus prudent de ic_nw_t, ic_nw_t_2h, ic_daily_t ; 0 si signes opposés),
    # ic_train, ic_test, train_sign, median_train, hit_rate_oos, auc_oos, up_rate_oos,
    # n_oos, n_eff_oos (= n_oos/h), p_hit (binomial unilatéral sur n_eff)
    # attrs : train_frac, split_row, split_time, n_rows
def aggregate_scores(per_ticker: dict[str, pd.DataFrame], families=None) -> pd.DataFrame
    # AGGREGATE_COLUMNS : indicator, horizon, family, n_tickers, ic_mean, ic_std, ic_t_cross,
    # frac_same_sign, ic_daily_t_mean, ic_train_mean (sélection sans fuite), ic_test_mean,
    # hit_rate_oos_mean, auc_oos_mean, z_combined / p_combined (Stouffer sur t_cons),
    # q_value (Benjamini-Hochberg), q_value_by (Benjamini-Yekutieli) ; tri par |ic_mean|
def combined_model_walkforward(features, targets, horizons=(5, 10, 15), n_splits=5,
                               model="logit", max_train_rows=None, row_step=1, *,
                               random_state=0) -> pd.DataFrame
    # walk-forward croissant purgé de h barres ; "logit" | "hgb" (arrêt précoce chronologique) ;
    # colonnes fold, p_up_{h}, y_{h}, ret_{h} ; attrs : folds (bornes de chaque pli), row_step…
def direction_summary(df_oos, horizons=(5, 10, 15)) -> pd.DataFrame
    # SUMMARY_COLUMNS : horizon, n, n_eff (= n / ceil(h/row_step)), accuracy, balanced_accuracy,
    # auc, brier, log_loss, bss, acc_w (accuracy pondérée par |ret|), up_rate, p_binom
# outils : newey_west_tstat, benjamini_hochberg ("bh" | "by"), stouffer, purged_walk_forward_splits
```
Grands volumes (≈ 525 000 barres par an et par actif) : code vectorisé, Spearman par rangs
calculés une fois par colonne ; mesuré sur BTCUSDT 1 an × 158 indicateurs × 3 horizons ≈ 54 s.

### forecaster.py
```python
QUANTILE_LEVELS = (0.1, ..., 0.9); MODEL_REPOS = {"timesfm3": ..., "timesfm2p5": ...}
@dataclass
class ForecastBatch:
    median: np.ndarray       # (n_series, horizon), espace du contexte transformé
    quantiles: np.ndarray    # (n_series, horizon, 9), déciles 0.1..0.9 triés
    last_value: np.ndarray   # (n_series,)
    quantile_levels: tuple[float, ...]
    def prob_up(self, h, threshold=None) -> np.ndarray   # seuil = last_value par défaut

class TimesFMForecaster:
    def __init__(self, cfg: TimesFMConfig = TimesFMConfig(), model=None, *,
                 use_symmetric_averaging=True, cache_dir=None, local_files_only=False)
    def load(self) -> TimesFMForecaster      # sinon chargement au premier forecast (cache local d'abord)
    def forecast(self, contexts: list[np.ndarray], horizon: int,
                 covariates: list[np.ndarray | None] | None = None) -> ForecastBatch
        # contexte centré en float64 sur sa dernière valeur ; lots homogènes par nombre de covariables
def transform_context(prices, kind="logprice") -> np.ndarray   # "logprice" | "price" | "cumret"
def prob_up(quantiles_h, threshold, levels=QUANTILE_LEVELS) -> np.ndarray   # borné [0.01, 0.99]
def calibrate_isotonic(p_train, y_train) -> Callable   # à ajuster sur une période ANTÉRIEURE
def prepare_covariates(cov) -> np.ndarray; def to_log_return(values, last_value, kind) -> np.ndarray
```

### backtest.py
```python
def walk_forward_timesfm(df, forecaster, horizons=(5, 10, 15), context_len=512, stride=5,
                         covariates: pd.DataFrame | None = None, start=None, end=None,
                         max_origins=None, max_gap_bars=3, chunk_size=None) -> pd.DataFrame
    # origines sur une grille alignée sur l'horloge UTC (multiple de stride barres) ;
    # origines dont le contexte a un trou > max_gap_bars écartées ; max_origins : sous-échantillonnage
    # régulier. Colonnes : time, horizon, last_close, pred_median (prix), p_up (brute), ret, y_true,
    # pred_ret, q10_ret, q90_ret ; attrs : n_candidates, n_excluded_gaps, n_origins, stride, bar…
def baseline_predictions(df, origins, horizons) -> pd.DataFrame
    # format long (time, horizon) : momentum_h, reversal_h, always_up, majority_prev_day
    # (probabilités 0,52 / 0,48 / 0,5), ret, y_true
def direction_metrics(y_true, p_up, h) -> dict
    # n, n_eff (= n // h), accuracy, balanced_accuracy, auc, brier, log_loss, p_binom, pt_stat,
    # pt_pvalue, base_rate, excess_accuracy, pt_hac_t / pt_hac_pvalue (Newey-West lags=2h ;
    # NaN si moins de PT_HAC_MIN_CLASS = 10 prévisions dans une classe)
def metrics_by_horizon(preds, stride=None, p_col="p_up") -> pd.DataFrame   # h_eff = ceil(h/stride)
def toy_strategy(preds, h, threshold=0.55, cost_bps=10.0, *, p_col="p_up", bar=None) -> dict
    # trades non chevauchants long/short ; n_signals, n_trades, n_long, n_short, hit_rate,
    # gross_bps_mean, net_bps_mean, net_bps_se, gross_bps_sum, net_bps_sum
def pesaran_timmermann(y_true, y_pred) -> (stat, p_value)
```

### report.py
Mise en forme uniquement (aucun chargement de données) :
```python
def report_dir(study, root=None) -> Path          # reports/<étude>/
def write_csv(df, path); def write_text(path, text); def write_json(path, obj)
def fmt_number(v, decimals=3, *, pct=False, signed=False) -> str   # format français
def to_markdown(df, formats=None, *, index=False, na="—", code_columns=()) -> str  # sans tabulate
def columns_doc_table(columns, docs=COLUMN_DOCS) -> str   # dictionnaire des colonnes (README)
def family_summary(agg) -> pd.DataFrame           # FAMILY_COLUMNS
def top_bottom(agg, k=20) -> (top, bottom)        # plus grands / plus petits |ic_mean|
def ic_title(ic_values) -> str; def verdict_vs_baselines(metrics, models, baselines, label, ...) -> str
def plot_ic_heatmap(agg, path, top=40)            # divergente bleu #2a78d6 / gris / orange #eb6834
def plot_family_bars(fam, path)
def plot_excess_accuracy(metrics, path, *, models, facet_col=None, label_models=())
def render_study_readme(...) -> str; def render_timesfm_readme(out_dir, runs) -> str
```
Style : fond `#fcfcfb`, texte `#0b0b0b` / `#52514e`, grille `#e4e3de`, une seule échelle Y,
titre court qui dit la conclusion (calculée), nombres au format français dans le Markdown.

### cli.py
```python
def run_fetch(tickers, *, interval, days, derivatives=True, cache=True, loader=None) -> pd.DataFrame
def run_study(tickers, *, interval, days, horizons, train_frac, combined=True, n_splits=5,
              row_step=5, max_train_rows=100_000, out_dir=None, loader=None) -> dict
def run_timesfm_backtest(tickers, *, covariates="none"|"top"|"all", top_k=5, stride=15,
                         context_len=512, backend="timesfm3", max_origins=1000, transform="logprice",
                         cost_bps=10.0, threshold=0.55, start=None, end=None, aggregate_path=None,
                         out_dir=None, loader=None, forecaster=None, ...) -> pd.DataFrame  # runs
def run_predict(ticker, *, covariates="none", top_k=5, context_len=512, backend="timesfm3",
                ..., loader=None, forecaster=None) -> pd.DataFrame
def select_covariates(mode, *, available, aggregate, top_k=5, horizons=None, max_covariates=31)
def main(argv=None) -> int
```
`loader` (même signature que `data.load_universe`) et `forecaster` sont injectables : les tests
exécutent toute la chaîne hors ligne. Sorties :

* `reports/etat_des_lieux/` : `scores_<ACTIF>.csv`, `aggregate.csv`, `top_20.csv`,
  `bottom_20.csv`, `familles.csv`, `combined_summary.csv` (logit, hgb **et** baselines sur les
  mêmes barres), `combined_folds.csv`, `catalogue_indicateurs.csv`, `donnees.csv`,
  `timings.csv`, `run.json` (paramètres, coupures par actif, commit, versions), figures,
  `README.md` (synthèse + dictionnaire des colonnes). Un actif à la fois (mémoire) ; BTCUSDT
  chargé d'abord et réutilisé comme référence des altcoins.
* `reports/timesfm/` : par run `<actif>_<backend>_<covariables>_s<stride>_c<contexte>_<transformation>`,
  `_predictions.csv`, `_metrics.csv` (TimesFM, TimesFM recalibré, baselines ; `h_eff = ceil(h/stride)`,
  `acc_w`, Diebold-Mariano sur le Brier contre `reversal_h`), `_diagnostics.csv` (couverture
  [q10, q90], ρ avec le rendement des 60 min passées, IC partiel), `_strategy.csv`,
  `_accuracy.png` ; `runs.csv` (ajout / remplacement par run) et `README.md` régénéré.

## Temps et mémoire mesurés (VM 4 CPU partagée, 25/09/2026)

| Commande | Mesure |
|---|---|
| `study`, 1 actif × 365 jours (SOLUSDT, 167 indicateurs) | 175 s à 283 s selon la charge : chargement 3 s, indicateurs 9 s, scores 54 s, logit 73–76 s, hgb 31–136 s ; pic mémoire 2,4 Go |
| `study`, BTC + SOL × 30 jours | 65–75 s ; pic mémoire 0,5 Go |
| `study` complet, 6 actifs × 365 jours (extrapolation) | ≈ 18 min (6 × 175 s) sur machine libre, ≈ 30 min chargée ; mémoire ≈ 2,5 Go (un actif à la fois) ; `--no-combined` : ≈ 7 min |
| `timesfm-backtest`, TimesFM 3.0, contexte 512, moyenne symétrique | 6,5 à 11 origines/s sans covariable (3 horizons par appel) ; ≈ 2 origines/s avec 5 covariables ; chargement du modèle 4–5 s ; pic mémoire 3,1 Go |
| `predict` | ≈ 7 s (dont chargement du modèle) |

## Écarts au contrat initial (justifiés)

* `evaluation`, `forecaster`, `backtest` : colonnes et paramètres **en plus** du contrat (voir
  ci-dessus) ; aucun n'en retire. `prob_up` nomme son 2e paramètre `threshold` (même position
  que `last_value`). `baseline_predictions` rend un format long fusionnable sur (time, horizon).
* `backtest.direction_metrics` : `pt_hac_t` vaut NaN quand une des deux classes prévues compte
  moins de 10 origines (sinon t absurde : 2 « hausse » justes sur 98 donnaient t ≈ 10 pour une
  accuracy de 46 %).
* `timesfm-backtest` : origines dans la partie **test** par défaut ; avec `--covariates top|all`,
  le début est reculé après la fin de l'apprentissage de l'étude qui a produit `aggregate.csv`
  (`run.json`), pour que la sélection des covariables ne voie jamais la période évaluée.
  `--covariates all` est plafonné à 31 covariables (TimesFM 3.0 entraîné jusqu'à 32 variates,
  coût × (1 + k)). La P(hausse) recalibrée (isotonique, 1re moitié -> 2e moitié des origines)
  n'est calculée qu'avec au moins 300 labels d'apprentissage par horizon.
* Pas encore de « coffre-fort » (30 derniers jours réservés, méthodologie § 4.4) : à ajouter
  (option `--end` en attendant).

## Licence TimesFM — point d'attention

* **TimesFM 3.0** (août 2026, 330 M paramètres, covariables passées natives) :
  poids sous `timesfm-non-commercial-license-v1.0` → **recherche uniquement**,
  pas de trading avec de l'argent réel.
* **TimesFM 2.5** (200 M, Apache-2.0) : utilisable en production ; covariables via
  XReg (dépend de JAX).
