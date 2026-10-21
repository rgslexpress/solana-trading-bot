"""Comando de backtest compartilhado pelo CLI e por scripts/run_backtest.py."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestResult, TokenData
from app.backtest.report import dataframe_hash, write_report
from app.backtest.robustness import run_period, sensitivity, split_periods, walk_forward
from app.config import Settings
from app.data.providers import CandleProvider
from app.data.validation import validate_candles
from app.config import TIMEFRAME_SECONDS


def load_history(s: Settings, provider: CandleProvider, tokens: dict[str, Any]) -> tuple[dict[str, TokenData], dict[str, str]]:
    if not hasattr(provider, "full_history"):
        raise RuntimeError("o provedor configurado não oferece histórico completo; use data.source=csv ou synthetic")
    sp = s.strategy
    out: dict[str, TokenData] = {}
    hashes: dict[str, str] = {}
    for sym, tok in tokens.items():
        entry = provider.full_history(tok, sp.entry_timeframe)  # type: ignore[attr-defined]
        htf = provider.full_history(tok, sp.htf_timeframe)  # type: ignore[attr-defined]
        for name, df, tf in (("entrada", entry, sp.entry_timeframe), ("superior", htf, sp.htf_timeframe)):
            end = df.index[-1].to_pydatetime() + pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf])
            v = validate_candles(df, TIMEFRAME_SECONDS[tf], end, max_staleness_candles=1.01, min_rows=2, max_gap_candles=s.data.max_gap_candles)
            if not v.ok:
                raise RuntimeError(f"dados inválidos para {sym}/{name}: {v.issues}")
        out[sym] = TokenData(tok, entry, htf)
        hashes[sym] = dataframe_hash(entry) + "/" + dataframe_hash(htf)
    return out, hashes


def run_backtest_command(s: Settings, provider: CandleProvider, tokens: dict[str, Any], liquidity_usd: float, with_robustness: bool = False) -> dict[str, str]:
    data, hashes = load_history(s, provider, tokens)
    first = next(iter(data.values())).entry
    periods = split_periods(first.index, s.backtest.dev_fraction, s.backtest.val_fraction)
    results: dict[str, BacktestResult] = {n: run_period(s, data, liquidity_usd, a, b) for n, (a, b) in periods.items()}
    extra: dict[str, Any] = {}
    if with_robustness:
        grid = {"strategy.ema_fast": [7, 9, 12], "risk.rr_target": [1.5, 2.0, 3.0]}
        extra["Sensibilidade de parâmetros (período de validação)"] = sensitivity(s, data, liquidity_usd, grid, *periods["validacao"])
        extra["Walk-forward (3 janelas)"] = walk_forward(s, data, liquidity_usd, grid, first.index, n_folds=3)
    info = {
        "fonte": s.data.source,
        "tokens": list(tokens),
        "timeframes": [s.strategy.entry_timeframe, s.strategy.htf_timeframe],
        "periodos": {k: [str(a), str(b)] for k, (a, b) in periods.items()},
        "hash_dos_dados": hashes,
        "liquidez_assumida_usd": liquidity_usd,
        "aviso_dados": "SINTÉTICOS (não representam mercado real)" if s.data.source == "synthetic" else "dados do provedor/CSV configurado",
    }
    params = {"strategy": asdict(s.strategy), "risk": asdict(s.risk), "execution": asdict(s.execution), "backtest": asdict(s.backtest)}
    return write_report(s.reports_dir, "backtest", results, params, info, extra)
