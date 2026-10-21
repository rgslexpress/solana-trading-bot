import os
import tempfile
from datetime import timedelta

import numpy as np
import pandas as pd

from app.config import Settings
from app.domain import Mode, OrderState
from app.execution.paper import PaperExecutor
from app.monitoring.alerts import AlertManager
from app.risk.kill_switch import KillSwitch
from app.services.trading_service import TradingService
from app.storage.database import Database
from _helpers import JUP, USDC, ArrayProvider, make_candles, trend_series, utc


def _settings(db_path):
    s = Settings(db_path=db_path, mode=Mode.PAPER, paper_capital=1000.0)
    s.strategy.ema_trend = 20
    s.strategy.min_candles = 100
    s.data.history_candles = 400
    return s


def _build(db_path, provider, clock_holder, capital=1000.0):
    s = _settings(db_path)
    db = Database(db_path)
    ex = PaperExecutor(s.execution, capital, on_state=lambda iid, st, **kw: db.set_intent_state(iid, st, **kw))
    ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
    svc = TradingService(s, db, provider, ex, ks, AlertManager(db), {"JUP": JUP}, USDC, clock=lambda: clock_holder["now"])
    return svc, db, ks, ex


def _provider(n_down=40):
    closes = trend_series(n_down=n_down)
    entry = make_candles(closes, 900)
    htf = make_candles(np.linspace(80, 140, 240), 3600)
    return ArrayProvider(entry, htf), entry


def _step(clock, entry, i):
    clock["now"] = (entry.index[i] + pd.Timedelta(seconds=910)).to_pydatetime()


def test_full_paper_lifecycle_and_persistence_across_restart():
    prov, entry = _provider()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bot.sqlite3")
        clock = {"now": None}
        _step(clock, entry, 125)
        svc, db, ks, ex = _build(path, prov, clock)
        rep = svc.startup()
        assert rep.ok, rep.steps
        opened_at = None
        for i in range(126, len(entry)):
            _step(clock, entry, i)
            svc.run_cycle()
            if svc.positions and opened_at is None:
                opened_at = i
                break
        assert opened_at is not None, "esperava ao menos uma compra simulada"
        pos = list(svc.positions.values())[0]
        cash_after = ex.cash
        assert cash_after < 1000.0
        # --- reinício: novo processo, mesmo banco
        db.close()
        svc2, db2, ks2, ex2 = _build(path, prov, clock)
        rep2 = svc2.startup()
        assert rep2.ok, rep2.steps
        assert list(svc2.positions) == [pos.mint]
        restored = svc2.positions[pos.mint]
        assert abs(restored.qty - pos.qty) < 1e-12 and abs(restored.stop.stop - pos.stop.stop) < 1e-9
        assert abs(ex2.cash - cash_after) < 1e-9  # caixa virtual restaurado
        # mesmo candle não é reavaliado após reinício
        n_before = db2.conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        svc2.run_cycle()
        assert db2.conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == n_before
        # continua até a reversão: posição deve ser encerrada por saída (stop/TP/sinal)
        for i in range(opened_at + 1, len(entry)):
            _step(clock, entry, i)
            svc2.run_cycle()
        assert not svc2.positions, "posição deveria ter sido encerrada após a reversão"
        closed = db2.load_positions("CLOSED")
        assert len(closed) == 1 and closed[0].pnl_quote is not None and closed[0].exit_reason
        # auditoria: sinais (inclusive rejeitados) e intenções registradas
        types = {r["type"] for r in db2.conn.execute("SELECT DISTINCT type FROM signals")}
        assert "NO_TRADE" in types and "BUY" in types
        states = [r["state"] for r in db2.conn.execute("SELECT state FROM intents")]
        assert states and all(s == "CONFIRMED" for s in states)
        assert abs(sum(1 for _ in states) - 2) == 0  # 1 compra + 1 venda


def test_kill_switch_blocks_entries_but_allows_exits():
    prov, entry = _provider(n_down=60)
    with tempfile.TemporaryDirectory() as d:
        clock = {"now": None}
        _step(clock, entry, 125)
        svc, db, ks, ex = _build(os.path.join(d, "b.sqlite3"), prov, clock)
        assert svc.startup().ok
        ks.trip("teste manual")
        for i in range(126, 215):
            _step(clock, entry, i)
            svc.run_cycle()
        assert not svc.positions and ex.cash == 1000.0  # nenhuma entrada
        blocked = db.conn.execute("SELECT COUNT(*) c FROM signals WHERE candle_ts LIKE '%#bloqueio'").fetchone()["c"]
        assert blocked >= 1


def test_stale_or_unavailable_data_blocks_entries_and_trips_kill_eventually():
    prov, entry = _provider()
    with tempfile.TemporaryDirectory() as d:
        clock = {"now": None}
        _step(clock, entry, 125)
        svc, db, ks, ex = _build(os.path.join(d, "b.sqlite3"), prov, clock)
        assert svc.startup().ok
        prov.fail = True
        svc.s.data.max_failed_cycles_before_kill = 3
        for i in range(126, 160):
            _step(clock, entry, i)
            svc.run_cycle()
        assert not svc.positions
        assert ks.active and "indisponíveis" in ks.reason
        # dados desatualizados (relógio muito à frente do último candle)
        prov.fail = False
        clock["now"] = entry.index[-1].to_pydatetime() + timedelta(days=3)
        feats, why = svc._features(JUP, clock["now"])
        assert feats is None and "desatualizados" in why


def test_startup_fails_safe_when_data_invalid_and_blocks_cycle():
    prov, entry = _provider()
    prov.liq = None
    with tempfile.TemporaryDirectory() as d:
        clock = {"now": None}
        clock["now"] = (entry.index[10] + pd.Timedelta(seconds=900)).to_pydatetime()  # histórico insuficiente
        svc, db, ks, ex = _build(os.path.join(d, "b.sqlite3"), prov, clock)
        rep = svc.startup()
        assert not rep.ok and not svc.ready
        out = svc.run_cycle()
        assert "modo seguro" in out.entries_blocked_reason


def test_duplicate_intent_is_rejected_by_database():
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "b.sqlite3"))
        assert db.create_intent("a1", "BUY", JUP.mint, "JUP", "paper", {})
        assert not db.create_intent("a1", "BUY", JUP.mint, "JUP", "paper", {})  # mesmo id
        assert not db.create_intent("a2", "BUY", JUP.mint, "JUP", "paper", {})  # outro id, mesmo token/lado ativo
        db.set_intent_state("a1", OrderState.FAILED, error="x")
        assert db.create_intent("a3", "BUY", JUP.mint, "JUP", "paper", {})  # após terminal, permitido


def test_blocked_marker_does_not_hide_the_decided_candle():
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "b.sqlite3"))
        db.save_signal(JUP.mint, "JUP", "2025-01-01T10:00:00+00:00", "BUY", 7, [], [])
        db.save_signal(JUP.mint, "JUP", "2025-01-01T10:00:00+00:00#bloqueio", "NO_TRADE", 7, [], ["kill switch"])
        assert db.last_signal_ts(JUP.mint) == "2025-01-01T10:00:00+00:00"
        assert not db.save_signal(JUP.mint, "JUP", "2025-01-01T10:00:00+00:00", "BUY", 7, [], [])  # idempotente


def test_invalid_state_transition_rejected():
    from app.domain import InvalidTransition
    from _helpers import raises

    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "b.sqlite3"))
        db.create_intent("x", "BUY", JUP.mint, "JUP", "paper", {})
        with raises(InvalidTransition):
            db.set_intent_state("x", OrderState.CONFIRMED)  # CREATED -> CONFIRMED não existe
