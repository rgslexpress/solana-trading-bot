import numpy as np
import pandas as pd

from app.data.validation import closed_only, resample_ohlcv, validate_candles
from datetime import timedelta
from _helpers import make_candles, utc

TF = 900


def _now_after(df):
    return (df.index[-1] + pd.Timedelta(seconds=TF)).to_pydatetime()


def test_valid_candles_pass():
    df = make_candles(np.linspace(100, 110, 50))
    r = validate_candles(df, TF, _now_after(df))
    assert r.ok, r.issues


def test_duplicates_gaps_nan_and_stale_are_rejected():
    df = make_candles(np.linspace(100, 110, 50))
    dup = pd.concat([df, df.iloc[[-1]]])
    assert not validate_candles(dup, TF, _now_after(df)).ok
    gap = df.drop(df.index[20:30])
    assert any("lacuna" in i for i in validate_candles(gap, TF, _now_after(df)).issues)
    bad = df.copy()
    bad.iloc[5, bad.columns.get_loc("close")] = np.nan
    assert any("não finitos" in i for i in validate_candles(bad, TF, _now_after(df)).issues)
    neg = df.copy()
    neg.iloc[5, neg.columns.get_loc("open")] = -1
    assert not validate_candles(neg, TF, _now_after(df)).ok
    stale_now = (df.index[-1] + pd.Timedelta(seconds=TF * 10)).to_pydatetime()
    assert any("desatualizados" in i for i in validate_candles(df, TF, stale_now).issues)
    assert not validate_candles(df.iloc[0:0], TF, _now_after(df)).ok


def test_unordered_and_inconsistent_ohlc_rejected():
    df = make_candles(np.linspace(100, 110, 20))
    assert not validate_candles(df.iloc[::-1], TF, _now_after(df)).ok
    bad = df.copy()
    bad.iloc[3, bad.columns.get_loc("high")] = 1.0
    assert any("OHLC" in i for i in validate_candles(bad, TF, _now_after(df)).issues)


def test_closed_only_drops_forming_candle():
    df = make_candles(np.linspace(100, 110, 10))
    forming_now = (df.index[-1] + pd.Timedelta(seconds=TF // 2)).to_pydatetime()
    out = closed_only(df, TF, forming_now)
    assert len(out) == 9
    assert len(closed_only(df, TF, _now_after(df))) == 10


def test_resample_discards_incomplete_buckets():
    df = make_candles(np.linspace(100, 110, 10), tf_s=300)  # 10 candles de 5m
    out = resample_ohlcv(df, 900)  # 15m = 3 sub-candles
    assert len(out) == 3  # 9 sub-candles completos; o 10º é descartado
    assert abs(out["volume"].iloc[0] - 3000.0) < 1e-9


# ---------------------------- auditoria de dados ----------------------------
def test_single_missing_candle_is_rejected_by_default_but_old_gaps_outside_window_are_not():
    df = make_candles(np.linspace(100, 110, 60))
    one_missing = df.drop(df.index[30])
    assert any("lacuna" in i for i in validate_candles(one_missing, TF, _now_after(df)).issues)
    # lacuna antiga fora da janela usada pelos indicadores não invalida (janela = últimos 20 espaçamentos)
    assert validate_candles(one_missing, TF, _now_after(df), gap_window=20).ok
    assert not validate_candles(one_missing, TF, _now_after(df), gap_window=40).ok
    assert validate_candles(one_missing, TF, _now_after(df), max_gap_candles=2).ok  # tolerância explícita


def test_misaligned_timestamps_rejected():
    df = make_candles(np.linspace(100, 110, 30))
    shifted = df.copy()
    shifted.index = shifted.index + pd.Timedelta(seconds=37)
    now = (shifted.index[-1] + pd.Timedelta(seconds=TF)).to_pydatetime()
    assert any("desalinhados" in i for i in validate_candles(shifted, TF, now).issues)


def test_close_grace_excludes_just_closed_candle_until_provider_settles():
    df = make_candles(np.linspace(100, 110, 10))
    exactly_close = (df.index[-1] + pd.Timedelta(seconds=TF)).to_pydatetime()
    assert len(closed_only(df, TF, exactly_close, grace_s=0)) == 10
    assert len(closed_only(df, TF, exactly_close, grace_s=5)) == 9  # ainda dentro da carência
    assert len(closed_only(df, TF, exactly_close + timedelta(seconds=6), grace_s=5)) == 10


def test_stale_latest_price_is_not_used_for_stop_protection():
    from app.data.providers import SyntheticProvider
    from _helpers import JUP

    p = SyntheticProvider(seed=1, n_1m=60 * 24 * 3)
    last = p.full_history(JUP, "1m").index[-1].to_pydatetime()
    assert p.latest_price(JUP, last + timedelta(seconds=60)) is not None
    assert p.latest_price(JUP, last + timedelta(hours=2)) is None  # preço de 2h atrás não protege nada
