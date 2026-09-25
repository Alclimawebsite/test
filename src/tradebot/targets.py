"""Cibles de prévision : rendement futur et direction à h barres."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd


def _sessions(df: pd.DataFrame) -> pd.Series:
    if "session" in df.columns:
        return df["session"]
    return pd.Series(df.index.date, index=df.index)


def future_log_return(df: pd.DataFrame, h: int, respect_sessions: bool = True) -> pd.Series:
    """log(close[t+h] / close[t]) ; NaN si t+h sort de la séance de t ou des données.

    Le décalage se fait en *barres* (lignes), pas en temps calendaire : une barre
    manquante dans les données décale donc l'horizon réel. `data.clean_ohlcv`
    ne comble pas les trous, ce qui est cohérent avec ce que verrait le bot en direct.
    """
    if h <= 0:
        raise ValueError("h doit être >= 1")
    log_close = np.log(df["close"].astype(float))
    ret = log_close.shift(-h) - log_close
    if respect_sessions:
        sess = _sessions(df)
        ret = ret.where(sess.shift(-h) == sess)
    return ret.rename(f"ret_{h}")


def direction_label(ret: pd.Series, eps: float = 0.0) -> pd.Series:
    """1.0 si ret > eps, 0.0 si ret < -eps, NaN sinon (égalité, zone neutre ou NaN)."""
    out = pd.Series(np.nan, index=ret.index, dtype=float)
    out[ret > eps] = 1.0
    out[ret < -eps] = 0.0
    return out


def make_targets(df: pd.DataFrame, horizons: Iterable[int] = (5, 10, 15),
                 eps: float = 0.0) -> pd.DataFrame:
    cols = {}
    for h in horizons:
        ret = future_log_return(df, h)
        cols[f"ret_{h}"] = ret
        cols[f"dir_{h}"] = direction_label(ret, eps)
    return pd.DataFrame(cols, index=df.index)
