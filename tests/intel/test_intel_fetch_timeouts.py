"""P1.7 — Coleta: prazo total por requisição, busca em segundo plano (o ciclo de trading nunca espera pela rede), backoff após HTTP 429.

SIMULADO: transportes falsos e um módulo `httpx` de mentira (o httpx real pode não estar instalado). Nenhuma fonte real foi acessada."""

import sys
import threading
import time
import tempfile
import types
from datetime import timedelta

from app.http import HttpResponse, HttpxTransport, TransportError
from app.intel.collector import NewsCollector
from app.intel.runtime import IntelRuntime
from app.intel.sources import fetch_source, parse_retry_after
from app.intel.store import DbNewsView, IntelStore
from app.intel.engine import IntelEngine
from app.storage.database import Database
from _helpers import raises
from test_intel_integration import _build, _run
from _intel import CLF, FakeFeeds, MEDIA_A, NOW, SOLANA_STATUS, ok_xml, params, rfc822, rss


def _feed():
    return ok_xml(rss([("Solana outage ok", "https://a.example.com/1", "", rfc822(NOW - timedelta(minutes=3)))]))


class Mono:
    v = 0.0

    def __call__(self):
        return self.v


def _collector(routes, sources=(MEDIA_A,), **kw):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    t = FakeFeeds(routes)
    mono = Mono()
    c = NewsCollector(params(stale_after_s=10**8, **kw), list(sources), t, store, CLF, mono)
    return c, store, t, mono


# ------------------------------------------------------------------------------------------------ HttpxTransport (httpx simulado)
class _FakeHttpx(types.ModuleType):
    """Imita só o que o HttpxTransport usa: Client.stream (iter_bytes), Timeout, TimeoutException, HTTPError."""

    class HTTPError(Exception):
        pass

    class TimeoutException(HTTPError):
        pass

    class Timeout:
        def __init__(self, v):
            self.v = v

    def __init__(self, chunks, per_chunk_s=0.0, raise_exc=None):
        super().__init__("httpx")
        self._chunks, self._sleep, self._exc = chunks, per_chunk_s, raise_exc
        outer = self

        class Resp:
            status_code, headers, charset_encoding = 200, {"content-type": "text/xml"}, "utf-8"

            def iter_bytes(self_inner):
                for ch in outer._chunks:
                    if outer._sleep:
                        time.sleep(outer._sleep)
                    yield ch

        class Ctx:
            def __enter__(self_inner):
                if outer._exc:
                    raise outer._exc
                return Resp()

            def __exit__(self_inner, *a):
                return False

        class Client:
            def __init__(self_inner, follow_redirects=False):
                assert follow_redirects is False  # redirecionamentos NÃO são seguidos

            def stream(self_inner, method, url, **kw):
                return Ctx()

            def close(self_inner):
                pass

        self.Client = Client


def _with_httpx(fake, fn):
    old = sys.modules.get("httpx")
    sys.modules["httpx"] = fake
    try:
        return fn(HttpxTransport())
    finally:
        if old is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = old


def test_transport_total_timeout_cuts_a_source_that_drips_bytes_slowly():
    fake = _FakeHttpx([b"<rss>"] * 50, per_chunk_s=0.05)  # 50 pedaços a cada 50 ms = 2,5 s no total; cada pedaço individual é "rápido"
    t0 = time.monotonic()
    with raises(TransportError, "prazo total"):
        _with_httpx(fake, lambda tr: tr.request("GET", "https://x.example.com/", timeout=8.0, total_timeout=0.3))
    assert time.monotonic() - t0 < 1.0  # parou perto do prazo total, não esperou os 2,5 s
    try:
        _with_httpx(fake, lambda tr: tr.request("GET", "https://x.example.com/", timeout=8.0, total_timeout=0.1))
    except TransportError as exc:
        assert exc.kind == "total_timeout"


def test_transport_max_bytes_stops_the_download_and_exceptions_are_mapped():
    big = _FakeHttpx([b"x" * 1000] * 100)
    with raises(TransportError, "maior que"):
        _with_httpx(big, lambda tr: tr.request("GET", "https://x.example.com/", max_bytes=5000))
    ok = _with_httpx(_FakeHttpx([b"<rss></rss>"]), lambda tr: tr.request("GET", "https://x.example.com/", timeout=8.0, total_timeout=5.0, max_bytes=10_000))
    assert ok.status == 200 and ok.body == "<rss></rss>"
    for exc_type, kind in ((_FakeHttpx.TimeoutException, "timeout"), (_FakeHttpx.HTTPError, "network")):
        try:
            _with_httpx(_FakeHttpx([], raise_exc=exc_type("x")), lambda tr: tr.request("GET", "https://x.example.com/"))
        except TransportError as e:
            assert e.kind == kind
        else:
            raise AssertionError("deveria lançar TransportError")


def test_limits_are_passed_to_transports_that_support_them_and_not_to_the_others():
    class Limited(FakeFeeds):
        supports_limits = True

        def request(self, method, url, **kw):
            self.kw = kw
            return super().request(method, url, **{k: v for k, v in kw.items() if k in ("headers", "params", "timeout")})

    lim = Limited({MEDIA_A.url: _feed()})
    fetch_source(lim, MEDIA_A, user_agent="t/1", timeout=8.0, max_bytes=500_000, max_items=10, now=NOW, total_timeout=20.0)
    assert lim.kw["total_timeout"] == 20.0 and lim.kw["max_bytes"] == 500_000
    plain = FakeFeeds({MEDIA_A.url: _feed()})
    fetch_source(plain, MEDIA_A, user_agent="t/1", timeout=8.0, max_bytes=500_000, max_items=10, now=NOW, total_timeout=20.0)  # não quebra: não recebe os parâmetros extras


def test_timeout_kinds_are_recorded_with_duration_and_error_kind():
    c, store, t, mono = _collector({MEDIA_A.url: [TransportError("ReadTimeout", "timeout")]})
    c.refresh(NOW)
    r = store.status_rows()["media_a"]
    assert r["last_error_kind"] == "timeout" and r["consecutive_failures"] == 1 and r["last_duration_ms"] is not None and "tempo esgotado" in r["last_error"]
    c2, store2, _, _ = _collector({MEDIA_A.url: [HttpResponse(503, "", {})]})
    c2.refresh(NOW)
    r2 = store2.status_rows()["media_a"]
    assert r2["last_error_kind"] == "http" and r2["last_http_status"] == 503


# ------------------------------------------------------------------------------------------------ HTTP 429 / backoff
def test_retry_after_parsing():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("Thu, 22 Oct 2026 12:10:00 GMT", NOW) == 600.0
    assert parse_retry_after("Thu, 22 Oct 2026 11:00:00 GMT", NOW) == 0.0  # data no passado
    for bad in (None, "", "abc", "-5"):
        assert parse_retry_after(bad, NOW) is None


def test_http_429_applies_backoff_respects_retry_after_and_does_not_hammer_the_source():
    c, store, t, mono = _collector({MEDIA_A.url: [HttpResponse(429, "", {"Retry-After": "3600"}), HttpResponse(429, "", {}), _feed()]}, backoff_base_s=900.0, backoff_max_s=7200.0)
    c.refresh(NOW)
    assert len(t.calls) == 1
    r = store.status_rows()["media_a"]
    assert r["last_error_kind"] == "rate_limit" and r["rate_limited_until"] and "Retry-After=3600" in r["last_error"]
    for minutes in (1, 10, 59):  # dentro do Retry-After: NENHUMA requisição
        st = c.refresh(NOW + timedelta(minutes=minutes))
        assert st["puladas_detalhe"] == {"backoff": 1} and len(t.calls) == 1
    c.refresh(NOW + timedelta(hours=1, minutes=1))  # depois do Retry-After: tenta de novo (e recebe outro 429, sem Retry-After)
    assert len(t.calls) == 2
    st = store.status_rows()["media_a"]
    assert st["backoff_level"] == 2  # o backoff cresce a cada novo 429
    # sem Retry-After o próximo prazo é base × 2^nível (limitado por backoff_max_s), e não menos que o primeiro nível
    until = c._backoff_until["media_a"]
    assert until - (NOW + timedelta(hours=1, minutes=1)) >= timedelta(seconds=1800)
    events = [e["tipo"] for e in store.events(10)]
    assert events.count("backoff_limite") == 2


def test_backoff_survives_a_restart():
    c, store, t, mono = _collector({MEDIA_A.url: [HttpResponse(429, "", {"Retry-After": "7200"})]})
    c.refresh(NOW)
    new = NewsCollector(c.p, [MEDIA_A], FakeFeeds({MEDIA_A.url: _feed()}), store, CLF, Mono())  # "reinício": novo coletor, mesmo banco
    new.load_state(NOW + timedelta(minutes=10))
    st = new.refresh(NOW + timedelta(minutes=10))
    assert st["puladas_detalhe"] == {"backoff": 1} and not new.t.calls  # um reinício NÃO ignora o limite já recebido


def test_daily_request_cap_is_respected_even_across_restarts():
    from dataclasses import replace

    bls = replace(MEDIA_A, id="bls_values", kind="bls_api", url="https://api.bls.gov/publicAPI/v1/timeseries/data/", series=("LNS14000000", "CES0000000001"), daily_cap=3, domain="macro_us")
    body = HttpResponse(200, {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"data": []}]}}, {})
    routes = {bls.url.rstrip("/") + "/LNS14000000": body, bls.url.rstrip("/") + "/CES0000000001": body}
    c, store, t, mono = _collector(routes, sources=(bls,))
    assert c.refresh(NOW)["fontes_ok"] == 1 and len(t.calls) == 2  # 2 de 3 requisições do dia
    mono.v += 10
    st = c.refresh(NOW + timedelta(hours=1))
    assert st["puladas_detalhe"] == {"limite_diario": 1} and len(t.calls) == 2  # mais 2 passariam de 3: pula
    again = NewsCollector(c.p, [bls], FakeFeeds(routes), store, CLF, Mono())
    again.load_state(NOW + timedelta(hours=2))
    assert again.refresh(NOW + timedelta(hours=2))["puladas_detalhe"] == {"limite_diario": 1}
    nextday = NewsCollector(c.p, [bls], FakeFeeds(routes), store, CLF, Mono())
    nextday.load_state(NOW + timedelta(days=1))
    assert nextday.refresh(NOW + timedelta(days=1))["fontes_ok"] == 1  # novo dia UTC: contagem zerada


# ------------------------------------------------------------------------------------------------ coleta em segundo plano
class BlockedTransport(FakeFeeds):
    """Transporte que "trava" até ser liberado (simula fonte lenta)."""

    def __init__(self, routes):
        super().__init__(routes)
        self.gate = threading.Event()
        self.started = threading.Event()

    def request(self, *a, **k):
        self.started.set()
        self.gate.wait(10)
        return super().request(*a, **k)


def _runtime(transport, background=True):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    P = params(stale_after_s=10**8, background_fetch=background, refresh_interval_s=300)
    coll = NewsCollector(P, [MEDIA_A], transport, store, CLF)
    return IntelRuntime(P, store, IntelEngine(P, DbNewsView(store, [MEDIA_A], P)), coll, [MEDIA_A], []), store


def test_background_fetch_never_blocks_the_caller_and_applies_the_result_later():
    tr = BlockedTransport({MEDIA_A.url: _feed()})
    rt, store = _runtime(tr)
    t0 = time.monotonic()
    assert rt.refresh(NOW) is None  # volta IMEDIATAMENTE, mesmo com a fonte travada
    assert time.monotonic() - t0 < 1.0 and tr.started.wait(2)
    assert rt.refresh(NOW + timedelta(seconds=1)) is None and rt._job is not None  # ainda travada: não abre 2ª busca
    assert store.latest() == []
    tr.gate.set()
    stats = rt.flush(NOW + timedelta(seconds=2))
    assert stats and stats["novos"] == 1 and len(store.latest()) == 1 and rt._job is None


def test_synchronous_mode_is_still_available_for_scripts_and_tests():
    tr = FakeFeeds({MEDIA_A.url: _feed()})
    rt, store = _runtime(tr, background=False)
    st = rt.refresh(NOW)
    assert st and st["novos"] == 1 and rt._job is None


def test_trading_cycle_is_not_delayed_by_a_stuck_news_source_and_stops_still_run():
    """Com a fonte de notícias travada, o ciclo de trading (stops, sinais) termina rápido e a decisão é a mesma do baseline."""
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow")
        tr = BlockedTransport({SOLANA_STATUS.url: _feed()})
        rt = svc.intel
        rt.collector = NewsCollector(rt.params, [SOLANA_STATUS], tr, store, CLF)
        rt.params.background_fetch = True
        rt.sources = [SOLANA_STATUS]
        t0 = time.monotonic()
        _run(svc, clock, entry)
        elapsed = time.monotonic() - t0
        tr.gate.set()
        assert elapsed < 20.0  # nenhum ciclo esperou os 10 s do bloqueio (a busca fica em segundo plano)
        assert tr.started.is_set()  # a busca foi disparada
        assert len(db.load_positions()) >= 1  # o robô negociou normalmente (paper) com a coleta travada


def test_failure_in_the_background_thread_is_contained():
    class Exploding(FakeFeeds):
        def request(self, *a, **k):
            raise RuntimeError("bug interno no transporte")

    rt, store = _runtime(Exploding({}))
    assert rt.refresh(NOW) is None
    rt._job.finished.wait(5)
    rt.refresh(NOW + timedelta(seconds=400))  # recolhe o resultado/erro sem lançar
    assert store.status_rows()["media_a"]["last_error_kind"] == "other"  # o erro virou ESTADO da fonte, não exceção no ciclo
