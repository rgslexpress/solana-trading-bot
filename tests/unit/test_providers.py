import os
import tempfile

import numpy as np
import pandas as pd

from app.data.providers import CsvProvider, DataProviderError, GeckoTerminalProvider, SyntheticProvider
from _helpers import JUP, make_candles, raises, utc


def test_csv_provider_reads_filters_future_and_requires_file():
    df = make_candles(np.linspace(100, 110, 30), 900)
    with tempfile.TemporaryDirectory() as d:
        out = df.copy()
        out.insert(0, "timestamp", (df.index.astype("int64") // 10**9))
        out.to_csv(os.path.join(d, "JUP_15m.csv"), index=False)
        p = CsvProvider(d, liquidity_usd=2e6)
        now = df.index[10].to_pydatetime()
        got = p.get_candles(JUP, "15m", 100, now)
        assert len(got) == 11 and got.index[-1] == df.index[10]  # nunca devolve o futuro
        assert p.liquidity_usd(JUP, now) == 2e6
        assert CsvProvider(d).liquidity_usd(JUP, now) is None  # desconhecida => fail-closed
        with raises(DataProviderError):
            p.get_candles(JUP, "1h", 10, now)


def test_gecko_provider_parses_caches_and_fails_closed():
    rows = [[1735689600 + 900 * i, 1.0, 1.1, 0.9, 1.05, 100.0] for i in range(10)][::-1]  # descendente, como na API
    calls = []

    def http_get(url, params):
        calls.append(url)
        if url.endswith("/pools/pool1"):
            return {"data": {"attributes": {"reserve_in_usd": "750000.5"}}}
        return {"data": {"attributes": {"ohlcv_list": rows}}}

    t = [0.0]
    p = GeckoTerminalProvider("https://x", http_get, cache_ttl_s=30, min_interval_s=0.0, clock=lambda: t[0], liquidity_cap=500_000)
    now = utc(2026, 1, 1)
    df = p.get_candles(JUP, "15m", 100, now)
    assert df.index.is_monotonic_increasing and len(df) == 10
    p.get_candles(JUP, "15m", 100, now)
    assert len([c for c in calls if "ohlcv" in c]) == 1  # segunda chamada veio do cache
    t[0] = 100.0
    p.get_candles(JUP, "15m", 100, now)
    assert len([c for c in calls if "ohlcv" in c]) == 2  # cache expirou
    assert p.liquidity_usd(JUP, now) == 500_000  # menor entre consultado e teto
    bad = GeckoTerminalProvider("https://x", lambda u, q: {"data": {}}, min_interval_s=0.0)
    with raises(DataProviderError):
        bad.get_candles(JUP, "15m", 10, now)
    assert bad.liquidity_usd(JUP, now) is None
    nopool = GeckoTerminalProvider("https://x", http_get, min_interval_s=0.0)
    from app.domain import TokenInfo

    with raises(DataProviderError, "pool"):
        nopool.get_candles(TokenInfo("X", JUP.mint, 6), "15m", 10, now)


def test_synthetic_provider_is_deterministic_and_causal():
    a, b = SyntheticProvider(seed=3, n_1m=60 * 24 * 5), SyntheticProvider(seed=3, n_1m=60 * 24 * 5)
    now = pd.Timestamp("2025-01-03 12:00", tz="UTC").to_pydatetime()
    x, y = a.get_candles(JUP, "15m", 50, now), b.get_candles(JUP, "15m", 50, now)
    assert x.equals(y) and x.index[-1] <= pd.Timestamp(now)
