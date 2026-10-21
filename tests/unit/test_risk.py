from datetime import timedelta

from app.config import ExecutionParams, RiskParams
from app.domain import Position
from app.execution.costs import CostModel
from app.risk.kill_switch import RESUME_PHRASE, KillSwitch
from app.risk.risk_manager import RiskManager
from app.risk.sizing import size_position
from app.risk.stops import check_exit, initial_stop, update_stop
from _helpers import utc

X = ExecutionParams()
COSTS = CostModel.from_params(X)
NOW = utc(2025, 3, 1, 12)


def _size(**kw):
    base = dict(
        equity=10_000.0, risk_pct=0.5, entry_price=100.0, stop_price=96.0, costs=COSTS, liquidity_usd=5e6,
        cash_available=10_000.0, token_room_quote=2_000.0, total_room_quote=5_000.0, max_impact_bps=100.0, min_order_quote=10.0,
    )
    base.update(kw)
    return size_position(**base)


def test_sizing_loss_at_stop_matches_planned_risk():
    s = _size(token_room_quote=1e9, total_room_quote=1e9)
    assert s.approved
    exit_price = COSTS.fill_price("SELL", 96.0, s.notional, 5e6)
    entry_fill = COSTS.fill_price("BUY", 100.0, s.notional, 5e6)
    loss = s.qty * (entry_fill - exit_price) + COSTS.fee_quote(s.notional) * 2
    assert abs(loss - 50.0) / 50.0 < 0.02, loss  # 0,5% de 10.000 = 50


def test_sizing_caps_and_rejections():
    capped = _size()  # exposição por token 2.000 limita
    assert capped.notional <= 2_000.0 + 1e-6
    assert not _size(stop_price=101.0).approved
    assert not _size(liquidity_usd=None).approved
    assert not _size(cash_available=5.0).approved
    thin = _size(liquidity_usd=20_000.0, token_room_quote=1e9, total_room_quote=1e9)
    assert thin.notional <= 100 / 1e4 * 20_000.0 + 1e-6  # impacto máx. 1% de uma pool de 20k
    assert not _size(equity=float("nan")).approved


def test_risk_manager_limits():
    rm = RiskManager(RiskParams(max_open_positions=1), X)
    rm.observe_equity(NOW, 10_000.0)
    ok = rm.check_entry(now=NOW, equity=10_000.0, cash=10_000.0, mint="M1", entry_price=100.0, stop_price=96.0, liquidity_usd=5e6, positions=[])
    assert ok.approved
    from app.risk.stops import initial_stop as ist
    pos = Position("p1", "M1", "T", 10.0, 100.0, NOW.isoformat(), 1.0, ist(100.0, 2.0, RiskParams()))
    again = rm.check_entry(now=NOW, equity=10_000.0, cash=9_000.0, mint="M2", entry_price=100.0, stop_price=96.0, liquidity_usd=5e6, positions=[pos])
    assert not again.approved and any("simultâneas" in r for r in again.reasons)
    dup = RiskManager(RiskParams(), X)
    dup.observe_equity(NOW, 10_000.0)
    d = dup.check_entry(now=NOW, equity=10_000.0, cash=9_000.0, mint="M1", entry_price=100.0, stop_price=96.0, liquidity_usd=5e6, positions=[pos])
    assert not d.approved and any("já existe" in r for r in d.reasons)
    noreserve = rm.check_entry(now=NOW, equity=10_000.0, cash=10_000.0, mint="M3", entry_price=100.0, stop_price=96.0, liquidity_usd=5e6, positions=[], fee_reserve_ok=False)
    assert not noreserve.approved


def test_daily_loss_limit_resets_next_utc_day():
    rm = RiskManager(RiskParams(daily_loss_limit_pct=2.0), X)
    rm.observe_equity(NOW, 10_000.0)
    assert rm.entry_block_reasons(NOW, 9_850.0) == []
    assert any("perda diária" in r for r in rm.entry_block_reasons(NOW, 9_790.0))
    nxt = NOW + timedelta(days=1)
    rm.observe_equity(nxt, 9_790.0)
    assert rm.entry_block_reasons(nxt, 9_790.0) == []


def test_consecutive_losses_cooldown_and_trades_per_day():
    rm = RiskManager(RiskParams(max_consecutive_losses=2, cooldown_minutes=60, max_trades_per_day=2), X)
    rm.observe_equity(NOW, 10_000.0)
    rm.record_trade_closed(NOW, -10)
    assert rm.entry_block_reasons(NOW, 10_000.0) == []
    rm.record_trade_closed(NOW, -10)
    assert any("espera" in r for r in rm.entry_block_reasons(NOW, 10_000.0))
    assert rm.entry_block_reasons(NOW + timedelta(minutes=61), 10_000.0) == []
    rm.record_trade_closed(NOW, +5)
    assert rm.state.consecutive_losses == 0
    rm.record_trade_opened(); rm.record_trade_opened()
    assert any("operações por dia" in r for r in rm.entry_block_reasons(NOW + timedelta(minutes=61), 10_000.0))


def test_cumulative_loss_trips_kill_request_and_state_roundtrip():
    rm = RiskManager(RiskParams(max_cumulative_loss_pct=10.0), X)
    rm.observe_equity(NOW, 10_000.0)
    d = rm.check_entry(now=NOW, equity=8_900.0, cash=8_900.0, mint="M", entry_price=100.0, stop_price=96.0, liquidity_usd=5e6, positions=[])
    assert not d.approved and d.trip_kill_switch
    from app.risk.risk_manager import RiskState
    assert RiskState.from_dict(rm.state.to_dict()) == rm.state


def test_stops_atr_tp_breakeven_trailing_never_decrease():
    p = RiskParams()
    st = initial_stop(100.0, 2.0, p)
    assert st.stop == 96.0 and st.take_profit == 108.0
    update_stop(st, 104.5, 2.0, p, roundtrip_cost_per_unit=0.3)  # +1.125R => break-even
    assert st.breakeven_done and st.stop >= 100.3
    before = st.stop
    update_stop(st, 106.0, 2.0, p, 0.3)  # +1.5R => trailing: 106 - 2.5*2 = 101
    assert st.trailing_active and st.stop >= 101.0 > before
    held = st.stop
    update_stop(st, 100.0, 5.0, p, 0.3)  # ATR maior e máxima menor não podem baixar o stop
    assert st.stop == held


def test_exit_precedence_and_gap():
    st = initial_stop(100.0, 2.0, RiskParams())
    assert check_exit(st, low=95.0, high=109.0, open_=100.0) == ("stop", 96.0)  # ambos no candle => stop
    assert check_exit(st, 99.0, 109.0, 100.0) == ("take_profit", 108.0)
    assert check_exit(st, 99.0, 101.0, 100.0) is None
    assert check_exit(st, 90.0, 92.0, 92.0) == ("stop", 92.0)  # gap: executa na abertura


def test_kill_switch_persists_and_requires_explicit_resume():
    store = {"v": (False, "")}
    alerts = []
    ks = KillSwitch(lambda: store["v"], lambda a, r, s: store.update(v=(a, r)), alerts.append)
    assert not ks.active
    ks.trip("teste")
    ks.trip("segunda vez")  # idempotente
    assert ks.active and ks.reason == "teste" and len(alerts) == 1
    ks2 = KillSwitch(lambda: store["v"], lambda a, r, s: store.update(v=(a, r)))
    assert ks2.active  # sobrevive à reinicialização
    assert not ks2.resume("sim", "ok") and not ks2.resume(RESUME_PHRASE, "  ")
    assert ks2.resume(RESUME_PHRASE, "causa corrigida") and not ks2.active
