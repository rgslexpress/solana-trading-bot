"""Auditoria do simulador com candles FABRICADOS (resultado conhecido), não com dados aleatórios."""
import numpy as np
import pandas as pd

from app.backtest.engine import BacktestEngine, TokenData
from app.backtest.metrics import TRADE_COLUMNS, compute_metrics
from app.config import Settings
from _helpers import JUP, make_candles, trend_series

BUY_BAR = 127  # nos dados fabricados o sinal de compra ocorre no fechamento deste candle


def _settings(**risk):
    s = Settings()
    s.strategy.ema_trend = 20
    for k, v in risk.items():
        setattr(s.risk, k, v)
    return s


def _data(mutate=None, n=160):
    entry = make_candles(trend_series(n_flat=160, n_up=60), 900)
    htf = make_candles(np.linspace(80, 140, 240), 3600)
    if mutate:
        mutate(entry)
    return {"JUP": TokenData(JUP, entry, htf)}


def _buy_row(r):
    assert len(r.trades) >= 1, r.signal_counts
    return r.trades.iloc[0]


def test_entry_happens_at_open_after_signal_bar_never_at_signal_close():
    data = _data()
    r = BacktestEngine(_settings(), data, 5e6).run()
    t = _buy_row(r)
    entry_ts = pd.Timestamp(t["entry_ts"])
    assert entry_ts == data["JUP"].entry.index[BUY_BAR + 1]  # candle seguinte ao do sinal
    assert abs(t["entry_ref"] - data["JUP"].entry["open"].iloc[BUY_BAR + 1]) < 1e-12


def test_stop_without_gap_fills_at_stop_and_loss_matches_planned_risk():
    def crash(e):  # candle após a entrada cai 4% sem gap (abre no fechamento anterior)
        i = BUY_BAR + 2
        e.iloc[i, e.columns.get_loc("close")] = e["close"].iloc[i - 1] * 0.96
        e.iloc[i, e.columns.get_loc("low")] = e["close"].iloc[i] * 0.998
        e.iloc[i + 1 :, e.columns.get_loc("open")] = e["close"].iloc[i]
        e.iloc[i + 1 :, e.columns.get_loc("close")] = e["close"].iloc[i]
        e.iloc[i + 1 :, e.columns.get_loc("high")] = e["close"].iloc[i] * 1.001
        e.iloc[i + 1 :, e.columns.get_loc("low")] = e["close"].iloc[i] * 0.999

    s = _settings(max_token_exposure_pct=100.0, max_total_exposure_pct=100.0)  # sem teto de exposição: o tamanho é pelo risco
    data = _data(crash)
    r = BacktestEngine(s, data, 5e6).run()
    t = _buy_row(r)
    assert t["exit_reason"] == "stop"

    # o preço de saída de referência é o stop (sem gap), com slippage adverso depois
    assert t["exit_ref"] < t["entry_ref"] and t["exit_fill"] < t["exit_ref"]
    planned = 1000.0 * 0.5 / 100  # 0,5% do patrimônio
    assert 0.7 * planned < -t["net_pnl"] < 1.15 * planned, (t["net_pnl"], planned)  # perda no stop ≈ risco planejado (inclui custos)


def test_gap_through_stop_fills_at_open_not_at_stop():
    def gap(e):
        i = BUY_BAR + 2
        for col in ("open", "high", "low", "close"):
            e.iloc[i:, e.columns.get_loc(col)] = 90.0  # gap de ~10% abaixo do stop, sem negociação intermediária
        e.iloc[i:, e.columns.get_loc("high")] = 90.05
        e.iloc[i:, e.columns.get_loc("low")] = 89.95

    r = BacktestEngine(_settings(max_token_exposure_pct=100.0, max_total_exposure_pct=100.0), _data(gap), 5e6).run()
    t = _buy_row(r)
    assert t["exit_reason"] == "stop" and abs(t["exit_ref"] - 90.0) < 1e-9  # executa na abertura do gap, pior que o stop
    assert -t["net_pnl"] > 1.5 * 5.0  # perda MAIOR que o risco planejado: o gap é refletido (o limite não é garantia)


def test_execution_failures_degrade_or_change_results_and_are_reported():
    data = _data()
    ideal = BacktestEngine(_settings(), data, 5e6).run()
    s = _settings()
    s.backtest.execution_failure_rate = 0.5
    failing = BacktestEngine(s, data, 5e6).run()
    assert any("falha de execução simulada" in k for k in failing.rejection_counts)
    assert failing.metrics["num_trades"] <= ideal.metrics["num_trades"] + 1
    again = BacktestEngine(s, data, 5e6).run()
    assert again.metrics["final_equity"] == failing.metrics["final_equity"]  # determinístico por semente


def test_metrics_match_hand_computation():
    trades = pd.DataFrame([
        dict(symbol="A", timeframe="15m", entry_ts="t", exit_ts="t", qty=1, entry_ref=1, entry_fill=1, exit_ref=1, exit_fill=1,
             gross_pnl=g + f, fees=f, slippage_cost=0.0, net_pnl=g, ret_pct=0.0, bars_held=b, exit_reason="x", condition="c")
        for g, f, b in ((10.0, 1.0, 4), (-5.0, 1.0, 2), (20.0, 1.0, 6), (-5.0, 1.0, 4))
    ], columns=TRADE_COLUMNS)
    eq = pd.DataFrame({"equity": [1000, 1010, 1005, 1025, 1020] * 10, "exposure": [0, 100, 100, 100, 0] * 10},
                      index=pd.date_range("2025-01-01", periods=50, freq="15min", tz="UTC"))
    m = compute_metrics(trades, eq, 1000.0, "15m", 5.0)
    assert m["num_trades"] == 4 and m["win_rate"] == 0.5
    assert m["profit_factor"] == 30.0 / 10.0 and m["expectancy_per_trade"] == 5.0
    assert m["avg_win"] == 15.0 and m["avg_loss"] == -5.0 and m["avg_bars_held"] == 4.0
    assert m["total_fees"] == 4.0 and m["net_return"] == 20.0 and abs(m["net_return_pct"] - 2.0) < 1e-9
    assert abs(m["max_drawdown_pct"] - (1005 - 1010) / 1010 * 100) < 1e-6 or m["max_drawdown_pct"] < 0
    peak = eq["equity"].cummax()
    assert abs(m["max_drawdown_pct"] - ((eq["equity"] - peak) / peak).min() * 100) < 1e-9
    r = eq["equity"].pct_change().dropna()
    assert abs(m["sharpe"] - r.mean() / r.std(ddof=1) * np.sqrt(365 * 96)) < 1e-6  # anualização por candles de 15m
    assert m["benchmark_buy_hold_return_pct"] == 5.0


def test_sharpe_is_none_without_enough_points_and_profit_factor_none_without_losses():
    one = pd.DataFrame([dict(symbol="A", timeframe="15m", entry_ts="t", exit_ts="t", qty=1, entry_ref=1, entry_fill=1, exit_ref=1, exit_fill=1,
                             gross_pnl=3.0, fees=0.0, slippage_cost=0.0, net_pnl=3.0, ret_pct=0.0, bars_held=1, exit_reason="x", condition="c")], columns=TRADE_COLUMNS)
    eq = pd.DataFrame({"equity": [1000, 1003.0], "exposure": [0, 0]}, index=pd.date_range("2025-01-01", periods=2, freq="15min", tz="UTC"))
    m = compute_metrics(one, eq, 1000.0, "15m")
    assert m["sharpe"] is None and m["profit_factor"] is None
