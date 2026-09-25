"""Configuration partagée par tous les modules.

Les horizons sont exprimés en *barres* : avec des barres de 1 minute,
(5, 10, 15) signifie « dans 5, 10 et 15 minutes ». Avec des barres
journalières, le même code prédit à 5, 10 et 15 jours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = ROOT / "reports"

HORIZONS: tuple[int, ...] = (5, 10, 15)
DEFAULT_INTERVAL = "1m"

# Univers par défaut : actions/ETF US très liquides + grandes capitalisations du CAC 40.
US_TICKERS: tuple[str, ...] = (
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM",
)
FR_TICKERS: tuple[str, ...] = (
    "MC.PA", "TTE.PA", "SAN.PA", "AIR.PA", "BNP.PA", "OR.PA",
)
DEFAULT_TICKERS: tuple[str, ...] = US_TICKERS + FR_TICKERS

# Référence de marché pour la force relative (seul le cours de clôture est utilisé :
# ^FCHI n'a pas de volume sur Yahoo mais toutes ses barres 1 minute existent).
BENCHMARKS: dict[str, str] = {"US": "SPY", "FR": "^FCHI"}


def market_of(ticker: str) -> str:
    """Renvoie "FR" pour les tickers Euronext Paris (suffixe .PA), "US" sinon."""
    return "FR" if ticker.upper().endswith(".PA") else "US"


def benchmark_for(ticker: str) -> str | None:
    """Ticker de référence du marché de `ticker`, ou None si c'est lui-même."""
    bench = BENCHMARKS[market_of(ticker)]
    return None if bench.upper() == ticker.upper() else bench


@dataclass(frozen=True)
class TimesFMConfig:
    # "timesfm3" : TimesFM 3.0 (poids sous licence NON commerciale, recherche uniquement).
    # "timesfm2p5" : TimesFM 2.5 (poids Apache-2.0, utilisable en production).
    backend: str = "timesfm3"
    context_len: int = 512
    batch_size: int = 32
    device: str = "cpu"
    # "logprice" | "price" | "cumret" — voir forecaster.transform_context.
    context_transform: str = "logprice"


@dataclass(frozen=True)
class StudyConfig:
    tickers: tuple[str, ...] = DEFAULT_TICKERS
    interval: str = DEFAULT_INTERVAL
    days: int = 29
    horizons: tuple[int, ...] = HORIZONS
    # Part chronologique utilisée pour « apprendre » le signe d'un indicateur ;
    # le reste sert d'échantillon hors-échantillon (out-of-sample).
    train_frac: float = 0.6
    # Coût aller-retour (en points de base) pour la mini-stratégie de backtest.
    cost_bps: float = 2.0
    timesfm: TimesFMConfig = field(default_factory=TimesFMConfig)
