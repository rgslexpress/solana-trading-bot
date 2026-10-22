"""P0.4 — Saúde individual das fontes; P0.5 — indisponibilidade prolongada e contingência (nunca libera operações pela passagem do tempo).

SIMULADO: tempo e falhas injetados no armazenamento/transporte falso. Não representa o comportamento de fontes reais."""

import json
import os
import tempfile
from datetime import timedelta

from app.config import Settings, validate_settings
from app.intel.collector import NewsCollector
from app.intel.engine import IntelEngine
from app.intel.runtime import IntelRuntime
from app.intel.store import DbNewsView, IntelStore, compute_source_states
from app.storage.database import Database
from harness import Panel, make_settings
from _intel import CLF, FED, FakeFeeds, NOW, SOLANA_STATUS, TIMEOUT, ok_xml, params, rss

SOURCES = [SOLANA_STATUS, FED]  # Solana Status = CRÍTICA; Fed = secundária


def env(**kw):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    P = params(stale_after_s=1800, **kw)
    eng = IntelEngine(P, DbNewsView(store, SOURCES, P))
    return db, store, P, eng


def ok(store, src, t, P, items=3):
    return store.record_attempt(src, t, True, None, items, 0, recovery_min=P.recovery_min_successes)


def fail(store, src, t, P, kind="network", msg="rede", status=None):
    return store.record_attempt(src, t, False, msg, 0, 0, error_kind=kind, http_status=status, recovery_min=P.recovery_min_successes)


def states(store, P, t):
    return {s.source_id: s for s in compute_source_states(SOURCES, store.status_rows(), t, P)}


# ------------------------------------------------------------------------------------------------ P0.4 — saúde individual
def test_each_source_has_its_own_state_and_a_critical_outage_is_never_hidden_by_a_healthy_secondary():
    db, store, P, eng = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(hours=1), P)
    ok(store, FED, NOW - timedelta(minutes=2), P)
    for k in range(3):
        fail(store, SOLANA_STATUS, NOW - timedelta(minutes=30 - k), P)  # a crítica cai
    st = states(store, P, NOW)
    assert st["solana_status"].state == "unavailable" and st["fed_monetary"].state == "ok" and st["solana_status"].critical and not st["fed_monetary"].critical
    a = eng.assess("JUP", NOW)
    assert a.data_state == "unavailable" and a.block and not a.no_events  # A3: NÃO fica "ok" só porque o Fed respondeu
    h = eng.health(NOW)
    assert h["geral"] == "critico" and "Solana Status" in h["mensagem"] and h["criticas_com_problema"] == ["Solana Status"]


def test_secondary_source_outage_degrades_but_does_not_make_the_module_critical():
    db, store, P, eng = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(minutes=1), P)
    for k in range(3):
        fail(store, FED, NOW - timedelta(minutes=10 - k), P)
    a = eng.assess("JUP", NOW)
    h = eng.health(NOW)
    assert a.data_state == "ok" and h["geral"] == "degradado" and h["secundarias_com_problema"] == ["Federal Reserve"] and not h["criticas_com_problema"]
    assert any("Federal Reserve" in r for r in a.rationale[0:1])  # a nota da fonte secundária aparece na justificativa


def test_all_healthy_is_the_only_way_to_be_saudavel():
    db, store, P, eng = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(minutes=1), P)
    ok(store, FED, NOW - timedelta(minutes=1), P)
    assert eng.health(NOW)["geral"] == "saudavel" and eng.assess("JUP", NOW).no_events


def test_unavailable_stale_invalid_and_healthy_are_distinguished():
    db, store, P, _ = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(minutes=1), P)  # saudável
    ok(store, FED, NOW - timedelta(hours=2), P)  # respondeu há 2 h e depois não foi mais consultada: DESATUALIZADA (sem falhas)
    assert states(store, P, NOW)["fed_monetary"].state == "stale"
    assert states(store, P, NOW)["solana_status"].state == "ok"
    for k in range(3):
        fail(store, FED, NOW - timedelta(minutes=5 - k), P, kind="parse", msg="XML inválido")  # responde com conteúdo inválido
    assert states(store, P, NOW)["fed_monetary"].state == "invalid"  # RESPOSTA INVÁLIDA ≠ indisponível
    for k in range(3):
        fail(store, FED, NOW - timedelta(minutes=2 - k * 0.5), P, kind="http", msg="HTTP 503", status=503)
    s = states(store, P, NOW)["fed_monetary"]
    assert s.state == "unavailable" and s.error_kind == "http" and s.http_status == 503 and s.consecutive_failures >= 3 and s.down_since


def test_short_outage_with_fresh_data_is_tolerated_but_three_consecutive_failures_are_not():
    db, store, P, eng = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(minutes=10), P)
    ok(store, FED, NOW - timedelta(minutes=10), P)
    fail(store, SOLANA_STATUS, NOW - timedelta(minutes=5), P)
    fail(store, SOLANA_STATUS, NOW - timedelta(minutes=3), P)
    assert states(store, P, NOW)["solana_status"].state == "ok"  # 2 falhas com dados ainda frescos
    fail(store, SOLANA_STATUS, NOW - timedelta(minutes=1), P)
    assert states(store, P, NOW)["solana_status"].state == "unavailable"


def test_per_source_stale_threshold_overrides_the_global_one():
    from dataclasses import replace

    db, store, P, _ = env()
    ok(store, SOLANA_STATUS, NOW - timedelta(hours=3), P)
    slow = replace(SOLANA_STATUS, stale_after_s=86400.0)
    s = compute_source_states([slow], store.status_rows(), NOW, P)[0]
    assert s.state == "ok" and s.stale_after_s == 86400.0
    assert compute_source_states([SOLANA_STATUS], store.status_rows(), NOW, P)[0].state == "stale"


# ------------------------------------------------------------------------------------------------ P0.5 — contingência
def _critical_down_for(store, P, hours):
    """A fonte crítica fica fora do ar durante `hours` (falhas a cada 5 min, até NOW)."""
    ok(store, SOLANA_STATUS, NOW - timedelta(hours=hours + 1), P)
    ok(store, FED, NOW - timedelta(minutes=1), P)
    t = NOW - timedelta(hours=hours)
    while t <= NOW:
        fail(store, SOLANA_STATUS, t, P)
        t += timedelta(minutes=30)


def test_short_unavailability_is_not_contingency_but_blocks_per_policy():
    db, store, P, eng = env()
    _critical_down_for(store, P, 2)
    a = eng.assess("JUP", NOW)
    assert a.data_state == "unavailable" and a.contingency is None and a.block and eng.health(NOW)["geral"] == "critico"


def test_over_six_hours_enters_contingency_and_keeps_the_restriction_by_default():
    db, store, P, eng = env()
    _critical_down_for(store, P, 7)
    a = eng.assess("JUP", NOW)
    c = a.contingency
    assert a.data_state == "contingency" and c and c["horas"] >= 6 and c["politica"] == "keep_restriction" and c["fontes"] == ["Solana Status"]
    assert a.block and any("contingência" in r.lower() for r in a.block_reasons)  # restrição MANTIDA
    assert eng.health(NOW)["geral"] == "contingencia"
    assert P.contingency_policy == "keep_restriction"  # padrão conservador: nenhuma política de liberação foi inventada


def test_time_alone_never_releases_operations():
    """A passagem do tempo (horas, dias) com a fonte crítica ainda fora NÃO libera as entradas."""
    db, store, P, eng = env()
    _critical_down_for(store, P, 7)
    for extra_h in (0, 24, 72, 24 * 30):
        a = eng.assess("JUP", NOW + timedelta(hours=extra_h))
        assert a.block and a.data_state == "contingency" and a.risk_scale <= 1.0, extra_h


def test_explicit_reduced_risk_policy_releases_entries_only_with_reduced_risk_and_says_so():
    db, store, P, eng = env(contingency_policy="reduced_risk", contingency_risk_multiplier=0.25)
    _critical_down_for(store, P, 7)
    a = eng.assess("JUP", NOW)
    assert a.data_state == "contingency" and not a.block and a.risk_scale == 0.25 and a.would_change
    assert any("POLÍTICA DE CONTINGÊNCIA 'reduced_risk'" in r for r in a.rationale)  # visível/auditável
    assert eng.assess("JUP", NOW).risk_scale <= 1.0


def test_warn_only_policy_marks_contingency_without_blocking_when_the_operator_chose_it():
    db, store, P, eng = env(unavailable_policy="warn_only")
    _critical_down_for(store, P, 7)
    a = eng.assess("JUP", NOW)
    assert a.data_state == "contingency" and not a.block and any("CONTINGÊNCIA registrada" in r for r in a.rationale)


def test_config_rejects_contingency_policies_that_could_release_or_raise_risk():
    s = Settings()
    assert s.intel.contingency_policy == "keep_restriction" and s.intel.contingency_after_s == 21600.0 and validate_settings(s) == []
    for kw in ({"contingency_policy": "release"}, {"contingency_policy": "auto"}, {"contingency_risk_multiplier": 1.5}, {"contingency_risk_multiplier": 0.0}, {"contingency_after_s": 0.0},
               {"macro_window_risk_multiplier": 2.0}, {"recovery_min_successes": 0}):
        t = Settings()
        for k, v in kw.items():
            setattr(t.intel, k, v)
        assert validate_settings(t), kw


def test_partial_recovery_keeps_critical_source_flagged_and_restriction_until_confirmed():
    db, store, P, eng = env()
    _critical_down_for(store, P, 7)
    assert eng.assess("JUP", NOW).data_state == "contingency"
    # a fonte SECUNDÁRIA continua ok; a crítica volta a responder UMA vez: recuperação parcial (1/2)
    ok(store, SOLANA_STATUS, NOW + timedelta(minutes=5), P)
    t = NOW + timedelta(minutes=6)
    s = states(store, P, t)["solana_status"]
    assert s.state == "unavailable" and s.recovering == "1/2"
    a = eng.assess("JUP", t)
    assert a.block and a.data_state in ("contingency", "unavailable")
    # recuperação completa: segundo sucesso consecutivo
    ok(store, SOLANA_STATUS, NOW + timedelta(minutes=10), P)
    t2 = NOW + timedelta(minutes=11)
    assert states(store, P, t2)["solana_status"].state == "ok"
    a2 = eng.assess("JUP", t2)
    assert not a2.block and a2.data_state == "ok" and a2.contingency is None and eng.health(t2)["geral"] == "saudavel"


def test_flapping_during_recovery_makes_the_outage_firm_again():
    db, store, P, eng = env()
    _critical_down_for(store, P, 7)
    ok(store, SOLANA_STATUS, NOW + timedelta(minutes=5), P)  # volta 1x
    fail(store, SOLANA_STATUS, NOW + timedelta(minutes=10), P)  # oscila
    s = states(store, P, NOW + timedelta(minutes=11))["solana_status"]
    assert s.state == "unavailable" and s.consecutive_failures >= 3
    assert eng.assess("JUP", NOW + timedelta(minutes=11)).block


def test_secondary_recovery_alone_does_not_clear_a_critical_outage():
    db, store, P, eng = env()
    _critical_down_for(store, P, 3)
    for k in range(3):
        fail(store, FED, NOW - timedelta(minutes=20 - k), P)
    ok(store, FED, NOW + timedelta(minutes=1), P)
    ok(store, FED, NOW + timedelta(minutes=2), P)
    a = eng.assess("JUP", NOW + timedelta(minutes=3))
    assert a.block and eng.health(NOW + timedelta(minutes=3))["geral"] == "critico"


def test_health_events_are_logged_for_failure_partial_and_full_recovery():
    db = Database(":memory:")
    store = IntelStore(db.conn)
    t = FakeFeeds({SOLANA_STATUS.url: [TIMEOUT, TIMEOUT, TIMEOUT, ok_xml(rss([])), ok_xml(rss([]))]})
    P = params(stale_after_s=1800, refresh_interval_s=1)

    class Mono:
        v = 0.0

        def __call__(self):
            return self.v

    mono = Mono()
    c = NewsCollector(P, [SOLANA_STATUS], t, store, CLF, mono)
    for i in range(5):
        mono.v += 700  # passa do circuito aberto
        c.refresh(NOW + timedelta(minutes=10 * i))
    kinds = [e["tipo"] for e in reversed(store.events(20))]
    assert kinds == ["fonte_indisponivel", "recuperacao_parcial", "fonte_recuperada"]
    assert all(e["fonte"] == "solana_status" for e in store.events(20))


def test_runtime_logs_contingency_start_and_end_with_the_policy_applied():
    db, store, P, eng = env()
    rt = IntelRuntime(P, store, eng, None, SOURCES, [])
    _critical_down_for(store, P, 7)
    rt._after_apply(NOW)
    ok(store, SOLANA_STATUS, NOW + timedelta(minutes=5), P)
    ok(store, SOLANA_STATUS, NOW + timedelta(minutes=10), P)
    rt._after_apply(NOW + timedelta(minutes=11))
    ev = list(reversed(store.events(20)))
    assert [e["tipo"] for e in ev] == ["contingencia_iniciada", "contingencia_encerrada"]
    assert "keep_restriction" in ev[0]["detalhe"] and "Política aplicada" in ev[0]["detalhe"]


# ------------------------------------------------------------------------------------------------ painel
def _panel_with_state(d, writer, **intel_kw):
    cat = os.path.join(d, "intel_sources.json")
    json.dump({"sources": [
        {"id": "solana_status", "name": "Solana Status", "kind": "rss", "url": SOLANA_STATUS.url, "reliability": 0.95, "official": True, "assets": ["SOL"], "critical": True,
         "domain": "solana_status", "status_format": "statuspage"},
        {"id": "fed_monetary", "name": "Federal Reserve", "kind": "rss", "url": FED.url, "reliability": 0.95, "official": True, "domain": "macro_us"}]}, open(cat, "w"))
    s = make_settings(d)
    s.intel = params(mode="shadow", sources_file=cat, stale_after_s=1800, macro_calendar_file=os.path.join(d, "nao_existe.json"), **intel_kw)
    p = Panel(d, settings=s)
    now = p.clock["now"]
    import sqlite3

    c = sqlite3.connect(s.db_path)
    c.row_factory = sqlite3.Row
    writer(IntelStore(c), s.intel, now)
    c.commit()
    c.close()
    return p, now


def test_panel_shows_each_source_and_the_critical_outage_even_when_the_fed_is_fine():
    def writer(store, P, now):
        ok(store, FED, now - timedelta(minutes=1), P)
        ok(store, SOLANA_STATUS, now - timedelta(hours=2), P)
        for k in range(4):
            fail(store, SOLANA_STATUS, now - timedelta(minutes=40 - k * 10), P, kind="http", msg="HTTP 503", status=503)

    with tempfile.TemporaryDirectory() as d:
        p, now = _panel_with_state(d, writer)
        try:
            js = p.get("/api/intel")[1]
            assert js["situacao"] == "fontes_indisponiveis" and js["saude"]["geral"] == "critico" and "NÃO está saudável" in js["saude"]["mensagem"]
            by = {f["source_id"]: f for f in js["fontes"]}
            assert by["solana_status"]["state"] == "unavailable" and by["solana_status"]["critical"] is True and by["solana_status"]["error_kind"] == "http" and by["solana_status"]["http_status"] == 503
            assert by["solana_status"]["down_since"] and by["solana_status"]["domain"] == "solana_status" and by["fed_monetary"]["state"] == "ok"
            assert js["contingencia"] is None
        finally:
            p.close()


def test_panel_shows_contingency_with_duration_policy_and_affected_decisions():
    def writer(store, P, now):
        _critical_down_for_at(store, P, now, 8)
        for i in range(3):  # decisões de entrada tomadas durante a indisponibilidade (afetadas)
            store.add_decision({"ts": (now - timedelta(hours=i)).isoformat(), "symbol": "JUP", "technical": "BUY", "mode": "shadow", "data_state": "contingency", "action": "SHADOW: bloquearia",
                                "would_change": True, "applied": False, "risk_scale": 1.0})
        store.log_event(now - timedelta(hours=2), "contingencia_iniciada", None, "alerta", "teste")

    with tempfile.TemporaryDirectory() as d:
        p, now = _panel_with_state(d, writer)
        try:
            js = p.get("/api/intel")[1]
            c = js["contingencia"]
            assert js["saude"]["geral"] == "contingencia" and c and c["horas"] >= 6 and c["politica"] == "keep_restriction" and c["decisoes_afetadas"] == 3
            assert "NÃO libera" in c["mensagem_politica"] or "não libera" in c["mensagem_politica"]
            assert js["estado_dados"] == "contingency" and "NÃO significa" in js["mensagem"]
            assert any(e["tipo"] == "contingencia_iniciada" for e in js["eventos"])
        finally:
            p.close()


def _critical_down_for_at(store, P, now, hours):
    ok(store, SOLANA_STATUS, now - timedelta(hours=hours + 1), P)
    ok(store, FED, now - timedelta(minutes=1), P)
    t = now - timedelta(hours=hours)
    while t <= now:
        fail(store, SOLANA_STATUS, t, P)
        t += timedelta(minutes=30)
