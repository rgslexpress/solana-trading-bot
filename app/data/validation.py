"""Validação e saneamento de candles. Nunca inventa dados ausentes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


def closed_only(df: pd.DataFrame, tf_seconds: int, now: datetime, grace_s: float = 0.0) -> pd.DataFrame:
    """Remove o candle em formação: só candles com (abertura + tf + grace) <= now.
    `grace_s` dá tempo ao provedor de consolidar as últimas negociações do candle recém-fechado."""
    if df.empty:
        return df
    close_time = df.index + pd.Timedelta(seconds=tf_seconds + grace_s)
    return df[close_time <= pd.Timestamp(now)]


def validate_candles(
    df: pd.DataFrame,
    tf_seconds: int,
    now: datetime,
    max_staleness_candles: float = 2.5,
    min_rows: int = 1,
    max_gap_candles: float = 1,
    gap_window: int | None = None,
) -> ValidationResult:
    """Valida candles JÁ fechados. `ok=False` => a estratégia deve parar para esse ativo."""
    issues: list[str] = []
    if df is None or df.empty:
        return ValidationResult(False, ["sem dados"])
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        issues.append("índice deve ser DatetimeIndex com fuso UTC")
        return ValidationResult(False, issues)
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        return ValidationResult(False, [f"colunas ausentes: {missing}"])
    if len(df) < min_rows:
        issues.append(f"histórico insuficiente: {len(df)} < {min_rows}")
    if df.index.has_duplicates:
        issues.append("candles duplicados")
    if not df.index.is_monotonic_increasing:
        issues.append("candles fora de ordem cronológica")
    vals = df[OHLCV].to_numpy(dtype=float)
    if not np.isfinite(vals).all():
        issues.append("valores não finitos (NaN/inf)")
    else:
        if (df[["open", "high", "low", "close"]] <= 0).any().any():
            issues.append("preços não positivos")
        if (df["volume"] < 0).any():
            issues.append("volume negativo")
        if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any() or (
            df["low"] > df[["open", "close", "high"]].min(axis=1)
        ).any():
            issues.append("OHLC inconsistente (high/low)")
    if len(df) > 1 and df.index.is_monotonic_increasing and not df.index.has_duplicates:
        gaps = df.index.to_series().diff().dropna().dt.total_seconds() / tf_seconds
        recent = gaps.tail(gap_window) if gap_window else gaps  # só a janela usada pelos indicadores
        if (recent > max_gap_candles + 1e-9).any():
            issues.append(f"lacuna temporal (candles ausentes; tolerância {max_gap_candles:g} candle(s) de espaçamento)")
        if (gaps < 1 - 1e-9).any():
            issues.append("espaçamento menor que o timeframe")
    if tf_seconds >= 60 and ((df.index.astype("int64") // 10**9) % tf_seconds != 0).any():
        issues.append("timestamps desalinhados com o timeframe (abertura não é múltiplo do período)")
    last_close = df.index[-1] + pd.Timedelta(seconds=tf_seconds)
    age = (pd.Timestamp(now) - last_close).total_seconds() / tf_seconds
    if age > max_staleness_candles:
        issues.append(f"dados desatualizados ({age:.1f} candles)")
    if last_close > pd.Timestamp(now):
        issues.append("candle ainda não fechado presente")
    return ValidationResult(not issues, issues)


def resample_ohlcv(df: pd.DataFrame, tf_seconds: int) -> pd.DataFrame:
    """Agrega candles menores em maiores (abertura=primeiro, máx, mín, fechamento=último, soma de volume).

    Candles de destino incompletos (faltam sub-candles) são descartados."""
    rule = f"{tf_seconds}s"
    g = df.resample(rule, label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "n": g["close"].count(),
        }
    )
    base = int(df.index.to_series().diff().dropna().dt.total_seconds().median())
    expected = tf_seconds // base
    out = out[out["n"] == expected].drop(columns="n")
    return out
