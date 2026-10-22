"""Regressões da revisão de segurança independente (2026-10-22): texto externo não libera bloqueios, contingência exige falhas reais, erro de módulo não apaga
restrições, calendário sem janelas fantasmas, SSRF sintático, busca travada, data futura e mensagem de política.

SIMULADO: cada teste reproduz a sonda que o revisor usou."""

import json
import os
import tempfile
import threading
import time
from datetime import timedelta

from app.intel.collector import NewsCollector
from app.intel.engine import IntelEngine
from app.intel.macro import CALENDAR_FILE_SOURCE, MacroEngine, MacroIngest, MacroStore, MemoryMacroView, make_event
from app.intel.models import SourceState
from app.intel.runtime import IntelRuntime
from app.intel.sources import SourceConfig, SourceError, load_sources
from app.intel.store import DbNewsView, IntelStore, MemoryNewsView
from app.storage.database import Database
from _helpers import raises
from _intel import CLF, FakeFeeds, MEDIA_A, NOW, SOLANA_STATUS, ok_xml, params, rec, rfc822, rss
from test_intel_health_contingency import SOURCES, env, ok, fail, states, _panel_with_state

BLOG = SourceConfig("blog", "Blog", "rss", "https://blog.example.org/rss", 0.3)


def assess(records, **kw):
    return IntelEngine(params(**kw), MemoryNewsView(records)).assess("JUP", NOW, "BUY")


# ------------------------------------------------------------------------------------------------ achado 1 — fechadores credíveis
def test_low_reliability_text_cannot_lift_a_critical_block():
    halt = rec("Solana mainnet-beta halted: block production stopped", SOLANA_STATUS, minutes_ago=60, status_state="investigating", summary="Investigating - mainnet halted")
    assert assess([halt]).block
    blog = rec("Solana outage has been resolved. The team confirmed the network is fully operational.", BLOG, minutes_ago=5)
    assert blog.resolution and blog.certainty == "confirmed"  # o TEXTO diz "resolvido e confirmado"...
    a = assess([halt, blog])
    assert a.block and a.alerts[0].blocks  # ...mas uma fonte de confiabilidade 0,3 NÃO levanta o bloqueio
    reliable = rec("Solana network operational again: incident resolved", SOLANA_STATUS, minutes_ago=5)
    assert not assess([halt, reliable]).block  # fonte confiável (oficial): encerra, e sobra a janela de cautela


def test_resolution_of_another_environment_does_not_close_a_mainnet_incident():
    halt = rec("Solana mainnet outage: block production halted", SOLANA_STATUS, minutes_ago=60, status_state="investigating", summary="Investigating - halted")
    devnet_resolved = rec("Solana devnet outage", SOLANA_STATUS, minutes_ago=5, status_state="resolved", summary="Resolved - devnet fixed")
    assert devnet_resolved.environment == "devnet" and devnet_resolved.resolution
    assert assess([halt, devnet_resolved]).block
    explorer = rec("Solana Explorer operational again: incident resolved", SOLANA_STATUS, minutes_ago=5)
    assert explorer.environment == "explorer" and assess([halt, explorer]).block
    mainnet_resolved = rec("Solana mainnet outage resolved, service restored", SOLANA_STATUS, minutes_ago=5)
    assert not assess([halt, mainnet_resolved]).block


def test_resolved_item_with_unreliable_event_key_cannot_close_an_unrelated_incident():
    """Variante do revisor: 'resolved' cujo event_key foi zerado (URL repetida) virava um fechador universal."""
    halt = rec("Solana mainnet outage: block production halted", SOLANA_STATUS, minutes_ago=60, status_state="investigating", summary="Investigating - halted")
    other = rec("Scheduled maintenance of the status page", SOLANA_STATUS, minutes_ago=5, status_state="resolved", summary="Completed - maintenance done")
    other.event_key = None  # a ingestão faz isso quando o mesmo link se repete no feed
    assert assess([halt, other]).block


def test_the_same_incident_resolved_with_its_own_event_key_still_closes_it():
    url = "https://status.solana.com/incidents/zzz"
    v1 = rec("Solana mainnet outage: block production halted", SOLANA_STATUS, minutes_ago=90, status_state="investigating", summary="Investigating - halted", url=url)
    v2 = rec("Solana mainnet outage: block production halted", SOLANA_STATUS, minutes_ago=10, status_state="resolved", summary="Resolved - fixed. Investigating - halted", url=url)
    assert v1.event_key == v2.event_key
    assert assess([v1]).block and not assess([v1, v2]).block


# ------------------------------------------------------------------------------------------------ achado 2 — contingência exige falhas reais
def test_robot_offline_for_hours_is_not_contingency_and_reduced_risk_does_not_release_entries():
    db, store, P, eng = env(contingency_policy="reduced_risk", unavailable_policy="block_entries")
    ok(store, SOLANA_STATUS, NOW - timedelta(hours=9), P)  # última coleta há 9 h; NENHUMA tentativa falhou (o robô estava desligado)
    ok(store, SOURCES[1], NOW - timedelta(hours=9), P)
    a = eng.assess("JUP", NOW)
    assert a.data_state == "stale" and a.contingency is None and a.block  # dados velhos != fonte falhando: segue bloqueado (política de indisponibilidade)
    assert eng.health(NOW)["geral"] == "critico"
    # com falhas REAIS por mais de 6 h, aí sim é contingência (e a política explícita vale)
    for k in range(20):
        fail(store, SOLANA_STATUS, NOW - timedelta(hours=8) + timedelta(minutes=30 * k), P)
    c = eng.assess("JUP", NOW)
    assert c.data_state == "contingency" and not c.block and c.risk_scale == 0.25


# ------------------------------------------------------------------------------------------------ achado 3 — erro de módulo não apaga restrições
def test_macro_engine_error_keeps_news_restrictions_and_adds_caution():
    class BoomMacro(MacroEngine):
        def assess(self, now):
            raise RuntimeError("calendário corrompido")

    news = rec("Solana mainnet outage hits validators", SOLANA_STATUS, minutes_ago=10)
    P = params(unavailable_policy="warn_only", macro_window_risk_multiplier=0.25)
    eng = IntelEngine(P, MemoryNewsView([news]), BoomMacro(P, MemoryMacroView([])))
    a = eng.assess("JUP", NOW)
    assert a.block and a.risk_scale == 0.25 and any("camada macro" in r for r in a.caution_reasons)  # o bloqueio de notícias sobrevive ao erro do macro


def test_runtime_fallback_applies_caution_even_with_warn_only():
    db = Database(":memory:")
    P = params(unavailable_policy="warn_only", caution_risk_multiplier=0.4)

    class Broken:
        def assess(self, *a, **k):
            raise RuntimeError("bug")

    rt = IntelRuntime(P, IntelStore(db.conn), Broken(), None, [], [])  # type: ignore[arg-type]
    a = rt.assess("JUP", NOW)
    assert not a.block and a.caution and a.risk_scale == 0.4 and 0 < a.risk_scale <= 1  # sem bloqueio (warn_only), mas com cautela
    P2 = params(unavailable_policy="block_entries")
    assert IntelRuntime(P2, IntelStore(db.conn), Broken(), None, [], []).assess("JUP", NOW).block  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ achado 4 — calendário sem janelas fantasmas
def test_corrected_or_removed_calendar_entries_replace_the_old_ones():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cal.json")
        json.dump({"events": [{"kind": "FOMC_DECISION", "date": "2026-10-28", "time": "14:00"}, {"kind": "CPI", "date": "2026-11-12", "time": "08:30"}]}, open(path, "w"))
        db = Database(":memory:")
        P = params(macro_calendar_file=path)
        st = MacroStore(db.conn)
        ing = MacroIngest(st, P)
        ing.load_calendar_file(NOW)
        assert {(e.kind, e.local_time) for e in st.events()} == {("FOMC_DECISION", "2026-10-28 14:00"), ("CPI", "2026-11-12 08:30")}
        json.dump({"events": [{"kind": "FOMC_DECISION", "date": "2026-10-28", "time": "14:30"}]}, open(path, "w"))  # horário CORRIGIDO e o CPI removido
        ing.load_calendar_file(NOW)
        assert {(e.kind, e.local_time) for e in st.events()} == {("FOMC_DECISION", "2026-10-28 14:30")}  # sem janela fantasma às 14:00 nem do CPI
        assert all(e.source_id == CALENDAR_FILE_SOURCE for e in st.events())


def test_official_ics_replaces_future_events_of_its_source_and_keeps_the_past():
    db = Database(":memory:")
    st = MacroStore(db.conn)
    past = make_event("CPI", "2026-10-13", "08:30", "America/New_York", source_id="bls_calendar", confidence="oficial")
    fut_old = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="bls_calendar", confidence="oficial")
    fut_new = make_event("CPI", "2026-11-13", "08:30", "America/New_York", source_id="bls_calendar", confidence="oficial")  # o BLS reagendou
    st.replace_events_from("bls_calendar", [past, fut_old], NOW)
    st.replace_events_from("bls_calendar", [fut_new], NOW, only_future_from=NOW)
    assert {e.local_time[:10] for e in st.events()} == {"2026-10-13", "2026-11-13"}


def test_replace_is_atomic_when_something_fails_midway():
    db = Database(":memory:")
    st = MacroStore(db.conn)
    a = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id=CALENDAR_FILE_SOURCE)
    st.replace_events_from(CALENDAR_FILE_SOURCE, [a], NOW)
    b = make_event("PPI", "2026-11-13", "08:30", "America/New_York", source_id=CALENDAR_FILE_SOURCE)
    b.indicators = (object(),)  # type: ignore[assignment]  # não serializável em JSON: falha no meio da operação
    with raises(TypeError):
        st.replace_events_from(CALENDAR_FILE_SOURCE, [b], NOW)
    assert [e.kind for e in st.events()] == ["CPI"]  # nada foi apagado pela metade (rollback)


# ------------------------------------------------------------------------------------------------ achado 5 — SSRF sintático
def test_catalog_rejects_numeric_hosts_trailing_dot_and_internal_names():
    for host in ("127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "localhost.", "metadata.google.internal.", "metadata.google.internal", "169.254.169.254", "10.0.0.5", "192.168.1.1",
                 "93.184.216.34", "intranet", "service.corp", "router.lan", "a..b.com", "-bad.example.com"):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            json.dump({"sources": [{"id": "x1", "kind": "rss", "url": f"https://{host}/feed", "reliability": 0.5}]}, open(path, "w"))
            with raises(SourceError):
                load_sources(path)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "c.json")
        json.dump({"sources": [{"id": "ok1", "kind": "rss", "url": "https://status.solana.com/history.rss", "reliability": 0.9},
                               {"id": "ok2", "kind": "rss", "url": "https://www.federalreserve.gov:443/feeds/x.xml", "reliability": 0.9}]}, open(path, "w"))
        assert [s.id for s in load_sources(path)] == ["ok1", "ok2"]


# ------------------------------------------------------------------------------------------------ achado 6 — busca travada
def test_stuck_background_fetch_is_abandoned_and_a_new_one_starts():
    release = threading.Event()

    class Stuck(FakeFeeds):
        def request(self, *a, **k):
            release.wait(30)
            return super().request(*a, **k)

    db = Database(":memory:")
    store = IntelStore(db.conn)
    P = params(stale_after_s=10**8, refresh_interval_s=60, refresh_budget_s=10, total_timeout_s=10)
    tr = Stuck({MEDIA_A.url: ok_xml(rss([("Solana outage ok", "https://a.example.com/1", "", rfc822(NOW))]))})
    rt = IntelRuntime(P, store, IntelEngine(P, DbNewsView(store, [MEDIA_A], P)), NewsCollector(P, [MEDIA_A], tr, store, CLF), [MEDIA_A], [])
    rt.refresh(NOW)
    first = rt._job
    assert first is not None and rt._watchdog_s() == 120.0
    rt.refresh(NOW + timedelta(seconds=100))
    assert rt._job is first  # ainda dentro do prazo do watchdog
    rt.refresh(NOW + timedelta(seconds=130))  # além do watchdog: a busca presa é abandonada e outra é iniciada
    assert first.abandoned and rt._job is not first and rt._job is not None
    assert any(e["tipo"] == "busca_travada" for e in store.events(5))
    release.set()
    rt.flush(NOW + timedelta(seconds=200))
    time.sleep(0.05)


# ------------------------------------------------------------------------------------------------ achado 8 — montagem do registro dentro do try
def test_record_decision_builder_errors_are_contained():
    db = Database(":memory:")
    rt = IntelRuntime(params(), IntelStore(db.conn), None, None, [], [])  # type: ignore[arg-type]

    def bad():
        raise ValueError("falha ao montar o registro")

    assert rt.record_decision(bad) is None and rt.audit_failures == 1 and "ValueError" in (rt.last_audit_error or "")
    assert rt.record_decision(lambda: {"ts": "t", "symbol": "JUP", "technical": "BUY", "mode": "shadow", "data_state": "ok", "action": "x", "would_change": False, "applied": False,
                                       "risk_scale": 1.0}) is not None


# ------------------------------------------------------------------------------------------------ achado 9 — mensagem de política no painel
def test_panel_contingency_message_does_not_claim_a_restriction_that_does_not_exist():
    from test_intel_health_contingency import _critical_down_for_at

    def writer(store, P, now):
        _critical_down_for_at(store, P, now, 8)

    for policy, expected in (("block_entries", "MANTIDA"), ("warn_only", "NÃO há restrição")):
        with tempfile.TemporaryDirectory() as d:
            p, now = _panel_with_state(d, writer, unavailable_policy=policy)
            try:
                js = p.get("/api/intel")[1]
                assert expected in js["contingencia"]["mensagem_politica"], (policy, js["contingencia"])
            finally:
                p.close()
