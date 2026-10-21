"""Separação temporal, sensibilidade de parâmetros e walk-forward.

Nenhum parâmetro é "otimizado" automaticamente para o relatório final: a seleção em `walk_forward`
usa SOMENTE a janela de treino e é avaliada na janela seguinte (fora da amostra)."""

from __future__ import annotations

import copy
import itertools
from typing import Any, Callable

import pandas as pd

from app.backtest.engine import BacktestEngine, BacktestResult, TokenData
from app.config import Settings


def split_periods(index: pd.DatetimeIndex, dev_fraction: float, val_fraction: float) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """Divide cronologicamente em desenvolvimento / validação / teste fora da amostra (OOS)."""
    n = len(index)
    a, b = int(n * dev_fraction), int(n * (dev_fraction + val_fraction))
    return {
        "desenvolvimento": (index[0], index[a]),
        "validacao": (index[a], index[b]),
        "fora_da_amostra": (index[b], index[-1] + (index[1] - index[0])),
    }


def run_period(settings: Settings, data: dict[str, TokenData], liquidity: float, start: pd.Timestamp, end: pd.Timestamp) -> BacktestResult:
    return BacktestEngine(settings, data, liquidity, start=start, end=end).run()


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """overrides: {'strategy.ema_fast': 8, 'risk.rr_target': 1.5}"""
    s = copy.deepcopy(settings)
    for path, val in overrides.items():
        section, name = path.split(".")
        setattr(getattr(s, section), name, val)
    return s


def sensitivity(
    settings: Settings, data: dict[str, TokenData], liquidity: float, grid: dict[str, list[Any]], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Executa o produto cartesiano da grade e retorna uma linha por combinação (para avaliar robustez)."""
    keys = list(grid)
    rows = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        ov = dict(zip(keys, combo))
        res = run_period(apply_overrides(settings, ov), data, liquidity, start, end)
        m = res.metrics
        rows.append({**ov, "trades": m["num_trades"], "net_return_pct": m["net_return_pct"], "profit_factor": m.get("profit_factor"),
                     "max_dd_pct": m["max_drawdown_pct"], "win_rate": m.get("win_rate")})
    return pd.DataFrame(rows)


def walk_forward(
    settings: Settings,
    data: dict[str, TokenData],
    liquidity: float,
    grid: dict[str, list[Any]],
    index: pd.DatetimeIndex,
    n_folds: int = 3,
    train_fraction: float = 0.6,
    min_train_trades: int = 5,
    log: Callable[[str], None] = lambda _m: None,
) -> pd.DataFrame:
    """Janelas deslizantes: seleciona na janela de treino (melhor retorno líquido com >= min_train_trades)
    e avalia na janela de teste imediatamente seguinte."""
    n = len(index)
    fold_len = n // (n_folds + 1) if n_folds > 0 else n
    keys = list(grid)
    out = []
    for k in range(n_folds):
        a = k * fold_len
        train_end = a + int(fold_len * (1 + train_fraction))
        test_end = min(train_end + fold_len, n - 1)
        if train_end >= n - 1:
            break
        tr = (index[a], index[train_end])
        te = (index[train_end], index[test_end])
        best, best_ret = None, None
        for combo in itertools.product(*[grid[k2] for k2 in keys]):
            ov = dict(zip(keys, combo))
            m = run_period(apply_overrides(settings, ov), data, liquidity, *tr).metrics
            if m["num_trades"] >= min_train_trades and (best_ret is None or m["net_return_pct"] > best_ret):
                best, best_ret = ov, m["net_return_pct"]
        if best is None:
            out.append({"fold": k, "escolha": None, "treino_ret_pct": None, "teste_ret_pct": None, "teste_trades": 0})
            continue
        mt = run_period(apply_overrides(settings, best), data, liquidity, *te).metrics
        log(f"fold {k}: {best}")
        out.append({"fold": k, "escolha": best, "treino_ret_pct": best_ret, "teste_ret_pct": mt["net_return_pct"], "teste_trades": mt["num_trades"]})
    return pd.DataFrame(out)
