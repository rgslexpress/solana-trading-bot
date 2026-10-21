"""Métricas de desempenho. Resultados históricos NÃO indicam resultados futuros."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.config import TIMEFRAME_SECONDS

TRADE_COLUMNS = [
    "symbol", "timeframe", "entry_ts", "exit_ts", "qty", "entry_ref", "entry_fill", "exit_ref", "exit_fill",
    "gross_pnl", "fees", "slippage_cost", "net_pnl", "ret_pct", "bars_held", "exit_reason", "condition",
]


def _f(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else float(x)


def _group(trades: pd.DataFrame, col: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, g in trades.groupby(col):
        out[str(key)] = {
            "trades": int(len(g)),
            "net_pnl": _f(g["net_pnl"].sum()),
            "win_rate": _f((g["net_pnl"] > 0).mean()),
            "avg_net_pnl": _f(g["net_pnl"].mean()),
        }
    return out


def compute_metrics(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    initial: float,
    timeframe: str,
    benchmark_return_pct: float | None = None,
) -> dict[str, Any]:
    """`equity`: colunas equity, exposure indexadas por tempo (uma linha por candle).

    Sharpe/Sortino: retornos simples por candle, taxa livre de risco = 0, anualizados por
    sqrt(candles por ano) (365 dias, mercado 24/7). Sortino usa desvio apenas dos retornos < 0
    (alvo 0). São None quando há menos de 30 pontos ou desvio nulo."""
    final = float(equity["equity"].iloc[-1]) if len(equity) else initial
    m: dict[str, Any] = {
        "initial_equity": initial,
        "final_equity": final,
        "net_return": final - initial,
        "net_return_pct": (final / initial - 1) * 100 if initial else None,
        "num_trades": int(len(trades)),
    }
    if len(trades):
        gross_ret = float(trades["gross_pnl"].sum())
        wins, losses = trades[trades["net_pnl"] > 0], trades[trades["net_pnl"] <= 0]
        gp, gl = float(wins["net_pnl"].sum()), float(-losses["net_pnl"].sum())
        m.update(
            gross_return=gross_ret,
            win_rate=len(wins) / len(trades),
            avg_win=_f(wins["net_pnl"].mean()) if len(wins) else None,
            avg_loss=_f(losses["net_pnl"].mean()) if len(losses) else None,
            profit_factor=_f(gp / gl) if gl > 0 else None,
            expectancy_per_trade=_f(trades["net_pnl"].mean()),
            avg_bars_held=_f(trades["bars_held"].mean()),
            total_costs=_f(trades["fees"].sum() + trades["slippage_cost"].sum()),
            total_fees=_f(trades["fees"].sum()),
            total_slippage=_f(trades["slippage_cost"].sum()),
            by_token=_group(trades, "symbol"),
            by_timeframe=_group(trades, "timeframe"),
            by_condition=_group(trades, "condition"),
            by_exit_reason=_group(trades, "exit_reason"),
        )
    else:
        m.update(gross_return=0.0, win_rate=None, profit_factor=None, expectancy_per_trade=None, total_costs=0.0)
    if len(equity) > 1:
        eq = equity["equity"]
        peak = eq.cummax()
        m["max_drawdown_pct"] = float(((eq - peak) / peak).min() * 100)
        rets = eq.pct_change().dropna()
        per_year = 365 * 86400 / TIMEFRAME_SECONDS[timeframe]
        if len(rets) >= 30 and rets.std(ddof=1) > 0:
            m["sharpe"] = float(rets.mean() / rets.std(ddof=1) * math.sqrt(per_year))
            down = rets[rets < 0]
            dd = math.sqrt(float((np.minimum(rets, 0) ** 2).mean()))
            m["sortino"] = float(rets.mean() / dd * math.sqrt(per_year)) if len(down) >= 5 and dd > 0 else None
        else:
            m["sharpe"] = m["sortino"] = None
        m["avg_exposure_pct"] = float((equity["exposure"] / eq).mean() * 100)
        m["max_exposure_pct"] = float((equity["exposure"] / eq).max() * 100)
    else:
        m.update(max_drawdown_pct=0.0, sharpe=None, sortino=None, avg_exposure_pct=0.0, max_exposure_pct=0.0)
    m["benchmark_buy_hold_return_pct"] = benchmark_return_pct
    m["methodology"] = "Sharpe/Sortino: retornos por candle, rf=0, anualização sqrt(candles/ano); custos incluem taxas+slippage"
    return m
