"""Integração com o serviço de trading (paper): off / shadow / enforce, falhas e garantia de que saídas e stops não são afetados."""

import os
import tempfile

import numpy as np
import pandas as pd

from app.config import Settings
from app.domain import Mode, Position
from app.execution.paper import PaperExecutor
from app.intel.engine import IntelEngine
from app.intel.runtime import IntelRuntime
from app.intel.store import DbNewsView, IntelStore
from app.monitoring.alerts import AlertManager
from app.risk.kill_switch import KillSwitch
from app.risk.stops import initial_stop
from app.services.trading_service import TradingService
from app.storage.database import Database
from _helpers import JUP, USDC, ArrayProvider, make_candles, trend_series
from _intel import FED, GDELT, SOLANA_STATUS, params, rec

SEC = 910
BUY_IDX = 127  # candle em que a estratégia técnica gera BUY nesta série


def _decision_now(entry):
    return (entry.index[BUY_IDX] + pd.Timedelta(seconds=SEC)).to_pydatetime()


def _build(d, mode, news=(), source_ok=True, uncapped=False, entry=None, provider=None, **intel_kw):
    s = Settings(db_path=os.path.join(d, "b.sqlite3"), mode=Mode.PAPER)
    s.strategy.ema_trend, s.strategy.min_candles, s.data.history_candles = 20, 100, 400
    if uncapped:
        s.risk.max_token_exposure_pct = s.risk.max_total_exposure_pct = 100.0
    s.intel = params(mode=mode, stale_after_s=10**8, **intel_kw)
    if entry is None:
        entry = make_candles(trend_series(n_down=40), 900)
        provider = ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600))
    db = Database(s.db_path)
    store = IntelStore(db.conn)
    t0 = (entry.index[BUY_IDX - 2] + pd.Timedelta(seconds=SEC)).to_pydatetime()
    if source_ok:
        store.record_attempt(SOLANA_STATUS, t0, True, None, 5, 0)
    for r in news:
        store.insert(r)
    sources = [SOLANA_STATUS]
    intel = IntelRuntime(s.intel, store, IntelEngine(s.intel, DbNewsView(store, sources, s.intel)), None, sources, []) if mode != "off" else None
    ex = PaperExecutor(s.execution, 1000.0, lambda i, st, **kw: db.set_intent_state(i, st, **kw))
    ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
    clock = {"now": t0}
    svc = TradingService(s, db, provider, ex, ks, AlertManager(db), {"JUP": JUP}, USDC, clock=lambda: clock["now"], intel=intel)
    return svc, db, store, clock, entry


def _run(svc, clock, entry, start=BUY_IDX - 2, stop=BUY_IDX + 6):
    assert svc.startup().ok
    acts = []
    for i in range(start, stop):
        clock["now"] = (entry.index[i] + pd.Timedelta(seconds=SEC)).to_pydatetime()
        acts += svc.run_cycle().actions
    return acts


def _critical(entry):
    return rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, minutes_ago=20, now=_decision_now(entry))


def _positions(db):
    """Todas as posições registradas (abertas OU já encerradas): a estratégia pode comprar e vender dentro da janela do teste."""
    return db.load_positions()


def _rows(db, sql):
    return [dict(r) for r in db.conn.execute(sql)]


def test_off_mode_is_identical_to_baseline_and_writes_nothing_about_news():
    with tempfile.TemporaryDirectory() as d:
        svc, db, _, clock, entry = _build(d, "off")
        _run(svc, clock, entry)
        assert svc.intel is None and len(_positions(db)) >= 1
        assert _rows(db, "SELECT COUNT(*) c FROM intel_decisions")[0]["c"] == 0
        qty_off = _positions(db)[0].qty
    with tempfile.TemporaryDirectory() as d:  # sem nenhuma notícia relevante, enforce não muda a decisão técnica
        svc, db, _, clock, entry = _build(d, "enforce")
        _run(svc, clock, entry)
        assert abs(_positions(db)[0].qty - qty_off) < 1e-12


def test_enforce_blocks_buy_on_confirmed_critical_event_records_reason_and_keeps_strategy_signal():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce")
        store.insert(_critical(entry))
        acts = _run(svc, clock, entry)
        assert not _positions(db) and any("camada de notícias" in a for a in acts)
        sig = _rows(db, "SELECT type, rejections FROM signals WHERE candle_ts LIKE '%#noticias'")
        assert sig and "notícias:" in sig[0]["rejections"]
        base = _rows(db, "SELECT type FROM signals WHERE type='BUY'")
        assert base  # a estratégia técnica continua gerando e registrando BUY (não é reescrita nem silenciada)
        dec = _rows(db, "SELECT * FROM intel_decisions")[0]
        assert dec["action"] == "bloqueou a entrada" and dec["applied"] == 1 and dec["technical"] == "BUY" and "fato confirmado" in dec["rationale"]
        assert _rows(db, "SELECT COUNT(*) c FROM intents")[0]["c"] == 0  # nenhuma ordem criada


def test_shadow_does_not_change_behavior_but_records_what_would_have_changed_and_links_the_position():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow")
        store.insert(_critical(entry))
        _run(svc, clock, entry)
        pos = _positions(db)
        assert len(pos) >= 1  # a compra aconteceu normalmente (observação apenas)
        dec = _rows(db, "SELECT * FROM intel_decisions")[0]
        assert dec["mode"] == "shadow" and dec["would_change"] == 1 and dec["applied"] == 0 and dec["action"].startswith("SHADOW: bloquearia")
        assert dec["position_id"] == pos[0].id  # permite comparar o resultado depois


def test_caution_reduces_risk_never_increases_it_and_respects_exposure_limits():
    with tempfile.TemporaryDirectory() as d:
        svc, db, _st, clock, entry = _build(d, "off", uncapped=True)
        _run(svc, clock, entry)
        base_qty = _positions(db)[0].qty
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce", uncapped=True, caution_risk_multiplier=0.5)
        store.insert(rec("FOMC raises interest rates by 25 bps", FED, minutes_ago=20, now=_decision_now(entry)))
        _run(svc, clock, entry)
        qty = _positions(db)[0].qty
        assert 0.4 * base_qty < qty < 0.6 * base_qty  # risco pela metade
        dec = _rows(db, "SELECT * FROM intel_decisions")[0]
        assert dec["risk_scale"] == 0.5 and dec["action"] == "reduziu o risco" and dec["applied"] == 1
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow", uncapped=True)
        store.insert(rec("FOMC raises interest rates by 25 bps", FED, minutes_ago=20, now=_decision_now(entry)))
        _run(svc, clock, entry)
        assert abs(_positions(db)[0].qty - base_qty) < 1e-12  # shadow não altera o tamanho


def test_unavailable_sources_block_in_enforce_but_not_in_shadow_and_are_recorded():
    with tempfile.TemporaryDirectory() as d:
        svc, db, _, clock, entry = _build(d, "enforce", source_ok=False)
        acts = _run(svc, clock, entry)
        assert not _positions(db) and any("INDISPONÍVEIS" in a for a in acts)
        assert _rows(db, "SELECT data_state FROM intel_decisions")[0]["data_state"] == "unavailable"
    with tempfile.TemporaryDirectory() as d:
        svc, db, _, clock, entry = _build(d, "shadow", source_ok=False)
        _run(svc, clock, entry)
        assert _positions(db) and _rows(db, "SELECT would_change FROM intel_decisions")[0]["would_change"] == 1
    with tempfile.TemporaryDirectory() as d:
        svc, db, _, clock, entry = _build(d, "enforce", source_ok=False, unavailable_policy="warn_only")
        _run(svc, clock, entry)
        assert _positions(db)  # política configurada: só avisa


def test_news_published_after_the_decision_instant_cannot_affect_it():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce")
        late = rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, minutes_ago=-45, now=_decision_now(entry))  # publicada 45 min DEPOIS da decisão
        store.insert(late)
        _run(svc, clock, entry, stop=BUY_IDX + 1)
        assert _positions(db)  # notícia "do futuro" não bloqueou a compra
        assert _rows(db, "SELECT action FROM intel_decisions")[0]["action"] == "sem efeito"


def test_exits_and_stops_are_never_blocked_by_news_even_with_critical_alert_active():
    closes = np.full(140, 100.0)
    closes[130:] = 90.0
    entry = make_candles(closes, 900)
    prov = ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce", entry=entry, provider=prov)
        pos = Position("p1", JUP.mint, "JUP", 2.0, 100.0, "2025-01-01T00:00:00+00:00", 0.1, initial_stop(100.0, 2.0, svc.s.risk))
        db.save_position(pos)
        store.insert(rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, minutes_ago=10, now=(entry.index[135] + pd.Timedelta(seconds=SEC)).to_pydatetime()))
        clock["now"] = (entry.index[135] + pd.Timedelta(seconds=SEC)).to_pydatetime()
        assert svc.startup().ok
        out = svc.run_cycle()
        assert not svc.positions and db.load_positions("CLOSED")[0].exit_reason == "stop"  # stop executou normalmente
        assert any("VENDA" in a for a in out.actions)


def test_internal_failures_are_isolated_and_fail_safe_for_entries():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce")

        def boom(*a, **k):
            raise RuntimeError("bug interno")

        svc.intel.engine.assess = boom  # type: ignore[method-assign]

        class BadCollector:
            def refresh_if_due(self, now):
                raise RuntimeError("coletor quebrou")

        svc.intel.collector = BadCollector()  # type: ignore[assignment]
        acts = _run(svc, clock, entry)  # o ciclo NÃO propaga a exceção
        assert not _positions(db) and any("camada de notícias" in a for a in acts)  # conservador: bloqueia entrada em enforce
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow")
        svc.intel.engine.assess = boom  # type: ignore[method-assign]
        _run(svc, clock, entry)
        assert _positions(db)  # shadow nunca altera o comportamento, mesmo com falha interna


def test_kill_switch_and_risk_limits_still_apply_with_intel_enabled():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow")
        svc.ks.trip("teste")
        assert not svc.startup().ok  # kill switch ativo => modo seguro (sem entradas), como antes
        for i in range(BUY_IDX - 2, BUY_IDX + 6):
            clock["now"] = (entry.index[i] + pd.Timedelta(seconds=SEC)).to_pydatetime()
            svc.run_cycle()
        assert not _positions(db)  # kill switch continua mandando
        assert _rows(db, "SELECT COUNT(*) c FROM intel_decisions")[0]["c"] == 0  # nem chega a consultar notícias


def test_gdelt_only_sources_cannot_block_even_with_scary_headlines():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "enforce")
        store.insert(rec("Solana mainnet outage confirmed, validators offline", GDELT, minutes_ago=20, now=_decision_now(entry)))
        acts = _run(svc, clock, entry)
        assert _positions(db) and not any("bloqueado pela camada" in a for a in acts)  # fonte agregadora de baixa confiança não bloqueia
        assert _rows(db, "SELECT action FROM intel_decisions")[0]["action"] == "reduziu o risco"  # apenas cautela
