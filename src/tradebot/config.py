"""Configuration partagée par tous les modules.

Priorité : les grosses cryptos (BTC, SOL, ETH…) cotées en USDT sur Binance.
Les horizons sont exprimés en *barres* : avec des barres de 1 minute,
(5, 10, 15) signifie « dans 5, 10 et 15 minutes ». Les actions (via Yahoo)
restent possibles en secondaire.
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

# Univers crypto : paires spot Binance contre USDT, les plus liquides.
CORE_CRYPTO: tuple[str, ...] = ("BTCUSDT", "SOLUSDT", "ETHUSDT")
EXTRA_CRYPTO: tuple[str, ...] = ("XRPUSDT", "BNBUSDT", "DOGEUSDT")
DEFAULT_TICKERS: tuple[str, ...] = CORE_CRYPTO + EXTRA_CRYPTO

# Secondaire : actions (Yahoo Finance), conservées pour comparaison.
US_TICKERS: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
FR_TICKERS: tuple[str, ...] = ("MC.PA", "TTE.PA", "AIR.PA")

# Référence de marché pour la force relative : BTC pour les altcoins,
# SPY / CAC 40 pour les actions (^FCHI n'a pas de volume sur Yahoo, seul le close sert).
BENCHMARKS: dict[str, str] = {"CRYPTO": "BTCUSDT", "US": "SPY", "FR": "^FCHI"}

_CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD")


def market_of(ticker: str) -> str:
    """"CRYPTO" pour une paire Binance (…USDT), "FR" pour Euronext Paris (.PA / ^FCHI), "US" sinon."""
    t = ticker.upper()
    if t.endswith(_CRYPTO_QUOTES):
        return "CRYPTO"
    if t.endswith(".PA") or t == "^FCHI":
        return "FR"
    return "US"


def is_crypto(ticker: str) -> bool:
    return market_of(ticker) == "CRYPTO"


def benchmark_for(ticker: str) -> str | None:
    """Ticker de référence du marché de `ticker`, ou None si c'est lui-même."""
    bench = BENCHMARKS[market_of(ticker)]
    return None if bench.upper() == ticker.upper() else bench


# Frais aller-retour (en points de base) utilisés pour juger la rentabilité.
# Binance USDⓈ-M futures, niveau VIP0 : taker 0,05 % par côté -> 10 pb aller-retour ;
# spot taker 0,10 % par côté -> 20 pb aller-retour (hors remise BNB, hors spread/slippage).
COST_BPS: dict[str, float] = {"futures_taker": 10.0, "spot_taker": 20.0, "futures_maker": 4.0}


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
    days: int = 365
    horizons: tuple[int, ...] = HORIZONS
    # Part chronologique utilisée pour « apprendre » (signe d'un indicateur, sélection
    # des covariables TimesFM, modèles combinés) ; le reste est l'échantillon de test.
    train_frac: float = 0.6
    # Coût aller-retour (pb) de la mini-stratégie de backtest : futures taker par défaut.
    cost_bps: float = COST_BPS["futures_taker"]
    timesfm: TimesFMConfig = field(default_factory=TimesFMConfig)
