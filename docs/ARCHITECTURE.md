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
  report.py      tableaux CSV/Markdown + graphiques dans reports/
  cli.py         commandes `tradebot fetch | study | timesfm-backtest | predict`
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
* Aucun appel réseau hors de `data.py` (et du téléchargement des poids TimesFM).

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
    # par (indicateur, horizon) pour UN actif : n, ic_spearman, ic_daily_mean, ic_daily_t,
    # ic_nw_t (Newey-West, lags=h), p_value, ic_train, ic_test, train_sign, hit_rate_oos,
    # auc_oos, n_oos, n_eff_oos (= n_oos/h), p_hit (binomial unilatéral sur n_eff)
def aggregate_scores(per_ticker: dict[str, pd.DataFrame]) -> pd.DataFrame
    # moyenne des IC entre actifs, t inter-actifs, % même signe, hit-rate OOS moyen,
    # p combinée, q_value (Benjamini-Hochberg) ; colonnes ic_train_mean pour une sélection sans fuite
def combined_model_walkforward(features, targets, horizons=(5, 10, 15),
                               n_splits=5, model="logit") -> pd.DataFrame
    # walk-forward expanding, purge de h barres ; "logit" | "hgb" ; proba de hausse OOS
def direction_summary(df_oos, horizons) -> pd.DataFrame
```
Grands volumes (≈ 525 000 barres par an et par actif) : code vectorisé, sous-échantillonnage
documenté si un calcul est trop lent (ex. Newey-West sur une barre sur `h`).

### forecaster.py
```python
@dataclass
class ForecastBatch:
    median: np.ndarray       # (n_series, horizon), espace du contexte transformé
    quantiles: np.ndarray    # (n_series, horizon, 9), déciles 0.1..0.9
    last_value: np.ndarray   # (n_series,)
    quantile_levels: tuple[float, ...]

class TimesFMForecaster:
    def __init__(self, cfg: TimesFMConfig = TimesFMConfig(), model=None)   # model injectable
    def forecast(self, contexts: list[np.ndarray], horizon: int,
                 covariates: list[np.ndarray | None] | None = None) -> ForecastBatch
def transform_context(prices: np.ndarray, kind: str) -> np.ndarray
def prob_up(quantiles_h, last_value, levels=(0.1, ..., 0.9)) -> np.ndarray   # borné [0.01, 0.99]
```

### backtest.py
```python
def walk_forward_timesfm(df, forecaster, horizons=(5, 10, 15), context_len=512, stride=5,
                         covariates: pd.DataFrame | None = None, start=None, end=None,
                         max_origins=None) -> pd.DataFrame
    # lignes : time, horizon, last_close, pred_median, p_up, ret, y_true
def baseline_predictions(df, origins, horizons) -> pd.DataFrame
    # momentum_h, reversal_h, always_up, majority_prev_day
def direction_metrics(y_true, p_up, h) -> dict
    # n, accuracy, balanced_accuracy, auc, brier, log_loss, p_binom (n_eff = n/h), pt_stat, pt_pvalue
def toy_strategy(preds, h, threshold=0.55, cost_bps=10.0) -> dict
    # trades non chevauchants long/short ; rendement brut et net moyen, nb trades, hit-rate
```

### report.py / cli.py
`report.py` écrit `reports/<étude>/*.csv`, `*.md`, `*.png`.
`cli.py` : `tradebot fetch`, `tradebot study`, `tradebot timesfm-backtest`, `tradebot predict`.

## Licence TimesFM — point d'attention

* **TimesFM 3.0** (août 2026, 330 M paramètres, covariables passées natives) :
  poids sous `timesfm-non-commercial-license-v1.0` → **recherche uniquement**,
  pas de trading avec de l'argent réel.
* **TimesFM 2.5** (200 M, Apache-2.0) : utilisable en production ; covariables via
  XReg (dépend de JAX).
