"""Motor de backtest bar-a-bar, multi-token, com capital compartilhado e o MESMO risco/custos do paper.

Modelo de execução (sem look-ahead):
  * O sinal é gerado no FECHAMENTO do candle t (apenas dados fechados, inclusive HTF).
  * A ordem é executada na ABERTURA do candle t+1, com slippage e taxas.
  * Stop/TP são avaliados dentro do candle com high/low; se ambos forem tocados no mesmo candle,
    assume-se o stop (conservador). Gaps executam na abertura.
  * O stop é atualizado (break-even/trailing) no fim do candle e só vale a partir do próximo.
  * Liquidez histórica de pools não está disponível: `liquidity_usd` é uma PREMISSA explícita.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from app.backtest.metrics import TRADE_COLUMNS, compute_metrics
from app.config import TIMEFRAME_SECONDS, Settings
from app.domain import Position, SignalType, TokenInfo
from app.execution.costs import CostModel
from app.risk.risk_manager import RiskManager
from app.risk.stops import check_exit, initial_stop, update_stop
from app.strategy.signal_engine import ExecContext, compute_features, decide


@dataclass
class TokenData:
    token: TokenInfo
    entry: pd.DataFrame
    htf: pd.DataFrame | None = None
    ref: pd.DataFrame | None = None


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict[str, Any]
    signal_counts: dict[str, int] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    kill_events: list[str] = field(default_factory=list)
    intel_events: list[dict[str, Any]] = field(default_factory=list)  # decisões da camada de notícias (bloqueios/cautelas) no backtest


def _condition(row: dict[str, Any]) -> str:
    up = row.get("htf_up")
    trend = "htf_alta" if up is True else ("htf_baixa" if up is False else "htf_indef")
    atr_pct = row["atr"] / row["close"] * 100 if row.get("close") else 0
    return f"{trend}|{'vol_alta' if atr_pct > 0.6 else 'vol_baixa'}"


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        data: dict[str, TokenData],
        liquidity_usd: float,
        initial_capital: float | None = None,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        intel_gate: Callable[[str, datetime], Any] | None = None,
    ):
        """`intel_gate(símbolo, instante_de_fechamento)` -> IntelAssessment usando SOMENTE informação disponível naquele instante."""
        self.intel_gate = intel_gate
        self.s = settings
        self.data = data
        self.liq = liquidity_usd
        self.initial = initial_capital if initial_capital is not None else settings.backtest.initial_capital
        self.start, self.end = start, end
        self.costs = CostModel.from_params(settings.execution)

    # ------------------------------------------------------------------
    def run(self) -> BacktestResult:
        sp, rp, xp = self.s.strategy, self.s.risk, self.s.execution
        feats: dict[str, list[dict[str, Any]]] = {}
        index: dict[str, pd.DatetimeIndex] = {}
        for sym, d in self.data.items():
            f = compute_features(d.entry, d.htf, sp, d.ref)
            index[sym] = f.index
            feats[sym] = f.to_dict("records")
        pos_of: dict[str, dict[pd.Timestamp, int]] = {s: {t: i for i, t in enumerate(ix)} for s, ix in index.items()}
        timeline = sorted(set().union(*[set(ix) for ix in index.values()]))
        if self.start is not None:
            timeline = [t for t in timeline if t >= self.start]
        if self.end is not None:
            timeline = [t for t in timeline if t < self.end]

        rm = RiskManager(rp, xp)
        cash = self.initial
        positions: dict[str, Position] = {}
        meta: dict[str, dict[str, Any]] = {}  # dados auxiliares por posição aberta
        pending: dict[str, dict[str, Any]] = {}
        last_close: dict[str, float] = {}
        trades: list[dict[str, Any]] = []
        eq_rows: list[tuple[pd.Timestamp, float, float]] = []
        sigs, rejs = Counter(), Counter()
        halted = False
        rng = random.Random(self.s.backtest.failure_seed)
        fail_rate = self.s.backtest.execution_failure_rate
        kill_events: list[str] = []
        intel_events: list[dict[str, Any]] = []
        tf_s = pd.Timedelta(seconds=TIMEFRAME_SECONDS[sp.entry_timeframe])

        def marked_equity() -> float:
            return cash + sum(p.qty * last_close.get(s, p.entry_price) for s, p in positions.items())

        def close_position(sym: str, ts: pd.Timestamp, ref_price: float, reason: str, bars: int) -> None:
            nonlocal cash
            p = positions.pop(sym)
            m = meta.pop(sym)
            notional = p.qty * ref_price
            fill = self.costs.fill_price("SELL", ref_price, notional, self.liq)
            fee = self.costs.fee_quote(p.qty * fill)
            cash += p.qty * fill - fee
            gross = p.qty * (fill - p.entry_price)
            fees = m["entry_fee"] + fee
            slip = p.qty * (m["entry_fill"] - m["entry_ref"]) + p.qty * (ref_price - fill)
            net = gross - fees
            trades.append(dict(
                symbol=sym, timeframe=sp.entry_timeframe, entry_ts=p.entry_ts, exit_ts=ts.isoformat(), qty=p.qty,
                entry_ref=m["entry_ref"], entry_fill=m["entry_fill"], exit_ref=ref_price, exit_fill=fill,
                gross_pnl=gross, fees=fees, slippage_cost=slip, net_pnl=net, ret_pct=net / (p.qty * m["entry_fill"]) * 100,
                bars_held=bars - m["entry_bar"], exit_reason=reason, condition=m["condition"],
            ))
            rm.record_trade_closed(ts.to_pydatetime(), net)

        bar_no = {s: 0 for s in self.data}
        for ts in timeline:
            now_dt: datetime = (ts + tf_s).to_pydatetime()  # instante de fechamento do candle
            for sym, d in self.data.items():
                i = pos_of[sym].get(ts)
                if i is None:
                    continue
                row = feats[sym][i]
                o, h, l, c = row["open"], row["high"], row["low"], row["close"]
                bar_no[sym] += 1
                pend = pending.pop(sym, None)
                fail_now = bool(pend) and fail_rate > 0 and rng.random() < fail_rate  # transação perdida/expirada (simulado)
                # 1) ordens decididas no fechamento anterior, executadas na abertura
                if pend and fail_now:
                    rejs["falha de execução simulada (" + pend["kind"] + ")"] += 1
                    if pend["kind"] == "exit":
                        pending[sym] = pend  # saída é reenviada no candle seguinte (com novo preço)
                elif pend and pend["kind"] == "exit" and sym in positions:
                    close_position(sym, ts, o, pend["reason"], bar_no[sym])
                elif pend and pend["kind"] == "entry" and sym not in positions and not halted:
                    last_close.setdefault(sym, o)
                    eq_now = marked_equity()
                    st = initial_stop(o, pend["atr"], rp)
                    dec = rm.check_entry(
                        now=ts.to_pydatetime(), equity=eq_now, cash=max(cash * 0.995 - xp.network_fee_quote, 0.0), mint=d.token.mint,
                        entry_price=o, stop_price=st.stop, liquidity_usd=self.liq, positions=list(positions.values()),
                        risk_scale=pend.get("risk_scale", 1.0),
                    )
                    if dec.trip_kill_switch:
                        halted = True
                        kill_events.append(f"{ts.isoformat()}: {dec.trip_kill_switch}")
                    if dec.approved and dec.sizing:
                        qty = dec.sizing.qty
                        fill = self.costs.fill_price("BUY", o, qty * o, self.liq)
                        fee = self.costs.fee_quote(qty * fill)
                        cash -= qty * fill + fee
                        positions[sym] = Position(
                            id=f"bt-{sym}-{ts.value}", mint=d.token.mint, symbol=sym, qty=qty, entry_price=fill,
                            entry_ts=ts.isoformat(), entry_cost_quote=fee, stop=st,
                        )
                        meta[sym] = dict(entry_fee=fee, entry_fill=fill, entry_ref=o, entry_bar=bar_no[sym], condition=pend["condition"])
                        rm.record_trade_opened()
                    else:
                        rejs["risco: " + "; ".join(dec.reasons)[:80]] += 1
                # 2) stop / take-profit dentro do candle
                p = positions.get(sym)
                if p:
                    ex = check_exit(p.stop, l, h, o)
                    if ex and fail_rate > 0 and rng.random() < fail_rate:
                        rejs["falha de execução simulada (" + ex[0] + ")"] += 1
                        pending[sym] = dict(kind="exit", reason=ex[0] + " (reenvio)")  # sai na abertura seguinte, possivelmente pior
                    elif ex:
                        is_gap = ex[1] < p.stop.stop - 1e-12  # gap: já executa na abertura (pior que o stop), sem penalidade extra
                        px = ex[1] * (1 - self.s.backtest.stop_latency_bps / 1e4) if ex[0] == "stop" and not is_gap else ex[1]
                        close_position(sym, ts, px, ex[0], bar_no[sym])
                    else:
                        rt_cost = p.entry_price * 2 * (xp.fee_bps + self.costs.slippage_bps(p.qty * p.entry_price, self.liq)) / 1e4 + 2 * xp.network_fee_quote / p.qty
                        if row["atr"] == row["atr"]:
                            update_stop(p.stop, h, row["atr"], rp, rt_cost)
                last_close[sym] = c
            # 3) patrimônio ao fechamento do candle e decisões do fechamento
            eq = marked_equity()
            rm.observe_equity(now_dt, eq)
            expo = sum(p.qty * last_close[s] for s, p in positions.items())
            eq_rows.append((ts, eq, expo))
            kr = rm.kill_reason(eq)
            if kr and not halted:
                halted = True
                kill_events.append(f"{ts.isoformat()}: {kr}")
            for sym, d in self.data.items():
                i = pos_of[sym].get(ts)
                if i is None:
                    continue
                row = feats[sym][i]
                cost_bps = self.costs.one_way_cost_bps(max(eq * rp.risk_per_trade_pct / 100 * 10, rp.min_order_quote), self.liq)
                ctx = ExecContext(self.liq, xp.min_liquidity_usd, cost_bps)
                dec = decide(row, sp, ctx, sym in positions)
                sigs[dec.type.value] += 1
                if dec.type == SignalType.NO_TRADE:
                    for r in dec.rejections[:1]:
                        rejs[r.split("(")[0].strip()] += 1
                if dec.type == SignalType.SELL and sym in positions:
                    pending[sym] = dict(kind="exit", reason="sinal: " + dec.reasons[0])
                elif dec.type == SignalType.BUY and sym not in positions and not halted and not rm.entry_block_reasons(now_dt, eq):
                    scale = 1.0
                    if self.intel_gate is not None:
                        a = self.intel_gate(sym, now_dt)
                        if a.block or a.risk_scale < 1.0:
                            intel_events.append({"ts": now_dt.isoformat(), "simbolo": sym, "bloqueou": bool(a.block), "escala": a.risk_scale,
                                                 "motivos": list(a.block_reasons or a.caution_reasons)})
                        if a.block:
                            rejs["notícias: bloqueio de entrada"] += 1
                            continue
                        scale = min(1.0, a.risk_scale)
                    pending[sym] = dict(kind="entry", atr=row["atr"], condition=_condition(row), risk_scale=scale)

        # 4) encerra posições remanescentes ao último fechamento (marcação conservadora, com custos)
        if timeline:
            for sym in list(positions):
                close_position(sym, timeline[-1], last_close[sym], "fim_do_teste", bar_no[sym])
            eq_rows[-1] = (eq_rows[-1][0], cash, 0.0)
        tdf = pd.DataFrame(trades, columns=TRADE_COLUMNS)
        edf = pd.DataFrame(eq_rows, columns=["ts", "equity", "exposure"]).set_index("ts")
        bench = None
        ref_sym = next(iter(self.data))
        e0 = self.data[ref_sym].entry
        if timeline and len(e0):
            seg = e0[(e0.index >= timeline[0]) & (e0.index <= timeline[-1])]
            if len(seg) > 1:
                bench = float(seg["close"].iloc[-1] / seg["open"].iloc[0] - 1) * 100
        metrics = compute_metrics(tdf, edf, self.initial, sp.entry_timeframe, bench)
        metrics["benchmark_symbol"] = ref_sym
        return BacktestResult(tdf, edf, metrics, dict(sigs), dict(rejs), kill_events, intel_events)
