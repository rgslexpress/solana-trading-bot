"""Indicadores técnicos causais (o valor em t usa apenas dados até t)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, period: int) -> pd.Series:
    """EMA com `adjust=False`; os primeiros `period-1` valores são NaN (aquecimento)."""
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def wilder_smooth(s: pd.Series, period: int) -> pd.Series:
    """Suavização de Wilder: semente = média simples dos `period` primeiros valores válidos,
    depois avg = (avg_anterior*(period-1) + x)/period. Causal."""
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    valid = np.flatnonzero(~np.isnan(vals))
    if len(valid) < period:
        return pd.Series(out, index=s.index)
    start = valid[period - 1]
    prev = vals[valid[:period]].mean()
    out[start] = prev
    for i in range(start + 1, len(vals)):
        x = vals[i]
        if np.isnan(x):
            continue  # não inventa dados: mantém NaN nessa posição
        prev = (prev * (period - 1) + x) / period
        out[i] = prev
    return pd.Series(out, index=s.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gain.where(delta.notna()), period)
    avg_loss = wilder_smooth(loss.where(delta.notna()), period)
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # perda média zero => RSI 100 (se houve ganho); ambos zero => 50
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR de Wilder."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1, skipna=False)
    return wilder_smooth(tr, period)


def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume atual / média dos `period` candles ANTERIORES (exclui o atual)."""
    base = volume.shift(1).rolling(period, min_periods=period).mean()
    return volume / base.replace(0.0, np.nan)


def rolling_return_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    """Correlação móvel entre retornos simples de duas séries alinhadas pelo índice."""
    ra = a.pct_change(fill_method=None)
    rb = b.reindex(a.index).pct_change(fill_method=None)
    return ra.rolling(window, min_periods=window).corr(rb)
