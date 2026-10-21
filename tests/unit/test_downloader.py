"""Baixador de candles com transporte FALSO (simula o contrato documentado; não valida a API real)."""

import os
import tempfile
from datetime import datetime, timezone

import pandas as pd

from app.data.downloader import DownloadError, download_history, pool_liquidity
from app.data.providers import CsvProvider
from app.http import HttpResponse, TransportError
from _helpers import JUP, raises

NOW = datetime(2025, 3, 1, 12, 7, tzinfo=timezone.utc)  # 12:07 => último candle de 15m totalmente fechado abre 11:45
TF = 900


class FakeGecko:
    """Serve candles de 15m em ordem decrescente, paginando por before_timestamp (como a API documentada)."""

    def __init__(self, skip_every=0, fail_429=0):
        self.calls, self.skip_every, self.fail_429 = [], skip_every, fail_429
        self.start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())

    def request(self, method, url, *, headers=None, params=None, json=None, timeout=10.0):
        self.calls.append((url, dict(params or {})))
        if url.endswith(f"/pools/{JUP.pool}"):
            return HttpResponse(200, {"data": {"attributes": {"reserve_in_usd": "1234567.8"}}}, {})
        if self.fail_429:
            self.fail_429 -= 1
            return HttpResponse(429, {}, {})
        before, limit = params["before_timestamp"], params["limit"]
        ts = [t for t in range(self.start, int(NOW.timestamp()) + TF, TF) if t < before]  # inclui o candle em formação (11:45..12:00)
        if self.skip_every:
            ts = [t for i, t in enumerate(ts) if i % self.skip_every]
        page = sorted(ts, reverse=True)[:limit]
        return HttpResponse(200, {"data": {"attributes": {"ohlcv_list": [[t, 1.0, 1.1, 0.9, 1.05, 10.0] for t in page]}}}, {})


def _dl(d, t, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("sleep", lambda s: None)
    return download_history(t, JUP, "15m", kw.pop("days", 20), d, **kw)


def test_paginates_dedupes_excludes_forming_candle_and_writes_csv_readable_by_csvprovider():
    with tempfile.TemporaryDirectory() as d:
        t = FakeGecko()
        r = _dl(d, t, days=20)  # 20 dias * 96 = 1920 candles => 2 páginas
        assert r.pages >= 2 and r.candles == 20 * 96 and not r.issues, (r.pages, r.candles, r.issues)
        assert all(p["token"] == JUP.mint and p["currency"] == "usd" and p["aggregate"] == 15 and p["limit"] == 1000 for _, p in t.calls)
        assert t.calls[0][1]["before_timestamp"] == int(NOW.timestamp()) // TF * TF
        assert t.calls[1][1]["before_timestamp"] < t.calls[0][1]["before_timestamp"]  # paginação sempre retrocede
        assert pd.Timestamp(r.last) == pd.Timestamp("2025-03-01 11:45", tz="UTC")  # candle 11:45 fechou às 12:00; o das 12:00 está em formação às 12:07 e é excluído
        # o arquivo é lido pelo provedor CSV do robô
        got = CsvProvider(d).get_candles(JUP, "15m", 5000, NOW)
        assert len(got) >= r.candles - 1 and got.index.is_monotonic_increasing and not got.index.has_duplicates


def test_refuses_to_overwrite_existing_csv_and_never_touches_it():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "JUP_15m.csv")
        open(p, "w").write("conteudo-do-usuario")
        with raises(DownloadError, "já existe"):
            _dl(d, FakeGecko(), days=2)
        assert open(p).read() == "conteudo-do-usuario"
        _dl(d, FakeGecko(), days=2, overwrite=True)
        assert open(p).read() != "conteudo-do-usuario"


def test_gaps_from_omitted_empty_intervals_are_reported_not_hidden():
    with tempfile.TemporaryDirectory() as d:
        r = _dl(d, FakeGecko(skip_every=7), days=5)
        assert any("lacuna" in i for i in r.issues)  # candles ausentes NÃO são inventados; a validação aponta


def test_retries_on_429_and_gives_up_clearly_and_handles_missing_pool_and_bad_format():
    sleeps = []
    with tempfile.TemporaryDirectory() as d:
        _dl(d, FakeGecko(fail_429=2), days=1, sleep=sleeps.append)
        assert sleeps and max(sleeps) >= 20  # esperou antes de tentar de novo
    with tempfile.TemporaryDirectory() as d:
        with raises(DownloadError, "repetidamente"):
            _dl(d, FakeGecko(fail_429=99), days=1)
        from app.domain import TokenInfo

        with raises(DownloadError, "pool"):
            download_history(FakeGecko(), TokenInfo("X", JUP.mint, 6), "15m", 1, d, now=NOW, sleep=lambda s: None)

        class Bad:
            def request(self, *a, **k):
                return HttpResponse(200, {"data": {}}, {})

        with raises(DownloadError, "formato"):
            _dl(d, Bad(), days=1)

        class Down:
            def request(self, *a, **k):
                raise TransportError("sem internet")

        with raises(DownloadError, "rede"):
            _dl(d, Down(), days=1)


def test_pool_liquidity_parses_or_returns_none():
    assert pool_liquidity(FakeGecko(), "https://x", JUP.pool, sleep=lambda s: None) == 1234567.8

    class Bad:
        def request(self, *a, **k):
            return HttpResponse(500, {}, {})

    assert pool_liquidity(Bad(), "https://x", JUP.pool, sleep=lambda s: None) is None
