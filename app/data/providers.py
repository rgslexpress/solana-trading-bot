"""Provedores de candles (desacoplados da estratégia).

Todos retornam DataFrame indexado por abertura do candle (UTC) com colunas open/high/low/close/volume.
O candle em formação PODE vir no resultado; use `validation.closed_only` antes de gerar sinais.

* SyntheticProvider  — dados SINTÉTICOS determinísticos (testes/demonstração; NÃO são mercado real).
* CsvProvider        — arquivos `<dir>/<SIMBOLO>_<tf>.csv` (timestamp,open,high,low,close,volume).
* GeckoTerminalProvider — OHLCV público por pool. Endpoint e formato NÃO foram verificados neste
  ambiente (sem acesso à rede); valide em `scripts/check_environment.py` antes de usar.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.config import TIMEFRAME_SECONDS
from app.domain import TokenInfo


class DataProviderError(RuntimeError):
    pass


class CandleProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_candles(self, token: TokenInfo, timeframe: str, limit: int, now: datetime) -> pd.DataFrame:
        """Retorna até `limit` candles com abertura <= now (o último pode estar em formação)."""

    def liquidity_usd(self, token: TokenInfo, now: datetime) -> float | None:
        """Liquidez estimada do ativo em USD, ou None se desconhecida (=> não operar)."""
        return None

    def latest_price(self, token: TokenInfo, now: datetime, max_age_s: float = 300.0) -> float | None:
        """Último preço de candles de 1m; None se vazio ou mais velho que `max_age_s` (preço velho não protege stop)."""
        df = self.get_candles(token, "1m", 2, now)
        if df.empty:
            return None
        age = (_utc(now) - (df.index[-1] + pd.Timedelta(seconds=60))).total_seconds()
        return float(df["close"].iloc[-1]) if age <= max_age_s else None


def _utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class SyntheticProvider(CandleProvider):
    """Gera um passeio aleatório geométrico com regimes de tendência (determinístico por semente)."""

    name = "synthetic"

    def __init__(self, seed: int = 7, start: str = "2025-01-01", n_1m: int = 60 * 24 * 90, liquidity: float = 5e6):
        self.seed, self.start, self.n_1m, self._liq = seed, pd.Timestamp(start, tz="UTC"), n_1m, liquidity
        self._cache: dict[str, pd.DataFrame] = {}

    def _base(self, token: TokenInfo) -> pd.DataFrame:
        if token.symbol in self._cache:
            return self._cache[token.symbol]
        rng = np.random.default_rng(self.seed + sum(map(ord, token.symbol)))
        n = self.n_1m
        # regimes de ~2 dias com deriva alternada
        regime = np.repeat(rng.choice([-1.0, 0.0, 1.0], size=n // 2880 + 1, p=[0.3, 0.3, 0.4]), 2880)[:n]
        mu = regime * 4e-5
        ret = mu + rng.normal(0, 6e-4, size=n)
        close = 100.0 * np.exp(np.cumsum(ret))
        open_ = np.concatenate([[100.0], close[:-1]])
        spread = np.abs(rng.normal(0, 3e-4, size=n))
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)
        vol = rng.lognormal(mean=8.0, sigma=0.5, size=n) * (1 + np.abs(ret) * 800)
        idx = pd.date_range(self.start, periods=n, freq="1min", tz="UTC")
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
        self._cache[token.symbol] = df
        return df

    def full_history(self, token: TokenInfo, timeframe: str) -> pd.DataFrame:
        from app.data.validation import resample_ohlcv

        key = f"{token.symbol}|{timeframe}"
        if key not in self._cache:
            base = self._base(token)
            tf = TIMEFRAME_SECONDS[timeframe]
            self._cache[key] = base if tf == 60 else resample_ohlcv(base, tf)
        return self._cache[key]

    def get_candles(self, token: TokenInfo, timeframe: str, limit: int, now: datetime) -> pd.DataFrame:
        df = self.full_history(token, timeframe)
        df = df[df.index <= _utc(now)]
        return df.tail(limit)

    def liquidity_usd(self, token: TokenInfo, now: datetime) -> float | None:
        return self._liq


class CsvProvider(CandleProvider):
    name = "csv"

    def __init__(self, directory: str, liquidity_usd: float | None = None):
        self.dir = Path(directory)
        self._liq = liquidity_usd

    def _load(self, token: TokenInfo, timeframe: str) -> pd.DataFrame:
        path = self.dir / f"{token.symbol}_{timeframe}.csv"
        if not path.exists():
            raise DataProviderError(f"arquivo de candles não encontrado: {path}")
        df = pd.read_csv(path)
        if "timestamp" not in df.columns:
            raise DataProviderError("CSV precisa da coluna 'timestamp'")
        ts = df["timestamp"]
        if np.issubdtype(ts.dtype, np.number):
            idx = pd.to_datetime(ts, unit="s", utc=True)
        else:
            idx = pd.to_datetime(ts, utc=True)
        df = df.drop(columns="timestamp").set_index(idx)
        df.index.name = "timestamp"
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    def get_candles(self, token: TokenInfo, timeframe: str, limit: int, now: datetime) -> pd.DataFrame:
        df = self._load(token, timeframe)
        return df[df.index <= _utc(now)].tail(limit)

    def full_history(self, token: TokenInfo, timeframe: str) -> pd.DataFrame:
        return self._load(token, timeframe)

    def liquidity_usd(self, token: TokenInfo, now: datetime) -> float | None:
        return self._liq


class GeckoTerminalProvider(CandleProvider):
    """OHLCV por pool. Respeita cache (TTL) e um intervalo mínimo entre requisições.

    Contrato (conforme documentação da CoinGecko/GeckoTerminal consultada por busca; NÃO testado contra a API real):
    `GET /networks/solana/pools/{pool}/ohlcv/{minute|hour|day}?aggregate&limit<=1000&currency&token&include_empty_intervals`
    => `data.attributes.ohlcv_list = [[ts, o, h, l, c, v], ...]` em ordem decrescente. Intervalos sem negociação são OMITIDOS
    (gera lacunas => a validação rejeita). O volume é DA POOL, não consolidado do mercado. Limite gratuito ≈ 30 chamadas/min."""

    name = "geckoterminal"
    _UNIT = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15), "1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}

    def __init__(
        self,
        base_url: str,
        http_get: Callable[[str, dict[str, Any]], dict[str, Any]],
        cache_ttl_s: float = 30.0,
        min_interval_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        liquidity_cap: float = 0.0,
        include_empty_intervals: bool = False,
    ):
        self.include_empty = include_empty_intervals
        self.base_url, self._get, self.ttl, self.min_interval, self._clock = base_url, http_get, cache_ttl_s, min_interval_s, clock
        self.liquidity_cap = liquidity_cap
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._last_req = -1e9

    def get_candles(self, token: TokenInfo, timeframe: str, limit: int, now: datetime) -> pd.DataFrame:
        if not token.pool:
            raise DataProviderError(f"{token.symbol}: pool de referência não configurada")
        key = (token.mint, timeframe)
        hit = self._cache.get(key)
        t = self._clock()
        if hit and t - hit[0] < self.ttl:
            return hit[1][hit[1].index <= _utc(now)].tail(limit)
        wait = self.min_interval - (t - self._last_req)
        if wait > 0:
            time.sleep(wait)
        unit, agg = self._UNIT[timeframe]
        url = f"{self.base_url}/networks/solana/pools/{token.pool}/ohlcv/{unit}"
        # `token`=mint fixa a orientação do preço (token em USD), independente da posição na pool
        q: dict[str, Any] = {"aggregate": agg, "limit": min(limit, 1000), "currency": "usd", "token": token.mint}
        if self.include_empty:
            q["include_empty_intervals"] = "true"
        payload = self._get(url, q)
        self._last_req = self._clock()
        try:
            rows = payload["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as exc:
            raise DataProviderError("resposta de candles em formato inesperado") from exc
        if not rows:
            raise DataProviderError("provedor retornou zero candles")
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("timestamp"), unit="s", utc=True)
        df = df.astype(float).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        self._cache[key] = (self._clock(), df)
        return df[df.index <= _utc(now)].tail(limit)

    def liquidity_usd(self, token: TokenInfo, now: datetime) -> float | None:
        """Reserva em USD da pool (`data.attributes.reserve_in_usd`) — campo NÃO verificado neste ambiente.
        Se `override` > 0 for configurado, usa o MENOR entre o valor consultado e o override (conservador)."""
        if not token.pool:
            return None
        try:
            payload = self._get(f"{self.base_url}/networks/solana/pools/{token.pool}", {})
            val = float(payload["data"]["attributes"]["reserve_in_usd"])
        except Exception:  # noqa: BLE001 - qualquer falha => liquidez desconhecida => não operar
            return None
        if val <= 0:
            return None
        return min(val, self.liquidity_cap) if self.liquidity_cap else val


def collected_at() -> str:
    return datetime.now(timezone.utc).isoformat()
