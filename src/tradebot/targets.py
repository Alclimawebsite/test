"""Cibles de prévision : rendement futur et direction à h barres."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def bar_duration(index: pd.DatetimeIndex) -> pd.Timedelta:
    """Durée nominale d'une barre = écart le plus fréquent entre deux horodatages."""
    if len(index) < 2:
        raise ValueError("au moins deux barres sont nécessaires")
    return pd.Series(index).diff().mode().iloc[0]


def future_log_return(df: pd.DataFrame, h: int, bar: pd.Timedelta | None = None) -> pd.Series:
    """log(close[t+h] / close[t]) ; NaN si la barre t+h n'est pas exactement h barres plus tard.

    Le décalage se fait en lignes puis on exige que le temps écoulé vaille h * durée
    d'une barre. Cette règle unique couvre :
    * la crypto (24/7) : une panne / maintenance de l'exchange crée un trou -> cible NaN ;
    * les actions : la nuit et le week-end sont des trous -> la cible ne traverse jamais
      la clôture ; une barre manquante en séance invalide aussi la cible.
    """
    if h <= 0:
        raise ValueError("h doit être >= 1")
    if bar is None:
        bar = bar_duration(df.index)
    log_close = np.log(df["close"].astype(float))
    ret = log_close.shift(-h) - log_close
    ts = pd.Series(df.index, index=df.index)
    elapsed = ts.shift(-h) - ts
    return ret.where(elapsed == h * bar).rename(f"ret_{h}")


def direction_label(ret: pd.Series, eps: float = 0.0) -> pd.Series:
    """1.0 si ret > eps, 0.0 si ret < -eps, NaN sinon (égalité, zone neutre ou NaN)."""
    out = pd.Series(np.nan, index=ret.index, dtype=float)
    out[ret > eps] = 1.0
    out[ret < -eps] = 0.0
    return out


def make_targets(df: pd.DataFrame, horizons: Iterable[int] = (5, 10, 15),
                 eps: float = 0.0) -> pd.DataFrame:
    """Colonnes ret_{h} et dir_{h} pour chaque horizon."""
    bar = bar_duration(df.index)
    cols = {}
    for h in horizons:
        ret = future_log_return(df, h, bar)
        cols[f"ret_{h}"] = ret
        cols[f"dir_{h}"] = direction_label(ret, eps)
    return pd.DataFrame(cols, index=df.index)
