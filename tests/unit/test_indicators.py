import numpy as np
import pandas as pd

from app.strategy import indicators as ind
from _helpers import make_candles


def test_ema_matches_manual_recursion():
    s = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    out = ind.ema(s, 3)
    alpha = 2 / 4
    ref = [s.iloc[0]]
    for v in s.iloc[1:]:
        ref.append(alpha * v + (1 - alpha) * ref[-1])
    assert out.iloc[:2].isna().all()  # aquecimento
    assert abs(out.iloc[-1] - ref[-1]) < 1e-12


def test_rsi_extremes_and_flat():
    up = pd.Series(np.arange(1.0, 60.0))
    assert ind.rsi(up, 14).iloc[-1] == 100.0
    down = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert ind.rsi(down, 14).iloc[-1] < 1e-9
    flat = pd.Series([5.0] * 40)
    assert ind.rsi(flat, 14).iloc[-1] == 50.0


def test_rsi_known_value_wilder():
    # série clássica de Wilder (valores publicados ~70.5 no 15º ponto)
    close = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28])
    assert abs(ind.rsi(close, 14).iloc[-1] - 70.46) < 0.5


def test_macd_hist_is_line_minus_signal():
    c = pd.Series(np.linspace(10, 20, 80) + np.sin(np.arange(80)))
    m = ind.macd(c, 12, 26, 9)
    d = (m["macd"] - m["macd_signal"] - m["macd_hist"]).dropna()
    assert (d.abs() < 1e-12).all()


def test_bollinger_bands_symmetry_and_width():
    c = pd.Series(np.random.default_rng(1).normal(100, 2, 100))
    b = ind.bollinger(c, 20, 2.0).dropna()
    assert ((b["bb_upper"] - b["bb_mid"]) - (b["bb_mid"] - b["bb_lower"])).abs().max() < 1e-9
    assert (b["bb_width"] > 0).all()


def test_atr_constant_range():
    df = make_candles([100.0] * 60)
    a = ind.atr(df["high"], df["low"], df["close"], 14).dropna()
    assert abs(a.iloc[-1] - 100.0 * (1.002 - 0.998)) < 1e-6


def test_relative_volume_excludes_current_candle():
    v = pd.Series([100.0] * 20 + [300.0])
    assert abs(ind.relative_volume(v, 20).iloc[-1] - 3.0) < 1e-12


def test_correlation_of_identical_series_is_one_and_handles_missing():
    a = pd.Series(np.cumprod(1 + np.random.default_rng(2).normal(0, 0.01, 200)) * 100)
    assert abs(ind.rolling_return_corr(a, a.copy(), 50).iloc[-1] - 1.0) < 1e-9
    b = a.copy()
    b.iloc[100:110] = np.nan  # dados ausentes não geram exceção
    ind.rolling_return_corr(a, b, 50)


def test_indicators_are_causal():
    rng = np.random.default_rng(3)
    c = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    full = ind.rsi(c, 14)
    trunc = ind.rsi(c.iloc[:200], 14)
    assert np.allclose(full.iloc[:200].dropna(), trunc.dropna())
