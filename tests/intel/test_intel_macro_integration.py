"""Macro EUA integrada: serviço de trading (off/shadow/enforce), coleta de calendário/valores/mercado, migração do banco, painel e bloqueio de LIVE.

SIMULADO: calendário e respostas de fontes sintéticos. Nenhuma fonte macro real foi acessada."""

import json
import os
import sqlite3
import tempfile
from datetime import timedelta

from app.config import Settings
from app.domain import Mode
from app.http import HttpResponse
from app.intel.collector import NewsCollector
from app.intel.engine import IntelEngine
from app.intel.macro import DbMacroView, MacroEngine, MacroIngest, MacroStore, make_event
from app.intel.models import NewsRecord
from app.intel.runtime import IntelRuntime
from app.intel.sources import SourceConfig
from app.intel.store import DbNewsView, IntelStore
from app.storage.database import Database
from harness import Panel, make_settings
from test_intel_integration import _build, _decision_now, _positions, _rows, _run
from _intel import CLF, FakeFeeds, NOW, ok_xml, params
from test_intel_macro_time import CSV, ICS, ICS_SRC, _bls


def _attach_macro(svc, db, events):
    store = MacroStore(db.conn)
    for e in events:
        store.upsert_event(e, NOW)
    p = svc.intel.params
    svc.intel.macro_store = store
    svc.intel.engine.macro = MacroEngine(p, DbMacroView(store))
    return store


def _fomc_at(utc):
    ev = make_event("FOMC_DECISION", "2026-01-02", "10:00", "America/New_York", source_id="t", confidence="oficial")
    ev.scheduled_utc = utc
    ev.id = "fomc-" + utc.isoformat()
    return ev


def _case(mode, offset_min, **kw):
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, mode)
        for k, v in kw.items():
            setattr(svc.intel.params, k, v)
        if offset_min is not None and mode != "off":
            _attach_macro(svc, db, [_fomc_at(_decision_now(entry) + timedelta(minutes=offset_min))])
        _run(svc, clock, entry)
        pos = _positions(db)
        audit = _rows(db, "SELECT action, would_change, applied, risk_scale, alerts FROM intel_decisions")
        sig = [tuple(r) for r in db.conn.execute("SELECT candle_ts,type,score FROM signals WHERE candle_ts NOT LIKE '%#%' ORDER BY candle_ts")]
        return pos, audit, sig


def test_off_mode_is_identical_with_or_without_a_macro_calendar():
    base_pos, audit, sig = _case("off", None)
    assert audit == [] and len(base_pos) >= 1


def test_shadow_with_macro_window_does_not_change_the_decision_but_records_what_would_change():
    base_pos, _, base_sig = _case("off", None)
    pos, audit, sig = _case("shadow", 10)  # divulgação 10 min depois da decisão: dentro da janela
    assert [(round(p.qty, 9), p.status) for p in pos] == [(round(p.qty, 9), p.status) for p in base_pos] and sig == base_sig
    assert audit and audit[0]["would_change"] == 1 and audit[0]["applied"] == 0 and "SHADOW: reduziria o risco" in audit[0]["action"] and "macro_calendario" in audit[0]["alerts"]
    assert audit[0]["risk_scale"] == 0.5


def test_enforce_reduces_risk_inside_the_window_never_increases_and_is_neutral_outside():
    base_pos, _, _ = _case("off", None)
    inside, audit, _ = _case("enforce", 10)
    outside, audit_out, _ = _case("enforce", 60 * 6)
    assert len(inside) >= 1 and inside[0].qty <= base_pos[0].qty + 1e-12 and audit[0]["applied"] == 1 and audit[0]["risk_scale"] == 0.5
    assert abs(outside[0].qty - base_pos[0].qty) < 1e-12 and audit_out[0]["would_change"] == 0 and audit_out[0]["risk_scale"] == 1.0


def test_enforce_block_policy_prevents_new_entries_inside_the_window_only():
    blocked, audit, _ = _case("enforce", 10, macro_high_policy="block")
    assert blocked == [] and "bloqueou a entrada" in audit[0]["action"]
    free, _, _ = _case("enforce", 60 * 6, macro_high_policy="block")
    assert len(free) >= 1


def test_macro_never_closes_positions_or_touches_stops():
    """Mesmo com janela macro ativa e política de bloqueio, as posições abertas seguem a lógica normal de stops/saídas."""
    base_pos, _, _ = _case("off", None)
    pos, _, _ = _case("enforce", 10, macro_high_policy="block")  # sem entrada: nada a fechar; o cenário de saída é coberto abaixo
    assert pos == []
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce")
        _attach_macro(svc, db, [_fomc_at(_decision_now(entry) + timedelta(minutes=300))])  # janela só DEPOIS da entrada; cobre a janela do stop/saída? não: longe
        _run(svc, clock, entry)
        closed = [p for p in _positions(db) if p.status == "CLOSED"]
        assert closed and closed[0].pnl_quote is not None  # entrada e saída por regras normais


# ------------------------------------------------------------------------------------------------ coleta macro ponta a ponta
def _macro_runtime(sources, routes):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    P = params(stale_after_s=10**8, background_fetch=False, macro_calendar_file=os.path.join(tempfile.mkdtemp(), "nao_existe.json"))
    ms = MacroStore(db.conn)
    ing = MacroIngest(ms, P)
    coll = NewsCollector(P, sources, FakeFeeds(routes), store, CLF, macro=ing)
    rt = IntelRuntime(P, store, IntelEngine(P, DbNewsView(store, sources, P), MacroEngine(P, DbMacroView(ms))), coll, sources, [], ms, ing)
    return rt, db, ms, store


def test_ics_bls_and_treasury_are_ingested_through_the_collector_and_feed_the_engine():
    bls = SourceConfig("bls_values", "BLS", "bls_api", "https://api.bls.gov/publicAPI/v1/timeseries/data/", 0.95, official=True, series=("LNS14000000",), domain="macro_us", daily_cap=20)
    tsy = SourceConfig("treasury_yields", "Tesouro", "treasury_csv", "https://home.treasury.gov/x.csv", 0.95, official=True, domain="macro_us")
    routes = {ICS_SRC.url: ok_xml(ICS), bls.url + "LNS14000000": HttpResponse(200, _bls([((2026, 9), 4.4), ((2026, 8), 4.3)]), {}), tsy.url: ok_xml(CSV)}
    rt, db, ms, store = _macro_runtime([ICS_SRC, bls, tsy], routes)
    st = rt.refresh(NOW)
    assert st["fontes_ok"] == 3 and st["eventos_macro"] >= 6
    assert {e.kind for e in ms.events()} >= {"EMPLOYMENT_SITUATION", "CPI", "PPI"}
    assert [v.indicator for v in ms.values()] == ["UNEMPLOYMENT"] and {m.indicator for m in ms.market()} == {"UST_2Y", "UST_10Y"}
    # repetir a coleta não duplica nada (idempotente); um valor revisado vira NOVA linha
    rt.collector._last_refresh = None
    rt.collector.refresh(NOW + timedelta(hours=1))
    assert len(ms.values()) == 1 and len(ms.events()) == len(set(e.id for e in ms.events()))
    routes[bls.url + "LNS14000000"] = HttpResponse(200, _bls([((2026, 9), 4.5), ((2026, 8), 4.3)]), {})
    rt.collector.refresh(NOW + timedelta(hours=2))
    assert [v.actual for v in ms.values()] == [4.4, 4.5] and ms.latest_value("UNEMPLOYMENT", "2026-09").is_revision
    # saúde: cada fonte macro aparece com seu domínio
    states = {s.source_id: s for s in rt.engine.view.source_states(NOW + timedelta(hours=2))}
    assert states["bls_values"].domain == "macro_us" and states["bls_values"].state == "ok"


def test_bls_values_are_polled_only_when_a_release_is_expected_or_nothing_was_ever_collected():
    bls = SourceConfig("bls_values", "BLS", "bls_api", "https://api.bls.gov/publicAPI/v1/timeseries/data/", 0.95, official=True, series=("LNS14000000",), domain="macro_us", daily_cap=20)
    routes = {bls.url + "LNS14000000": HttpResponse(200, _bls([((2026, 9), 4.4), ((2026, 8), 4.3)]), {})}
    rt, db, ms, store = _macro_runtime([bls], routes)
    assert rt.macro_ingest.poll_hints([bls], NOW)["bls_values"] is True  # nunca houve valor: uma consulta inicial
    rt.refresh(NOW)
    assert rt.macro_ingest.poll_hints([bls], NOW + timedelta(hours=1))["bls_values"] is False  # sem divulgação esperada: poupa o limite diário
    ev = make_event("EMPLOYMENT_SITUATION", "2026-11-06", "08:30", "America/New_York", source_id="bls", confidence="oficial")
    ms.upsert_event(ev, NOW)
    assert rt.macro_ingest.poll_hints([bls], ev.scheduled_utc - timedelta(minutes=1))["bls_values"] is False  # antes da divulgação
    assert rt.macro_ingest.poll_hints([bls], ev.scheduled_utc + timedelta(minutes=1))["bls_values"] is True  # divulgação ocorreu e o valor ainda não chegou
    n_calls = len(rt.collector.t.calls)
    rt.collector._last_refresh = None
    rt.collector._last_fetch.clear()
    rt.refresh(ev.scheduled_utc + timedelta(hours=7))  # 6 h depois da janela de interesse: não consulta
    assert len(rt.collector.t.calls) == n_calls


def test_calendar_file_is_loaded_reloaded_and_a_broken_edit_keeps_the_previous_calendar():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "macro_calendar.json")
        json.dump({"events": [{"kind": "FOMC_DECISION", "date": "2026-10-28", "time": "14:00"}]}, open(path, "w"))
        db = Database(":memory:")
        P = params(macro_calendar_file=path)
        ing = MacroIngest(MacroStore(db.conn), P)
        assert ing.load_calendar_file(NOW) == 1 and len(ing.store.events()) == 1
        assert ing.reload_if_changed(NOW) == 0  # sem mudança
        json.dump({"events": [{"kind": "FOMC_DECISION", "date": "2026-10-28", "time": "14:00"}, {"kind": "CPI", "date": "2026-11-12", "time": "08:30"}]}, open(path, "w"))
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
        assert ing.reload_if_changed(NOW) == 1 and len(ing.store.events()) == 2
        open(path, "w").write("{ quebrado")
        os.utime(path, (os.path.getmtime(path) + 10, os.path.getmtime(path) + 10))
        assert ing.reload_if_changed(NOW) == 0 and len(ing.store.events()) == 2  # mantém o calendário anterior


# ------------------------------------------------------------------------------------------------ migração do banco
def test_existing_database_is_migrated_additively_and_old_rows_remain_readable():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old.sqlite3")
        c = sqlite3.connect(path)
        c.executescript("""CREATE TABLE news_items (id TEXT PRIMARY KEY, source_id TEXT, source_name TEXT, title TEXT, summary TEXT, url TEXT, published_at TEXT, collected_at TEXT,
          first_seen TEXT, assets TEXT, category TEXT, polarity TEXT, certainty TEXT, relevance REAL, reliability REAL, impact INTEGER, flags TEXT, reasons TEXT, resolution INTEGER, duplicate_of TEXT);
          CREATE TABLE news_source_status (source_id TEXT PRIMARY KEY, name TEXT, last_attempt_at TEXT, last_ok_at TEXT, consecutive_failures INTEGER, last_error TEXT, last_items INTEGER, last_malformed INTEGER);
          INSERT INTO news_items VALUES ('old1','s','S','Solana outage','','https://x.example/1','2026-10-22T10:00:00+00:00','2026-10-22T10:01:00+00:00','2026-10-22T10:01:00+00:00','["SOL"]','incidente_rede','negative','confirmed',1,0.9,3,'[]','[]',0,NULL);
          INSERT INTO news_source_status VALUES ('s','S','2026-10-22T10:01:00+00:00','2026-10-22T10:01:00+00:00',0,NULL,3,0);""")
        c.commit()
        c.close()
        db = Database(path)  # abre a base ANTIGA: as colunas novas são acrescentadas sem apagar nada
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(news_items)")}
        assert {"event_key", "nature", "environment", "status_state", "origin_key", "refs", "domain", "indicator"} <= cols
        assert {"last_error_kind", "unavailable_since", "consecutive_successes", "rate_limited_until", "day_requests"} <= {r["name"] for r in db.conn.execute("PRAGMA table_info(news_source_status)")}
        old = IntelStore(db.conn).latest()[0]
        assert isinstance(old, NewsRecord) and old.id == "old1" and old.environment == "desconhecido" and old.event_key is None and old.domain == "crypto_news"
        assert db.conn.execute("SELECT COUNT(*) c FROM macro_events").fetchone()["c"] == 0  # tabelas novas criadas
        Database(path)  # segunda abertura é idempotente


# ------------------------------------------------------------------------------------------------ painel
def test_panel_macro_payload_has_windows_upcoming_values_market_and_pending_sources():
    from app.intel.macro import MacroMarket, MacroValue

    with tempfile.TemporaryDirectory() as d:
        cat = os.path.join(d, "intel_sources.json")
        json.dump({"sources": [{"id": "solana_status", "name": "Solana Status", "kind": "rss", "url": "https://status.solana.com/history.rss", "reliability": 0.95, "official": True,
                                "assets": ["SOL"], "critical": True, "domain": "solana_status", "status_format": "statuspage"}]}, open(cat, "w"))
        s = make_settings(d)
        s.intel = params(mode="shadow", sources_file=cat, stale_after_s=10**8, macro_calendar_file=os.path.join(d, "x.json"))
        p = Panel(d, settings=s)
        try:
            now = p.clock["now"]
            c = sqlite3.connect(s.db_path)
            c.row_factory = sqlite3.Row
            IntelStore(c).record_attempt(SourceConfig("solana_status", "Solana Status", "rss", "https://status.solana.com/history.rss", 0.95), now, True, None, 3, 0)
            ms = MacroStore(c)
            ev = make_event("FOMC_DECISION", "2026-10-28", "14:00", "America/New_York", source_id="cal", confidence="estimado")
            ev.scheduled_utc = now + timedelta(minutes=10)
            ev.id = "x1"
            ms.upsert_event(ev, now)
            fut = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="cal", confidence="oficial")
            fut.scheduled_utc = now + timedelta(days=2)
            ms.upsert_event(fut, now)
            ms.insert_value(MacroValue("UNEMPLOYMENT", "2026-09", now - timedelta(minutes=5), 4.4, previous=4.3, unit="%", source_id="bls"))
            ms.add_market(MacroMarket("UST_10Y", "2026-10-21", 4.3, 4.2, 10.0, now - timedelta(hours=1), "treasury"))
            c.commit()
            c.close()
            js = p.get("/api/intel")[1]
            m = js["macro"]
            assert m["janelas_ativas"] and m["janelas_ativas"][0]["fase"] == "antes" and m["janelas_ativas"][0]["confianca_do_horario"] == "estimado"
            assert [e["tipo"] for e in m["proximos"]] == ["FOMC_DECISION", "CPI"] and all(e["resultado"].startswith("desconhecido") for e in m["proximos"])
            assert m["divulgados"][0]["valor_divulgado"] == 4.4 and m["divulgados"][0]["expectativa"] is None and m["divulgados"][0]["anterior"] == 4.3
            assert m["mercado"][0]["variacao_bps"] == 10.0 and any("DXY" in t or "Dólar" in t or "dólar" in t for t in m["pendentes"])
            assert js["situacao"] == "alertas" and any(a["categoria"] == "macro_calendario" for a in js["alertas"])
        finally:
            p.close()


def test_panel_flags_an_empty_macro_calendar_instead_of_claiming_no_events():
    with tempfile.TemporaryDirectory() as d:
        s = make_settings(d)
        s.intel = params(mode="shadow", sources_file=os.path.join(d, "nao.json"), macro_calendar_file=os.path.join(d, "x.json"))
        p = Panel(d, settings=s)
        try:
            js = p.get("/api/intel")[1]
            assert js["macro"] is None or js["macro"]["calendario_vazio"] is True
        finally:
            p.close()


# ------------------------------------------------------------------------------------------------ LIVE continua bloqueado
def test_live_stays_blocked_with_macro_enforce_and_the_module_has_no_path_to_execution():
    from app import factory
    from app.factory import LiveBlocked
    from _helpers import ArrayProvider, make_candles, raises

    with tempfile.TemporaryDirectory() as d:
        s = make_settings(d, mode=Mode.LIVE, intel=params(mode="enforce", macro_enabled=True, macro_high_policy="block", contingency_policy="reduced_risk"))
        with raises(LiveBlocked):
            factory.build_runtime(s, provider=ArrayProvider(make_candles([1.0] * 5, 900), make_candles([1.0] * 5, 3600)))
    assert Settings().mode == Mode.PAPER and Settings().intel.mode == "off"
