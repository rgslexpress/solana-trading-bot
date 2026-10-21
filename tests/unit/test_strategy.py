import numpy as np
import pandas as pd

from app.config import StrategyParams
from app.domain import SignalType
from app.strategy.signal_engine import ExecContext, compute_features, decide
from _helpers import make_candles

P = StrategyParams(ema_trend=20, min_candles=50)
OK_CTX = ExecContext(liquidity_usd=5e6, min_liquidity_usd=1e5, est_cost_bps=25.0)


def _trend_series(n_flat=160, n_up=60, n_down=0, slope=0.004):
    flat = 100 + np.sin(np.arange(n_flat) / 5.0) * 0.5
    up = flat[-1] * np.cumprod(np.full(n_up, 1 + slope))
    parts = [flat, up]
    if n_down:
        parts.append(up[-1] * np.cumprod(np.full(n_down, 1 - slope)))
    return np.concatenate(parts)


def _features(closes, htf_closes=None, p=P, vol=1000.0):
    entry = make_candles(closes, 900, vol=vol)
    htf = None
    if htf_closes is not None:
        htf = make_candles(htf_closes, 3600, vol=vol)
    return compute_features(entry, htf, p)


def _htf_up(n=200):
    return np.linspace(80, 140, n)


def test_no_trade_during_warmup():
    f = _features(np.linspace(100, 101, 10), _htf_up())
    d = decide(f.iloc[-1].to_dict(), P, OK_CTX, False)
    assert d.type == SignalType.NO_TRADE and "aquecimento" in d.rejections[0]


def test_buy_on_uptrend_cross_with_htf_confirmation():
    closes = _trend_series()
    f = _features(closes, _htf_up())
    buys = [i for i in range(len(f)) if decide(f.iloc[i].to_dict(), P, OK_CTX, False).type == SignalType.BUY]
    assert buys, "esperava ao menos um BUY no início da alta"


def test_htf_contrary_blocks_entry():
    closes = _trend_series()
    f_ok = _features(closes, _htf_up())
    f_bad = _features(closes, np.linspace(140, 80, 200))
    n_ok = sum(decide(f_ok.iloc[i].to_dict(), P, OK_CTX, False).type == SignalType.BUY for i in range(len(f_ok)))
    n_bad = sum(decide(f_bad.iloc[i].to_dict(), P, OK_CTX, False).type == SignalType.BUY for i in range(len(f_bad)))
    assert n_ok > 0 and n_bad == 0


def test_missing_htf_means_no_trade():
    f = _features(_trend_series(), None)
    types = {decide(f.iloc[i].to_dict(), P, OK_CTX, False).type for i in range(len(f))}
    assert SignalType.BUY not in types


def test_low_volume_and_low_liquidity_and_cost_block():
    closes = _trend_series()
    f = _features(closes, _htf_up())
    idx = [i for i in range(len(f)) if decide(f.iloc[i].to_dict(), P, OK_CTX, False).type == SignalType.BUY][0]
    row = f.iloc[idx].to_dict()
    assert decide({**row, "rel_vol": 0.1}, P, OK_CTX, False).type == SignalType.NO_TRADE
    low_liq = ExecContext(1000.0, 1e5, 25.0)
    d = decide(row, P, low_liq, False)
    assert d.type == SignalType.NO_TRADE and any("liquidez" in r for r in d.rejections)
    unknown = ExecContext(None, 1e5, 25.0)
    assert decide(row, P, unknown, False).type == SignalType.NO_TRADE
    costly = ExecContext(5e6, 1e5, 500.0)
    assert any("custo" in r for r in decide(row, P, costly, False).rejections)


def test_sell_on_trend_loss_and_hold_otherwise():
    closes = _trend_series(n_down=40)
    f = _features(closes, _htf_up())
    kinds = [decide(f.iloc[i].to_dict(), P, OK_CTX, True).type for i in range(len(f))]
    up_phase = kinds[185:219]  # alta consolidada: sem saída
    down_phase = kinds[225:]  # reversão
    assert SignalType.HOLD in up_phase and SignalType.SELL not in up_phase
    assert SignalType.SELL in down_phase


def test_no_lookahead_features_identical_when_future_removed():
    """O valor da feature em t não pode mudar se candles futuros forem removidos/alterados."""
    closes = _trend_series(n_down=40)
    entry = make_candles(closes, 900)
    htf = make_candles(_htf_up(240), 3600)
    full = compute_features(entry, htf, P)
    cut = 180
    part = compute_features(entry.iloc[:cut], htf, P)
    # HTF completo contém candles futuros, mas só os já fechados no instante t podem ser usados.
    cols = ["ema_fast", "ema_slow", "macd_hist", "rsi", "atr", "rel_vol", "htf_ema_trend"]
    a = full.iloc[:cut][cols].astype(float)
    b = part[cols].astype(float)
    assert np.allclose(a.fillna(-1).to_numpy(), b.fillna(-1).to_numpy())
    assert (full.iloc[:cut]["htf_up"].astype(str) == part["htf_up"].astype(str)).all()


def test_htf_candle_not_used_before_it_closes():
    entry = make_candles(np.linspace(100, 120, 40), 900)
    htf_closes = np.array([100.0, 100.0, 100.0, 1000.0])  # o 4º candle H1 (03:00-04:00) fecha às 04:00
    htf = make_candles(htf_closes, 3600)
    p = StrategyParams(ema_trend=2, min_candles=2)
    f = compute_features(entry, htf, p)
    # candles de entrada que fecham antes das 04:00 não podem enxergar o candle H1 das 03:00
    before = f[f["close_time"] < pd.Timestamp("2025-01-01 04:00", tz="UTC")]
    after = f[f["close_time"] >= pd.Timestamp("2025-01-01 04:00", tz="UTC")]
    assert len(before) > 5 and (before["htf_ema_trend"].dropna() < 500).all()
    assert (after["htf_ema_trend"] > 500).all()  # e passam a vê-lo assim que fecha
