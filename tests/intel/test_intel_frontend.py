"""Frontend (Node + DOM mínimo, NÃO é um navegador real): a seção News & Market Intelligence renderiza saúde por fonte, contingência e macro sem exceções,
com texto como DADO (sem HTML dinâmico)."""

import shutil
import tempfile
from datetime import timedelta

from test_dashboard_frontend import APP, _run
from test_intel_health_contingency import _critical_down_for_at, _panel_with_state, ok
from app.intel.macro import MacroMarket, MacroStore, MacroValue, make_event
from _intel import FED, SOLANA_STATUS


def test_frontend_renders_health_contingency_events_and_macro_sections():
    if not shutil.which("node"):
        print("IGNORADO: node não encontrado")
        return

    def writer(store, P, now):
        _critical_down_for_at(store, P, now, 8)
        store.log_event(now - timedelta(hours=2), "contingencia_iniciada", None, "alerta", "CONTINGÊNCIA de teste")
        ms = MacroStore(store.c)
        ev = make_event("FOMC_DECISION", "2026-10-28", "14:00", "America/New_York", source_id="cal", confidence="estimado")
        ev.scheduled_utc, ev.id = now + timedelta(minutes=10), "w1"
        ms.upsert_event(ev, now)
        ms.insert_value(MacroValue("UNEMPLOYMENT", "2026-09", now - timedelta(minutes=5), 4.4, previous=4.3, unit="%", source_id="bls"))
        ms.add_market(MacroMarket("UST_10Y", "2026-10-21", 4.3, 4.2, 10.0, now - timedelta(hours=1), "treasury"))

    with tempfile.TemporaryDirectory() as d:
        p, _ = _panel_with_state(d, writer)
        try:
            out = _run(p)
            assert "erro" not in out and not out["intel_msg"].startswith("Não foi possível"), out
            assert "CONTINGÊNCIA" in out["intel_conting"] and out["intel_conting_oculto"] is False and "keep_restriction" in out["intel_conting"]
            assert "CONTINGENCIA" in out["intel_saude"].upper() and "Solana Status" in out["intel_fontes"] and "SIM" in out["intel_fontes"] and "INDISPONÍVEL" in out["intel_fontes"]
            assert "contingencia_iniciada" in out["intel_eventos"]
            assert "janela(s) de risco macro" in out["macro_msg"] and "antes da divulgação" in out["macro_janelas"] and "desconhecido" in out["macro_proximos"]
            assert "UNEMPLOYMENT" in out["macro_divulgados"] and "N/D" in out["macro_divulgados"] and "UST_10Y" in out["macro_mercado"] and "DXY" in out["macro_pendentes"]
        finally:
            p.close()


def test_frontend_with_healthy_sources_hides_the_contingency_box():
    if not shutil.which("node"):
        print("IGNORADO: node não encontrado")
        return

    def writer(store, P, now):
        ok(store, SOLANA_STATUS, now - timedelta(minutes=1), P)
        ok(store, FED, now - timedelta(minutes=1), P)

    with tempfile.TemporaryDirectory() as d:
        p, _ = _panel_with_state(d, writer)
        try:
            out = _run(p)
            assert out["intel_conting_oculto"] is True and "SAUDAVEL" in out["intel_saude"].upper().replace("Á", "A") and "Nenhum evento relevante" in out["intel_msg"]
            assert "Calendário macro VAZIO" in out["macro_msg"]  # sem calendário: avisa, não finge ausência de eventos
        finally:
            p.close()


def test_new_frontend_code_keeps_the_no_innerhtml_rule_and_links_are_http_only():
    js = (APP / "app.js").read_text(encoding="utf-8")
    assert ".innerHTML" not in js and "renderMacro" in js and "linkOrText" in js and 'rel: "noopener noreferrer"' in js
