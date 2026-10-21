"""Utilitários de teste que funcionam com pytest e com o runner de contingência (sem pytest)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import numpy as np
import pandas as pd

from app.config import Settings
from app.domain import TokenInfo

SOL = TokenInfo("SOL", "So11111111111111111111111111111111111111112", 9)
USDC = TokenInfo("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6)
JUP = TokenInfo("JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6, pool="pool1")


@contextmanager
def raises(exc: type[BaseException], match: str | None = None) -> Iterator[None]:
    try:
        yield
    except exc as e:
        if match and match.lower() not in str(e).lower():
            raise AssertionError(f"mensagem {str(e)!r} não contém {match!r}") from e
        return
    raise AssertionError(f"esperava {exc.__name__}")


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def make_candles(closes: list[float] | np.ndarray, tf_s: int = 900, start: str = "2025-01-01", vol: float = 1000.0) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(opens, closes) * 1.002
    low = np.minimum(opens, closes) * 0.998
    idx = pd.date_range(start, periods=len(closes), freq=f"{tf_s}s", tz="UTC")
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": closes, "volume": np.full(len(closes), vol)}, index=idx)


def default_settings() -> Settings:
    return Settings()


class ArrayProvider:
    """Provedor com candles pré-montados (determinístico) para testes de integração."""

    name = "array"

    def __init__(self, entry: pd.DataFrame, htf: pd.DataFrame, liquidity: float | None = 5e6):
        self.frames = {"15m": entry, "1h": htf}
        self.liq = liquidity
        self.fail = False

    def get_candles(self, token, timeframe, limit, now):
        from app.data.providers import DataProviderError

        if self.fail:
            raise DataProviderError("indisponível (teste)")
        df = self.frames[timeframe]
        return df[df.index <= pd.Timestamp(now)].tail(limit)

    def liquidity_usd(self, token, now):
        return self.liq

    def latest_price(self, token, now):
        df = self.frames["15m"]
        df = df[df.index <= pd.Timestamp(now)]
        return float(df["close"].iloc[-1]) if len(df) else None


def trend_series(n_flat=160, n_up=60, n_down=0, slope=0.004):
    flat = 100 + np.sin(np.arange(n_flat) / 5.0) * 0.5
    up = flat[-1] * np.cumprod(np.full(n_up, 1 + slope))
    parts = [flat, up]
    if n_down:
        parts.append(up[-1] * np.cumprod(np.full(n_down, 1 - slope)))
    return np.concatenate(parts)
