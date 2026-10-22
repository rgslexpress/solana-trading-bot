"""Macro EUA — janelas de risco, eventos futuros, disponibilidade/revisão de valores, política de bloqueio e SEM vazamento de futuro (replay/backtest).

SIMULADO: calendário/valores sintéticos. Não prova que o módulo melhora o resultado de trading; só que ele restringe de forma correta e causal."""

import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

from app.backtest.engine import BacktestEngine
from app.config import IntelParams
from app.intel.engine import IntelEngine
from app.intel.macro import (MacroEngine, MacroMarket, MacroStore, MacroValue, MemoryMacroView, load_macro_archive, make_event)
from app.intel.replay import IntelReplay
from app.intel.shadow import compare_backtests
from app.intel.store import MemoryNewsView
from app.storage.database import Database
from test_intel_replay import BUY_BAR, _close_time, _data, _settings
from _intel import NOW, params, rec

UTC = timezone.utc
FOMC = make_event("FOMC_DECISION", "2026-10-28", "14:00", "America/New_York", source_id="cal", confidence="oficial")  # 18:00 UTC
T0 = FOMC.scheduled_utc


def eng(events=(), values=(), market=(), **kw) -> MacroEngine:
    return MacroEngine(params(**kw), MemoryMacroView(list(events), list(values), list(market)))


def value(ind, period, observed_at, actual, **kw) -> MacroValue:
    return MacroValue(ind, period, observed_at, actual, source_id="t", **kw)


# ------------------------------------------------------------------------------------------------ janelas
def test_high_importance_window_edges_before_and_after_the_release():
    e = eng([FOMC])  # padrão: 30 min antes, 60 min depois
    for dt_min, active, phase in ((-31, False, None), (-30, True, "antes"), (-1, True, "antes"), (0, True, "depois"), (60, True, "depois"), (61, False, None)):
        a = e.assess(T0 + timedelta(minutes=dt_min))
        assert bool(a.windows) is active, dt_min
        if active:
            assert a.windows[0]["fase"] == phase and a.risk_scale == 0.5 and not a.block
        else:
            assert a.risk_scale == 1.0 and not a.alerts


def test_medium_importance_is_informational_only_and_low_has_no_window():
    claims = make_event("JOBLESS_CLAIMS", "2026-10-29", "08:30", "America/New_York", source_id="cal")  # importância 2: 15 min antes / 30 depois
    low = make_event("JOBLESS_CLAIMS", "2026-10-30", "08:30", "America/New_York", source_id="cal", importance=1)
    a = eng([claims, low]).assess(claims.scheduled_utc - timedelta(minutes=10))
    assert a.windows and a.windows[0]["importancia"] == 2 and a.risk_scale == 1.0 and not a.block  # informa, não restringe
    assert not eng([low]).assess(low.scheduled_utc).windows


def test_future_event_registers_only_the_risk_and_never_a_known_result():
    a = eng([FOMC]).assess(T0 - timedelta(minutes=10))
    assert "FUTURO" in a.windows[0]["motivo"] and "DESCONHECIDO" in a.windows[0]["motivo"]
    assert a.divulgados == [] if hasattr(a, "divulgados") else a.released == []  # nenhum valor foi inventado
    nxt = eng([FOMC]).assess(T0 - timedelta(hours=40))
    assert nxt.upcoming and nxt.upcoming[0]["resultado"].startswith("desconhecido") and nxt.upcoming[0]["horario_utc"] == T0.isoformat()
    assert nxt.upcoming[0]["fuso_original"] == "America/New_York" and nxt.upcoming[0]["horario_local"] == "2026-10-28 14:00" and nxt.upcoming[0]["faltam_horas"] == 40.0
    assert not eng([FOMC]).assess(T0 - timedelta(days=30)).upcoming  # fora do horizonte do painel (168 h)


def test_estimated_schedule_is_flagged_and_cancelled_events_are_ignored():
    est = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="cal", confidence="estimado")
    a = eng([est]).assess(est.scheduled_utc - timedelta(minutes=5))
    assert "ESTIMADO" in a.windows[0]["motivo"] and a.alerts[0].certainty == "claim"
    est.status = "cancelled"
    assert not eng([est]).assess(est.scheduled_utc).windows


def test_policy_block_blocks_new_entries_only_inside_the_high_importance_window():
    e = eng([FOMC], macro_high_policy="block")
    inside, outside = e.assess(T0 + timedelta(minutes=5)), e.assess(T0 + timedelta(hours=3))
    assert inside.block and "macro_high_policy=block" in inside.block_reasons[0] and not outside.block
    assert not eng([FOMC]).assess(T0 + timedelta(minutes=5)).block  # padrão: só reduz risco


def test_calendar_empty_is_reported_instead_of_pretending_there_are_no_events():
    a = eng([]).assess(NOW)
    assert a.calendar_empty and a.risk_scale == 1.0 and a.to_public()["calendario_vazio"] is True


def test_risk_scale_is_always_in_zero_one_for_any_calendar_and_configuration():
    rnd = random.Random(11)
    for _ in range(200):
        evs = [make_event(rnd.choice(["FOMC_DECISION", "CPI", "JOBLESS_CLAIMS", "PPI", "EMPLOYMENT_SITUATION"]), f"2026-{rnd.randint(10, 12):02d}-{rnd.randint(1, 28):02d}", "08:30",
                          "America/New_York", source_id="c", confidence=rnd.choice(["oficial", "estimado"]), importance=rnd.randint(1, 3)) for _ in range(rnd.randint(0, 8))]
        e = eng(evs, macro_window_risk_multiplier=rnd.choice([0.05, 0.5, 1.0]), macro_high_policy=rnd.choice(["reduce", "block"]), macro_yield_move_bps=rnd.choice([0.0, 5.0]))
        a = e.assess(datetime(2026, 10, 22, tzinfo=UTC) + timedelta(hours=rnd.uniform(0, 24 * 60)))
        assert 0.0 < a.risk_scale <= 1.0 and (not a.block or a.block_reasons)
        assert set(a.to_public()) >= {"escala_de_risco", "bloqueia", "janelas_ativas", "proximos", "divulgados", "mercado"}


# ------------------------------------------------------------------------------------------------ valores: disponibilidade, expectativa, revisão
def test_released_values_only_exist_for_the_robot_from_the_instant_they_became_available():
    cpi_ev = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="cal", confidence="oficial")
    v = value("CPI", "2026-10", cpi_ev.scheduled_utc + timedelta(minutes=2), 3.4, previous=3.2)
    e = eng([cpi_ev], [v])
    before = e.assess(cpi_ev.scheduled_utc + timedelta(minutes=1))  # a divulgação já ocorreu, o valor ainda NÃO chegou ao robô
    assert before.released == [] and "NÃO disponível" in before.windows[0]["motivo"]
    after = e.assess(cpi_ev.scheduled_utc + timedelta(minutes=3))
    assert [r["valor_divulgado"] for r in after.released] == [3.4] and "valor disponível desde" in after.windows[0]["motivo"]
    assert "bom/ruim" in after.windows[0]["motivo"]  # o valor nunca é interpretado como positivo/negativo


def test_forecast_previous_and_revisions_are_only_present_when_provided():
    t = NOW
    plain = value("UNEMPLOYMENT", "2026-09", t, 4.4)
    assert plain.to_public()["expectativa"] is None and plain.to_public()["surpresa"] is None and plain.to_public()["anterior"] is None
    full = value("CPI", "2026-09", t, 3.4, forecast=3.2, previous=3.1, revised_previous=3.0)
    pub = full.to_public()
    assert abs(pub["surpresa"] - 0.2) < 1e-9 and pub["anterior_revisado"] == 3.0
    a = eng([], [plain, full]).assess(t + timedelta(minutes=1))
    assert {r["indicador"] for r in a.released} == {"UNEMPLOYMENT", "CPI"} and all("tem_expectativa" in r for r in a.released)


def test_store_detects_revisions_of_the_period_and_of_the_previous_value():
    db = Database(":memory:")
    st = MacroStore(db.conn)
    t = NOW
    assert st.insert_value(value("NFP", "2026-08", t, 150.0, previous=100.0))
    assert not st.insert_value(value("NFP", "2026-08", t + timedelta(minutes=5), 150.0, previous=100.0))  # idêntico: não duplica
    assert st.insert_value(value("NFP", "2026-09", t + timedelta(days=30), 120.0, previous=140.0))  # o "anterior" mudou de 150 para 140: revisão do mês anterior
    sep = st.latest_value("NFP", "2026-09")
    assert sep.revised_previous == 140.0 and "revisado" in sep.note
    assert st.insert_value(value("NFP", "2026-09", t + timedelta(days=31), 125.0, previous=140.0))  # o próprio período foi revisado
    latest = st.latest_value("NFP", "2026-09")
    assert latest.is_revision and latest.actual == 125.0 and "125" in latest.note
    assert [v.actual for v in st.values() if v.period == "2026-09"] == [120.0, 125.0]  # histórico de versões preservado


def test_revised_value_is_visible_only_after_its_own_availability_timestamp():
    t = NOW
    first = value("NFP", "2026-09", t, 120.0)
    revised = value("NFP", "2026-09", t + timedelta(days=30), 125.0, is_revision=True)
    view = MemoryMacroView([], [first, revised], [])
    assert [v.actual for v in view.values(t + timedelta(days=1))] == [120.0]  # a revisão futura NÃO vaza
    assert [v.actual for v in view.values(t + timedelta(days=31))] == [125.0]


# ------------------------------------------------------------------------------------------------ mercado
def test_treasury_moves_are_informational_by_default_and_cautious_only_if_configured():
    m = MacroMarket("UST_10Y", "2026-10-21", 4.5, 4.2, 30.0, NOW, "treasury")
    off = eng([], [], [m]).assess(NOW + timedelta(hours=1))
    assert off.market and off.risk_scale == 1.0 and not off.alerts
    on = eng([], [], [m], macro_yield_move_bps=20.0).assess(NOW + timedelta(hours=1))
    assert on.risk_scale == 0.5 and on.alerts[0].category == "macro_mercado" and "30.0 bps" in on.alerts[0].reason
    small = MacroMarket("UST_10Y", "2026-10-21", 4.3, 4.2, 10.0, NOW, "treasury")
    assert eng([], [], [small], macro_yield_move_bps=20.0).assess(NOW + timedelta(hours=1)).risk_scale == 1.0
    assert not eng([], [], [m]).assess(NOW - timedelta(hours=1)).market  # ainda não estava disponível


# ------------------------------------------------------------------------------------------------ integração com o motor de notícias
def test_macro_window_merges_with_news_caution_and_never_raises_risk():
    news = rec("Solana mainnet outage hits validators", minutes_ago=10, now=T0 + timedelta(minutes=5))
    P = params(macro_window_risk_multiplier=0.25, caution_risk_multiplier=0.5)
    engine = IntelEngine(P, MemoryNewsView([news]), MacroEngine(P, MemoryMacroView([FOMC])))
    a = engine.assess("JUP", T0 + timedelta(minutes=5))
    assert a.risk_scale == 0.25 and a.block  # o menor multiplicador vale; o bloqueio por notícia crítica continua
    only_macro = IntelEngine(P, MemoryNewsView([]), MacroEngine(P, MemoryMacroView([FOMC]))).assess("JUP", T0 + timedelta(minutes=5))
    assert only_macro.risk_scale == 0.25 and only_macro.caution and not only_macro.block and only_macro.macro["janelas_ativas"]
    assert any(al.category == "macro_calendario" and al.domain == "macro_us" for al in only_macro.alerts)
    assert IntelEngine(P, MemoryNewsView([]), MacroEngine(P, MemoryMacroView([FOMC]))).assess("JUP", T0 + timedelta(hours=5)).risk_scale == 1.0


# ------------------------------------------------------------------------------------------------ arquivo histórico e replay SEM look-ahead
def test_macro_archive_availability_rules_and_invalid_lines():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "macro.jsonl")
        lines = [
            {"type": "event", "kind": "CPI", "date": "2026-10-13", "time": "08:30", "known_at": "2026-01-02T00:00:00+00:00"},
            {"type": "event", "kind": "CPI", "date": "2026-11-12", "time": "08:30", "known_at": "2026-10-20T00:00:00+00:00"},
            {"type": "value", "indicator": "CPI", "period": "2026-09", "actual": 3.4, "previous": 3.2, "released_at": "2026-10-13T12:30:00+00:00"},
            {"type": "value", "indicator": "CPI", "period": "2026-09", "actual": 3.5, "released_at": "2026-10-13T12:30:00+00:00", "observed_at": "2026-10-13T12:00:00+00:00"},
            {"type": "market", "indicator": "UST_10Y", "obs_date": "2026-10-13", "value": 4.3, "prev_value": 4.2},
            {"type": "market", "indicator": "UST_10Y", "obs_date": "2026-01-13", "value": 4.0},
            {"type": "event", "kind": "NOPE", "date": "2026-10-13", "time": "08:30"}, {"type": "xyz"}, {"type": "value", "indicator": "CPI"},
        ]
        open(path, "w", encoding="utf-8").write("\n".join(json.dumps(x) for x in lines) + "\n{nao é json\n\n")
        evs, vals, mkt = load_macro_archive(path, IntParams())
    assert len(evs) == 2 and len(vals) == 2 and len(mkt) == 2  # linhas inválidas descartadas
    v1 = next(v for v in vals if v.actual == 3.4)
    assert v1.observed_at == datetime(2026, 10, 13, 12, 45, tzinfo=UTC)  # liberação 12:30Z + latência presumida de 900 s
    v2 = next(v for v in vals if v.actual == 3.5)
    assert v2.observed_at == datetime(2026, 10, 13, 12, 30, tzinfo=UTC)  # nunca disponível ANTES da divulgação
    m = next(x for x in mkt if x.obs_date == "2026-10-13")
    assert m.observed_at == datetime(2026, 10, 13, 22, 0, tzinfo=UTC) and abs(m.change_bps - 10.0) < 1e-6  # 18:00 ET (EDT) = 22:00Z
    assert next(x for x in mkt if x.obs_date == "2026-01-13").observed_at == datetime(2026, 1, 13, 23, 0, tzinfo=UTC)  # 18:00 ET (EST) = 23:00Z
    view = MemoryMacroView(evs, vals, mkt)
    assert [e.local_time[:10] for e in view.events(datetime(2026, 10, 21, tzinfo=UTC))] == ["2026-10-13", "2026-11-12"]
    assert [e.local_time[:10] for e in view.events(datetime(2026, 10, 19, tzinfo=UTC))] == ["2026-10-13"]  # agendamento ainda desconhecido


def IntParams():  # noqa: N802
    return IntelParams()


def _settings_macro():
    return _settings()


def test_replay_gate_uses_only_what_existed_at_the_decision_instant():
    data, entry = _data()
    s = _settings_macro()
    decision = _close_time(entry, BUY_BAR)
    cal = make_event("FOMC_DECISION", decision.strftime("%Y-%m-%d"), "00:00", "UTC", source_id="t")
    cal.scheduled_utc = decision + timedelta(minutes=10)  # divulgação 10 min depois da decisão
    late_known = make_event("FOMC_DECISION", decision.strftime("%Y-%m-%d"), "00:00", "UTC", source_id="t")
    late_known.scheduled_utc = decision + timedelta(minutes=10)
    late_known.known_at = decision + timedelta(minutes=5)  # o agendamento só passou a ser conhecido DEPOIS da decisão
    known = IntelReplay([], s.intel, macro_events=[cal]).gate("JUP", decision)
    unknown = IntelReplay([], s.intel, macro_events=[late_known]).gate("JUP", decision)
    assert known.risk_scale == 0.5 and known.macro["janelas_ativas"]
    assert unknown.risk_scale == 1.0 and not unknown.macro["janelas_ativas"]  # sem vazamento do agendamento futuro
    published_after = value("CPI", "2026-09", decision + timedelta(minutes=1), 3.4)
    g = IntelReplay([], s.intel, macro_events=[cal], macro_values=[published_after]).gate("JUP", decision)
    assert g.macro["divulgados"] == []  # valor divulgado/coletado depois da decisão não existe para ela


def test_backtest_with_macro_window_only_restricts_inside_the_window_and_is_deterministic():
    data, entry = _data()
    s = _settings_macro()
    decision = _close_time(entry, BUY_BAR)
    base = BacktestEngine(s, data, 5e6).run()
    first = base.trades.iloc[0]["entry_ts"]

    def run(release_offset_min, policy="reduce"):
        ev = make_event("FOMC_DECISION", "2026-01-02", "10:00", "America/New_York", source_id="t")
        ev.scheduled_utc = decision + timedelta(minutes=release_offset_min)
        s.intel.macro_high_policy = policy
        replay = IntelReplay([], s.intel, macro_events=[ev])
        return BacktestEngine(s, data, 5e6, intel_gate=replay.gate).run()

    reduced = run(10)  # a decisão cai DENTRO da janela (10 min antes da divulgação): risco reduzido
    assert any(e["escala"] == 0.5 and not e["bloqueou"] for e in reduced.intel_events)
    q_base, q_red = float(base.trades.iloc[0]["qty"]), float(reduced.trades.iloc[0]["qty"])
    assert 0 < q_red < q_base  # a posição nunca é MAIOR (aqui o tamanho também é limitado pela exposição máxima, por isso a redução é menor que 50%)
    blocked = run(10, "block")
    assert all(t != first for t in blocked.trades["entry_ts"]) and blocked.rejection_counts.get("notícias: bloqueio de entrada", 0) >= 1
    far = run(60 * 6)  # divulgação 6 h depois: fora da janela => idêntico ao baseline
    assert not far.intel_events and far.metrics["final_equity"] == base.metrics["final_equity"]
    assert run(10).metrics["final_equity"] == reduced.metrics["final_equity"]  # determinístico
    cmp_ = compare_backtests(s, data, 5e6, IntelReplay([], s.intel, macro_events=[_event_at(decision + timedelta(minutes=10))]))
    assert cmp_["entradas_com_risco_reduzido"] >= 1 and "não é evidência" in cmp_["aviso"].lower()


def _event_at(utc):
    ev = make_event("FOMC_DECISION", "2026-01-02", "10:00", "America/New_York", source_id="t")
    ev.scheduled_utc = utc
    return ev
