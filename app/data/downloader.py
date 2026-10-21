"""Baixa histórico de candles (GeckoTerminal, SOMENTE LEITURA) e grava CSV no formato do CsvProvider.

Não assina nada, não usa carteira/chaves. Contrato da API conforme a documentação consultada por busca
(`GET /networks/solana/pools/{pool}/ohlcv/{minute|hour|day}` com aggregate, limit<=1000, currency, token,
before_timestamp, include_empty_intervals) — NÃO VALIDADO contra a API real no ambiente de desenvolvimento.
Limite gratuito ~30 chamadas/min: há pausa entre as requisições e novas tentativas em HTTP 429."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import TIMEFRAME_SECONDS
from app.data.validation import closed_only, validate_candles
from app.domain import TokenInfo
from app.http import Transport, TransportError

UNIT = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15), "1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadReport:
    symbol: str
    timeframe: str
    path: str
    candles: int
    first: str | None
    last: str | None
    pages: int
    issues: list[str] = field(default_factory=list)
    liquidity_usd: float | None = None


def _get(transport: Transport, url: str, params: dict[str, Any], timeout: float, sleep: Callable[[float], None], retries: int = 3) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            r = transport.request("GET", url, params=params, headers={"Accept": "application/json"}, timeout=timeout)
        except TransportError as exc:
            if attempt >= retries:
                raise DownloadError(f"falha de rede: {exc}") from exc
            sleep(5.0 * (attempt + 1))
            continue
        if r.status == 429 or r.status >= 500:
            if attempt >= retries:
                raise DownloadError(f"provedor respondeu HTTP {r.status} repetidamente (limite de requisições?)")
            sleep(20.0 * (attempt + 1))
            continue
        if r.status != 200 or not isinstance(r.body, dict):
            raise DownloadError(f"resposta inesperada do provedor (HTTP {r.status})")
        return r.body
    raise DownloadError("tentativas esgotadas")


def pool_liquidity(transport: Transport, base_url: str, pool: str, timeout: float = 15.0, sleep: Callable[[float], None] = time.sleep) -> float | None:
    try:
        body = _get(transport, f"{base_url}/networks/solana/pools/{pool}", {}, timeout, sleep, retries=1)
        v = float(body["data"]["attributes"]["reserve_in_usd"])
        return v if v > 0 else None
    except (DownloadError, KeyError, TypeError, ValueError):
        return None


def download_history(
    transport: Transport,
    token: TokenInfo,
    timeframe: str,
    days: float,
    out_dir: str,
    *,
    now: datetime,
    base_url: str = "https://api.geckoterminal.com/api/v2",
    include_empty: bool = False,
    overwrite: bool = False,
    pause_s: float = 2.5,
    timeout: float = 15.0,
    max_pages: int = 400,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadReport:
    """Baixa `days` dias de candles FECHADOS, valida e grava `<out_dir>/<SIMBOLO>_<tf>.csv`.
    Recusa sobrescrever arquivo existente sem `overwrite=True` (nunca apaga dados sozinho)."""
    if timeframe not in UNIT:
        raise DownloadError(f"timeframe não suportado: {timeframe}")
    if not token.pool:
        raise DownloadError(f"{token.symbol}: defina 'pool' em config/tokens.json")
    path = Path(out_dir) / f"{token.symbol}_{timeframe}.csv"
    if path.exists() and not overwrite:
        raise DownloadError(f"{path} já existe (use --sobrescrever para substituir; nada foi alterado)")
    tf_s = TIMEFRAME_SECONDS[timeframe]
    unit, agg = UNIT[timeframe]
    url = f"{base_url}/networks/solana/pools/{token.pool}/ohlcv/{unit}"
    end_ts = int(now.timestamp()) // tf_s * tf_s  # só candles totalmente fechados
    start_ts = end_ts - int(days * 86400)
    rows: dict[int, list[float]] = {}
    before, pages = end_ts, 0
    while pages < max_pages:
        params: dict[str, Any] = {"aggregate": agg, "limit": 1000, "currency": "usd", "token": token.mint, "before_timestamp": before}
        if include_empty:
            params["include_empty_intervals"] = "true"
        body = _get(transport, url, params, timeout, sleep)
        pages += 1
        try:
            lst = body["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as exc:
            raise DownloadError("formato de resposta inesperado (ohlcv_list ausente)") from exc
        if not lst:
            break
        for r in lst:
            rows[int(r[0])] = [float(x) for x in r[1:6]]
        oldest = min(int(r[0]) for r in lst)
        if oldest >= before:  # sem progresso: evita laço infinito
            break
        before = oldest
        if oldest <= start_ts:
            break
        sleep(pause_s)
    if not rows:
        raise DownloadError(f"{token.symbol}/{timeframe}: o provedor não retornou candles")
    idx = sorted(t for t in rows if start_ts <= t < end_ts)
    df = pd.DataFrame([rows[t] for t in idx], columns=["open", "high", "low", "close", "volume"], index=pd.to_datetime(idx, unit="s", utc=True))
    df.index.name = "timestamp"
    closed = closed_only(df, tf_s, now)
    v = validate_candles(closed, tf_s, now, max_staleness_candles=2.5, min_rows=2, max_gap_candles=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = closed.copy()
    out.insert(0, "timestamp", (closed.index.astype("int64") // 10**9))
    out.to_csv(path, index=False)
    return DownloadReport(token.symbol, timeframe, str(path), len(closed), str(closed.index[0]) if len(closed) else None,
                          str(closed.index[-1]) if len(closed) else None, pages, v.issues)
