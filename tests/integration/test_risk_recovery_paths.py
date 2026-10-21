"""Auditoria de risco/recuperação: limites e kill switch em caminhos de serviço (não só em unidade)."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.config import Settings
from app.domain import Mode, OrderState, Position, Side
from app.execution.base import ExecutionResult, Executor
from app.execution.paper import PaperExecutor
from app.monitoring.alerts import AlertManager
from app.risk.kill_switch import KillSwitch
from app.risk.stops import initial_stop
from app.services.trading_service import TradingService
from app.storage.database import Database
from _helpers import JUP, USDC, ArrayProvider, make_candles, trend_series

SEC = 910  # fechamento do candle de 15m + carência de 5 s do provedor


def _env(tmp, provider, executor_factory=None, mut=None):
    s = Settings(db_path=os.path.join(tmp, "b.sqlite3"))
    s.strategy.ema_trend, s.strategy.min_candles, s.data.history_candles = 20, 100, 400
    if mut:
        mut(s)
    db = Database(s.db_path)
    ex = (executor_factory or (lambda s_, db_: PaperExecutor(s_.execution, 1000.0, lambda i, st, **kw: db_.set_intent_state(i, st, **kw))))(s, db)
    ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
    clock = {"now": None}
    svc = TradingService(s, db, provider, ex, ks, AlertManager(db), {"JUP": JUP}, USDC, clock=lambda: clock["now"])
    return svc, db, ks, ex, clock


def _at(clock, entry, i):
    clock["now"] = (entry.index[i] + pd.Timedelta(seconds=SEC)).to_pydatetime()


def _prov(closes=None):
    entry = make_candles(trend_series(n_down=40) if closes is None else closes, 900)
    return ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600)), entry


def _make_unknown_intent(db, side="BUY", age_s=0.0):
    db.create_intent("u1", side, JUP.mint, "JUP", "live", {"atr": 0.5, "reason": "r"})
    for st in (OrderState.VALIDATED, OrderState.QUOTED, OrderState.SIGNED, OrderState.SUBMITTED, OrderState.UNKNOWN):
        db.set_intent_state("u1", st, signature="SIG" if st == OrderState.SIGNED else None)
    if age_s:
        old = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        db.conn.execute("UPDATE intents SET created_ts=? WHERE id='u1'", (old,))


class ReconExecutor(PaperExecutor):
    """Paper + reconciliação programável (simula o que o executor live devolveria)."""

    def __init__(self, params, cash, db):
        super().__init__(params, cash)
        self.db, self.outcome, self.balance = db, None, None

    def reconcile(self, intent):
        if self.outcome is None:
            return None
        self.db.set_intent_state(intent["id"], OrderState.CONFIRMED, result={"via": "teste"})
        return self.outcome

    def token_balance(self, token):
        return self.balance


def test_safe_mode_recovers_automatically_when_pending_intent_resolves():
    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov, lambda s, db_: ReconExecutor(s.execution, 1000.0, db_))
        _at(clock, entry, 126)
        _make_unknown_intent(db)
        rep = svc.startup()
        assert not rep.ok and not svc.ready  # pendência não resolvida => modo seguro
        for i in range(127, 135):
            _at(clock, entry, i)
            out = svc.run_cycle()
            assert "modo seguro" in out.entries_blocked_reason
        assert not svc.positions and db.conn.execute("SELECT COUNT(*) c FROM intents WHERE id!='u1'").fetchone()["c"] == 0  # nada novo enviado
        ex.outcome = ExecutionResult("u1", OrderState.CONFIRMED, Side.BUY, 5.0, 500.0, 100.0, 0.5)
        _at(clock, entry, 136)
        svc.run_cycle()  # reconcilia, revalida e sai do modo seguro sozinho
        assert svc.ready and list(svc.positions) == [JUP.mint] and svc.positions[JUP.mint].qty == 5.0
        assert db.get_intent("u1")["state"] == "CONFIRMED"


def test_unknown_for_too_long_trips_kill_switch():
    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov, lambda s, db_: ReconExecutor(s.execution, 1000.0, db_))
        _at(clock, entry, 126)
        _make_unknown_intent(db, age_s=svc.s.execution.unknown_kill_after_s + 60)
        svc.startup()
        assert ks.active and "desconhecida" in ks.reason


def test_safe_mode_still_executes_stop_exit_for_open_positions():
    closes = np.full(140, 100.0)
    closes[130:] = 90.0  # queda abaixo do stop (96)
    prov, entry = _prov(closes)
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov)
        db.kill_switch_save(True, "teste", "manual")  # processo reinicia com kill switch ativo
        ks2 = KillSwitch(db.kill_switch_load, db.kill_switch_save)
        svc.ks = ks2
        pos = Position("p1", JUP.mint, "JUP", 2.0, 100.0, "2025-01-01T00:00:00+00:00", 0.1, initial_stop(100.0, 2.0, svc.s.risk))
        db.save_position(pos)
        _at(clock, entry, 135)
        rep = svc.startup()
        assert not rep.ok  # kill switch ativo => modo seguro
        out = svc.run_cycle()
        assert not svc.positions and db.load_positions("CLOSED")[0].exit_reason == "stop"
        assert any("VENDA" in a for a in out.actions)


def test_risk_state_survives_restart_including_cooldown_and_daily_reference():
    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov)
        _at(clock, entry, 126)
        assert svc.startup().ok
        for _ in range(svc.s.risk.max_consecutive_losses):
            svc.risk.record_trade_closed(clock["now"], -5.0)
        svc._persist_state()
        before = svc.risk.state.to_dict()
        db.close()
        svc2, db2, _, _, clock2 = _env(d, prov)
        clock2["now"] = clock["now"]
        assert svc2.risk.state.to_dict() == before and before["day_start_equity"] == 1000.0
        assert any("espera" in r for r in svc2.risk.entry_block_reasons(clock["now"], 1000.0))


def test_daily_loss_limit_blocks_entries_through_the_service_path():
    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov)
        _at(clock, entry, 125)
        assert svc.startup().ok
        ex.cash = 970.0  # -3% no dia (limite 2%) antes do candle de compra
        acts = []
        for i in range(126, 140):
            _at(clock, entry, i)
            acts += svc.run_cycle().actions
        assert not svc.positions and ex.cash == 970.0
        assert any("perda diária" in a for a in acts), acts


def test_cumulative_loss_trips_kill_switch_and_next_start_is_safe_mode():
    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov)
        _at(clock, entry, 125)
        assert svc.startup().ok
        ex.cash = 880.0  # -12% desde o patrimônio inicial (limite 10%)
        _at(clock, entry, 126)
        svc.run_cycle()
        assert ks.active and "perda acumulada" in ks.reason
        db.close()
        svc2, _, ks2, _, clock2 = _env(d, prov)
        clock2["now"] = clock["now"]
        assert ks2.active and not svc2.startup().ok


def test_insufficient_fee_reserve_blocks_entry():
    class NoFees(PaperExecutor):
        def fee_reserve_ok(self):
            return False

    prov, entry = _prov()
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov, lambda s, db_: NoFees(s.execution, 1000.0, lambda i, st, **kw: db_.set_intent_state(i, st, **kw)))
        _at(clock, entry, 125)
        svc.startup()
        svc.ready = True  # o startup reprova por reserva; força o ciclo para provar que a ENTRADA também é bloqueada
        acts = []
        for i in range(126, 140):
            _at(clock, entry, i)
            acts += svc.run_cycle().actions
        assert not svc.positions and any("SOL" in a for a in acts)


def test_local_vs_onchain_balance_divergence_trips_kill_switch():
    prov, entry = _prov(np.full(140, 100.0))
    with tempfile.TemporaryDirectory() as d:
        svc, db, ks, ex, clock = _env(d, prov, lambda s, db_: ReconExecutor(s.execution, 1000.0, db_))
        db.save_position(Position("p1", JUP.mint, "JUP", 10.0, 100.0, "2025-01-01T00:00:00+00:00", 0.1, initial_stop(100.0, 2.0, svc.s.risk)))
        _at(clock, entry, 135)
        assert svc.startup().ok
        ex.balance = 10.0
        svc.run_cycle()
        assert not ks.active  # saldo coincide
        ex.balance = 4.0  # 60% de diferença
        _at(clock, entry, 136)
        svc.run_cycle()
        assert ks.active and "divergência" in ks.reason
