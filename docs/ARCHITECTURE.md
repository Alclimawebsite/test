# Architecture — tradebot

Objectif de la phase 1 : **état des lieux des indicateurs** pour prévoir la
direction (hausse / baisse) d'une action à **5, 10 et 15 barres** (1 barre =
1 minute par défaut), et mesurer ce qu'apporte **Google TimesFM** par rapport
aux indicateurs classiques et à des baselines naïves.

```
src/tradebot/
  config.py      horizons, univers, chemins, TimesFMConfig, StudyConfig
  data.py        téléchargement OHLCV (yfinance), cache parquet, nettoyage, sessions
  indicators.py  catalogue d'indicateurs techniques strictement causaux + registre
  targets.py     rendements futurs et labels de direction à h barres (sans traverser la nuit)
  evaluation.py  pouvoir prédictif de chaque indicateur (IC, hit-rate OOS, AUC, FDR) + modèles combinés
  forecaster.py  wrapper TimesFM (3.0 et 2.5), probabilité de hausse depuis les quantiles
  backtest.py    walk-forward TimesFM vs baselines, métriques de direction, mini-stratégie
  report.py      tableaux CSV/Markdown + graphiques dans reports/
  cli.py         commandes `tradebot fetch | study | timesfm-backtest | predict`
```

## Conventions communes (contrat entre modules)

* **Format OHLCV** : `pd.DataFrame` indexé par un `DatetimeIndex` *tz-aware* dans le
  fuseau de la place de cotation (America/New_York ou Europe/Paris), trié, sans
  doublon, colonnes en minuscules `open, high, low, close, volume` (float64).
  Une colonne `session` (type `datetime.date`, date locale de la séance) est
  ajoutée par `data.add_session`.
* **Causalité** : toute valeur calculée au temps `t` n'utilise que les barres
  `<= t`. C'est testé automatiquement (tests/test_indicators.py : on tronque les
  données après `t` et on vérifie que la valeur en `t` est identique).
* **Horizon** `h` en barres. Rendement futur `r_h(t) = log(close[t+h] / close[t])`,
  **NaN si `t+h` n'est pas dans la même séance** (pas de gap overnight dans la cible)
  ou si `t+h` n'existe pas.
* **Direction** : `y = 1` si `r_h > eps`, `0` si `r_h < -eps`, NaN sinon
  (égalités / zone neutre exclues). `eps = 0` par défaut.
* Aucun module ne fait d'appel réseau en dehors de `data.py` (et du
  téléchargement des poids dans `forecaster.py`).

## API par module

### data.py
```python
def fetch_ohlcv(ticker: str, interval: str = "1m", days: int = 29,
                cache: bool = True, end: pd.Timestamp | None = None) -> pd.DataFrame
    # yfinance : 1m -> requêtes par tranches de 7 jours, max ~29 jours en arrière ;
    # 2m/5m/15m/30m -> 59 jours ; 60m/1h -> 729 jours ; 1d -> illimité.
    # Heures régulières uniquement (prepost=False), auto_adjust=True.
    # Supprime la dernière barre si elle est en cours (volume nul et < 1 intervalle).
    # Cache parquet dans data/cache/{ticker}_{interval}.parquet, fusionné avec les
    # nouvelles données (le 1m n'est disponible que 30 jours : le cache accumule l'historique).
def clean_ohlcv(df: pd.DataFrame, tz: str | None = None) -> pd.DataFrame
    # colonnes minuscules, suppression lignes NaN/prix <= 0, high>=max(o,c), low<=min(o,c)
def add_session(df: pd.DataFrame) -> pd.DataFrame        # ajoute la colonne `session`
def exchange_tz(ticker: str) -> str
def load_universe(tickers, interval="1m", days=29, cache=True) -> dict[str, pd.DataFrame]
```

### indicators.py
```python
@dataclass(frozen=True)
class IndicatorSpec:
    name: str            # identifiant unique snake_case, ex. "rsi_14"
    family: str          # "tendance" | "momentum" | "volatilite" | "volume" | "microstructure"
                         # | "statistique" | "calendrier" | "marche"
    description: str     # une phrase en français
    func: Callable[[pd.DataFrame], pd.Series]   # df OHLCV (+ `session`, + `bench_close` optionnel)
    needs_volume: bool = False
    needs_benchmark: bool = False

REGISTRY: dict[str, IndicatorSpec]
def compute_indicators(df: pd.DataFrame, names: Iterable[str] | None = None,
                       benchmark: pd.Series | None = None) -> pd.DataFrame
    # benchmark = série close de l'indice aligné (reindex + ffill *dans la séance*),
    # ignore silencieusement les indicateurs volume si le volume est nul partout.
def list_indicators() -> pd.DataFrame    # name, family, description, needs_volume, needs_benchmark
```
Les indicateurs sont **normalisés pour être comparables entre actions** (ratios au
prix, oscillateurs bornés, z-scores) : jamais un niveau de prix brut.

### targets.py
```python
def future_log_return(df: pd.DataFrame, h: int, respect_sessions: bool = True) -> pd.Series
def direction_label(ret: pd.Series, eps: float = 0.0) -> pd.Series          # 1.0 / 0.0 / NaN
def make_targets(df, horizons=(5, 10, 15), eps=0.0) -> pd.DataFrame
    # colonnes ret_{h} et dir_{h}
```

### evaluation.py
```python
def indicator_scores(features: pd.DataFrame, targets: pd.DataFrame,
                     horizons=(5, 10, 15), train_frac=0.6) -> pd.DataFrame
    # une ligne par (indicateur, horizon) pour UNE action :
    # n, ic_spearman (pooled), ic_daily_mean, ic_daily_t (IC par séance -> t-stat),
    # ic_nw_t (t-stat Newey-West, lags=h), p_value, train_sign,
    # hit_rate_oos (règle : up si (x - median_train) * train_sign > 0, évaluée sur le test),
    # auc_oos, n_oos, n_eff_oos (= n_oos / h), p_hit (binomial unilatéral sur n_eff)
def aggregate_scores(per_ticker: dict[str, pd.DataFrame]) -> pd.DataFrame
    # moyenne des IC entre actions, t-stat inter-actions, % d'actions de même signe,
    # hit-rate OOS moyen, q_value (Benjamini-Hochberg sur l'ensemble indicateurs x horizons)
def combined_model_walkforward(features, targets, horizons=(5, 10, 15),
                               n_splits=5, model="logit") -> pd.DataFrame
    # walk-forward expanding avec purge de h barres (embargo) entre train et test ;
    # modèles "logit" (StandardScaler + LogisticRegression L2) et "hgb"
    # (HistGradientBoostingClassifier) ; sortie : proba de hausse OOS par ligne
```

### forecaster.py
```python
@dataclass
class ForecastBatch:
    median: np.ndarray       # (n_series, horizon) dans l'espace du *contexte transformé*
    quantiles: np.ndarray    # (n_series, horizon, 9) déciles 0.1..0.9
    last_value: np.ndarray   # (n_series,) dernière valeur du contexte transformé
    quantile_levels: tuple[float, ...]

class TimesFMForecaster:
    def __init__(self, cfg: TimesFMConfig = TimesFMConfig(), model=None)   # model injectable (tests)
    def forecast(self, contexts: list[np.ndarray], horizon: int,
                 covariates: list[np.ndarray | None] | None = None) -> ForecastBatch
        # contexts : séries de *prix* ; transformées selon cfg.context_transform ;
        # covariates : (n_cov, len(context)) = indicateurs PASSÉS uniquement
        # (past_only_covariates de TimesFM 3.0), z-scorés sur la fenêtre de contexte.
def transform_context(prices: np.ndarray, kind: str) -> np.ndarray
def prob_up(quantiles_h: np.ndarray, last_value: np.ndarray,
            levels=(0.1, ..., 0.9)) -> np.ndarray
    # P(X_h > last_value) par interpolation linéaire de la CDF entre déciles,
    # extrapolation des queues bornée dans [0.01, 0.99].
```

### backtest.py
```python
def walk_forward_timesfm(df: pd.DataFrame, forecaster, horizons=(5, 10, 15),
                         context_len=512, stride=5, covariates: pd.DataFrame | None = None,
                         start: int | None = None) -> pd.DataFrame
    # origines t tous les `stride` barres, uniquement quand la cible d'au moins un
    # horizon est définie ; une seule prévision de horizon=max(horizons) par origine.
    # lignes : time, horizon, last_close, pred_median, p_up, ret, y_true
def baseline_predictions(df, origins, horizons) -> pd.DataFrame
    # "momentum_h" (signe du rendement des h dernières barres), "reversal_h",
    # "always_up", "prev_session_majority"
def direction_metrics(y_true, p_up, h) -> dict
    # n, accuracy, balanced_accuracy, auc, brier, log_loss, p_binom (n_eff = n/h),
    # pesaran_timmermann_stat, pt_pvalue
def toy_strategy(df_preds, h, threshold=0.55, cost_bps=2.0) -> dict
    # positions non chevauchantes : long si p_up>thr, short si p_up<1-thr,
    # rendement brut/net moyen par trade, nb trades, hit-rate
```

### report.py / cli.py
`report.py` écrit `reports/<étude>/*.csv`, `*.md` et `*.png`.
`cli.py` expose `tradebot fetch`, `tradebot study`, `tradebot timesfm-backtest`,
`tradebot predict` (prévision « live » de la direction à 5/10/15 barres).

## Licence TimesFM — point d'attention

* **TimesFM 3.0** (août 2026, 330 M paramètres, covariables passées natives) :
  poids sous `timesfm-non-commercial-license-v1.0` → **recherche uniquement**,
  pas de trading réel/production.
* **TimesFM 2.5** (200 M, Apache-2.0) : utilisable en production ; covariables
  via XReg (dépend de JAX).
