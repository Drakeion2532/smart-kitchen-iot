"""Streaming features. Everything is computed from the PAST only (rolling
windows), so the same code works on the live stream and offline."""
from __future__ import annotations

import numpy as np
import pandas as pd

SENSORS = ["temp_c", "humidity", "lpg_ppm", "aq_ppm"]


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    for s in SENSORS:
        x = df[s].astype(float)
        f[s] = x
        f[f"{s}_mean60"] = x.rolling(60, min_periods=1).mean()
        f[f"{s}_slope30"] = (x - x.shift(30)).fillna(0) / 30
        f[f"{s}_slope120"] = (x - x.shift(120)).fillna(0) / 120
        f[f"{s}_std60"] = x.rolling(60, min_periods=2).std().fillna(0)
    # temperature above the slow-moving room baseline (≈ heat from stove/fire)
    f["temp_above_base"] = df["temp_c"] - df["temp_c"].rolling(1800, min_periods=1).min()
    f["lpg_above_base"] = df["lpg_ppm"] - df["lpg_ppm"].rolling(1800, min_periods=1).min()
    m = df["motion"].astype(int)
    f["motion_60"] = m.rolling(60, min_periods=1).sum()
    f["motion_300"] = m.rolling(300, min_periods=1).sum()
    last = pd.Series(np.where(m > 0, np.arange(len(m)), np.nan), index=df.index).ffill()
    f["secs_since_motion"] = (np.arange(len(m)) - last).fillna(9999).clip(upper=9999)
    f["flame_10"] = df["flame"].astype(int).rolling(10, min_periods=1).sum()
    return f


def rule_based(df: pd.DataFrame) -> pd.Series:
    """The firmware's fixed-threshold logic, for comparison."""
    out = pd.Series("normal", index=df.index)
    f = add_features(df)
    out[(f["temp_above_base"] >= 4)] = "cooking"
    out[(f["temp_above_base"] >= 4) & (f["secs_since_motion"] > 300)] = "unattended"
    out[df["lpg_ppm"] >= 1000] = "gas_leak"
    out[(df["flame"] == 1) | ((df["temp_c"] >= 45) & (df["aq_ppm"] >= 1000))] = "fire"
    return out
