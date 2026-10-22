"""Orquestra um ciclo de trading (paper ou live): dados -> sinal -> risco -> execução -> persistência.

O mesmo código roda em PAPER e LIVE; só o `Executor` muda. Saídas (stop/TP/SELL) são sempre permitidas;
entradas exigem: startup aprovado, kill switch inativo, saúde OK e aprovação do RiskManager."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

import json

from app.config import TIMEFRAME_SECONDS, Settings, validate_settings
from app.data.providers import CandleProvider, DataProviderError
from app.data.validation import closed_only, validate_candles
from app.domain import Mode, OrderState, Position, Side, SignalType, TokenInfo
from app.execution.base import ExecutionResult, Executor, TradeRequest
from app.execution.costs import CostModel
from app.monitoring.alerts import AlertManager
from app.monitoring.health import HealthCheck, HealthMonitor, HealthReport
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskManager, RiskState
from app.risk.stops import check_exit, initial_stop, update_stop
from app.intel.runtime import IntelRuntime
from app.storage.database import Database
from app.strategy.signal_engine import ExecContext, compute_features, decide

log = logging.getLogger("servico")


@dataclass
class StartupReport:
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))


@dataclass
class CycleReport:
    ts: str
    actions: list[str] = field(default_factory=list)
    entries_blocked_reason: str | None = None


class TradingService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        provider: CandleProvider,
        executor: Executor,
        kill_switch: KillSwitch,
        alerts: AlertManager,
        tokens: dict[str, TokenInfo],
        quote: TokenInfo,
        reference: TokenInfo | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        intel: IntelRuntime | None = None,
    ):
        self.s, self.db, self.provider, self.executor, self.ks, self.alerts = settings, db, provider, executor, kill_switch, alerts
        self.tokens, self.quote, self.reference, self.clock = tokens, quote, reference, clock
        self.costs = CostModel.from_params(settings.execution)
        self.intel = intel  # News & Market Intelligence (opcional; só restringe entradas, nunca opera)
        self.risk = RiskManager(settings.risk, settings.execution, RiskState.from_dict(db.get_meta("risk_state", {})))
        self.positions: dict[str, Position] = {}
        self.ready = False
        self.health = HealthMonitor()
        self._data_ok: dict[str, bool] = {}
        self.entries_paused = bool(db.get_meta("entries_paused", False))  # pausa de NOVAS entradas pelo operador (painel)
        self.data_status: dict[str, dict] = {}  # por símbolo: ok, problemas, último candle fechado, preço, verificado em
        self._failed_cycles = 0
        self._startup_failure = ""
        self._last_prices: dict[str, float] = db.get_meta("last_prices", {})
        self._register_health()

    # ------------------------------------------------------------------ util
    @property
    def is_live(self) -> bool:
        return self.executor.mode == Mode.LIVE

    def _tf(self, name: str) -> int:
        return TIMEFRAME_SECONDS[name]

    def cash(self) -> float:
        return self.executor.quote_balance()

    def equity(self) -> float:
        total = self.cash()
        for p in self.positions.values():
            total += p.qty * self._last_prices.get(p.mint, p.entry_price)
        return total

    def exposure(self) -> float:
        return sum(p.qty * self._last_prices.get(p.mint, p.entry_price) for p in self.positions.values())

    def _persist_state(self) -> None:
        self.db.set_meta("risk_state", self.risk.state.to_dict())
        self.db.set_meta("last_prices", self._last_prices)
        self.db.set_meta("data_status", self.data_status)
        if hasattr(self.executor, "cash") and not self.is_live:
            self.db.set_meta("paper_cash", self.executor.cash)  # type: ignore[attr-defined]

    def _on_state(self, intent_id: str, state: OrderState, **kw: object) -> None:
        self.db.set_intent_state(intent_id, state, **kw)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ saúde
    def _register_health(self) -> None:
        def db_integrity() -> HealthCheck:
            return HealthCheck("banco_de_dados", self.db.integrity_ok())

        def kill_switch() -> HealthCheck:
            return HealthCheck("kill_switch", not self.ks.active, True, self.ks.reason)

        def data_fresh() -> HealthCheck:
            bad = [k for k, v in self._data_ok.items() if not v]
            return HealthCheck("dados_de_mercado", not bad, True, f"problemas: {bad}" if bad else "")

        def pending_unknown() -> HealthCheck:
            n = len(self.db.intents_in_states((OrderState.UNKNOWN, OrderState.SUBMITTED)))
            return HealthCheck("transacoes_pendentes", n == 0, True, f"{n} pendente(s)" if n else "")

        for fn in (db_integrity, kill_switch, data_fresh, pending_unknown):
            self.health.register(fn)

    # ------------------------------------------------------------------ inicialização segura
    def _check_balance_divergence(self) -> None:
        """Compara posições locais com o saldo on-chain (quando o executor o fornece). Divergência => kill switch."""
        tol = self.s.execution.balance_divergence_pct / 100.0
        for p in self.positions.values():
            tok = next((t for t in self.tokens.values() if t.mint == p.mint), None)
            bal = self.executor.token_balance(tok) if tok else None
            if bal is not None and p.qty > 0 and abs(bal - p.qty) / p.qty > tol:
                self.ks.trip(f"divergência de saldo em {p.symbol}: local {p.qty:.6f} vs on-chain {bal:.6f}")

    def startup(self, quiet: bool = False) -> StartupReport:
        """Sequência de inicialização. Qualquer falha crítica mantém o sistema em modo seguro (sem entradas)."""
        rep = StartupReport()
        errs = validate_settings(self.s)
        rep.add("configuração válida", not errs, "; ".join(errs))
        rep.add("banco de dados íntegro", self.db.integrity_ok())
        rep.add("modo de execução", self.executor.mode == self.s.mode, f"config={self.s.mode.value} executor={self.executor.mode.value}")
        # restaura estado persistido
        self.positions = {p.mint: p for p in self.db.load_positions("OPEN")}
        if not self.is_live:
            cash = self.db.get_meta("paper_cash")
            if cash is not None and hasattr(self.executor, "cash"):
                self.executor.cash = float(cash)  # type: ignore[attr-defined]
        rep.add("posições restauradas", True, f"{len(self.positions)} aberta(s)")
        # intenções interrompidas antes do envio => FAILED (seguro); pós-envio => reconciliação
        stale = self.db.intents_in_states((OrderState.CREATED, OrderState.VALIDATED, OrderState.QUOTED))
        for row in stale:
            self.db.set_intent_state(row["id"], OrderState.FAILED, error="interrompida antes do envio (reinício)", note="startup")
        if stale:
            rep.add("intenções interrompidas encerradas", True, f"{len(stale)}")
        pend_ok = self.reconcile_pending()
        rep.add("reconciliação de pendentes", pend_ok, "")
        # autoridades/extensões/decimais dos mints (somente execução real; simulação não consulta a chain)
        bad = {t.symbol: self.executor.verify_token(t) for t in self.tokens.values()}
        bad = {k: v for k, v in bad.items() if v}
        rep.add("verificação on-chain dos tokens", not bad, "; ".join(f"{k}: {', '.join(v)}" for k, v in bad.items()))
        # dados
        now = self.clock()
        data_ok = True
        detail = ""
        for tok in self.tokens.values():
            feats, why = self._features(tok, now)
            if feats is None:
                data_ok, detail = False, f"{tok.symbol}: {why}"
        rep.add("qualidade dos dados", data_ok, detail)
        # patrimônio / risco
        self.risk.observe_equity(now, self.equity())
        rep.add("kill switch inativo", not self.ks.active, self.ks.reason)
        rep.add("reserva de taxas", self.executor.fee_reserve_ok())
        health = self.health.run()
        rep.add("saúde dos componentes", health.ok, health.summary() if not health.ok else "")
        self._persist_state()
        self.ready = rep.ok
        failure = "; ".join(f"{n}: {d}" for n, ok, d in rep.steps if not ok)
        self._startup_failure = failure
        if not self.ready and not (quiet and failure == getattr(self, "_last_alerted_failure", None)):
            self.alerts.critical("Startup reprovado — modo seguro (sem novas entradas): " + failure)
            self._last_alerted_failure = failure
        log.info("startup concluído", extra={"data": {"ok": rep.ok, "passos": rep.steps}})
        return rep

    # ------------------------------------------------------------------ dados / features
    def _features(self, tok: TokenInfo, now: datetime) -> tuple[pd.DataFrame | None, str]:
        sp, dp = self.s.strategy, self.s.data
        try:
            entry = self.provider.get_candles(tok, sp.entry_timeframe, dp.history_candles, now)
            htf = self.provider.get_candles(tok, sp.htf_timeframe, dp.history_candles, now)
            ref = None
            if sp.use_ref_filter and self.reference and self.reference.mint != tok.mint:
                ref = closed_only(self.provider.get_candles(self.reference, sp.entry_timeframe, dp.history_candles, now), self._tf(sp.entry_timeframe), now, dp.close_grace_s)
        except DataProviderError as exc:
            self._mark_data(tok, now, False, [f"provedor: {exc}"])
            return None, f"provedor: {exc}"
        except Exception as exc:  # noqa: BLE001
            self._mark_data(tok, now, False, [f"erro inesperado de dados: {type(exc).__name__}"])
            return None, f"erro inesperado de dados: {type(exc).__name__}"
        entry = closed_only(entry, self._tf(sp.entry_timeframe), now, dp.close_grace_s)  # nunca usa candle em formação
        htf = closed_only(htf, self._tf(sp.htf_timeframe), now, dp.close_grace_s)
        v = validate_candles(entry, self._tf(sp.entry_timeframe), now, dp.max_data_staleness_candles, sp.min_candles, dp.max_gap_candles, sp.min_candles)
        vh = validate_candles(htf, self._tf(sp.htf_timeframe), now, dp.max_data_staleness_candles, sp.ema_trend + 10, dp.max_gap_candles, sp.ema_trend + 10)
        if not v.ok or not vh.ok:
            issues = v.issues + [f"htf: {i}" for i in vh.issues]
            last = (entry.index[-1] + pd.Timedelta(seconds=self._tf(sp.entry_timeframe))).isoformat() if len(entry) else None
            self._mark_data(tok, now, False, issues, last)
            return None, "dados inválidos: " + "; ".join(issues)
        self._mark_data(tok, now, True, [], (entry.index[-1] + pd.Timedelta(seconds=self._tf(sp.entry_timeframe))).isoformat(), float(entry["close"].iloc[-1]))
        self._last_prices[tok.mint] = float(entry["close"].iloc[-1])
        return compute_features(entry, htf, sp, ref), ""

    def _mark_data(self, tok: TokenInfo, now: datetime, ok: bool, issues: list[str], last_close: str | None = None, price: float | None = None) -> None:
        self._data_ok[tok.symbol] = ok
        prev = self.data_status.get(tok.symbol, {})
        self.data_status[tok.symbol] = {
            "ok": ok, "problemas": issues, "checado_em": now.isoformat(),
            "ultimo_candle_fechado": last_close if last_close else prev.get("ultimo_candle_fechado"),
            "ultimo_preco": price if price is not None else prev.get("ultimo_preco"),
            "fonte": getattr(self.provider, "name", "?"),
        }

    def _save_last_cycle(self, rep: "CycleReport") -> None:
        self.db.set_meta("last_cycle", {"ts": rep.ts, "acoes": rep.actions[-20:], "bloqueio": rep.entries_blocked_reason, "pronto": self.ready, "falha_startup": self._startup_failure})

    # ------------------------------------------------------------------ ciclo
    def run_cycle(self) -> CycleReport:
        now = self.clock()
        rep = CycleReport(now.isoformat())
        self.ks.refresh()  # acionamentos externos (painel/CLI) valem imediatamente no próximo ciclo
        if not self.ready:
            # MODO SEGURO: tenta se recuperar (reconcilia e revalida tudo) e, enquanto não conseguir,
            # apenas monitora posições abertas (saídas são sempre permitidas). Nenhuma entrada.
            self.startup(quiet=True)
            if not self.ready:
                for p in list(self.positions.values()):
                    tok = next((t for t in self.tokens.values() if t.mint == p.mint), None)
                    if tok:
                        self._monitor_position(p, tok, now, rep)
                self._persist_state()
                rep.entries_blocked_reason = "startup não aprovado (modo seguro): " + self._startup_failure
                self._save_last_cycle(rep)
                return rep
        self.reconcile_pending()
        self._check_balance_divergence()
        any_data = False
        # 1) stops/take-profit das posições abertas (saídas sempre permitidas)
        for p in list(self.positions.values()):
            tok = next((t for t in self.tokens.values() if t.mint == p.mint), None)
            if tok:
                self._monitor_position(p, tok, now, rep)
        # 1b) notícias externas (falha isolada; nunca afeta stops nem o restante do ciclo)
        if self.intel is not None:
            stats = self.intel.refresh(now)
            if stats:
                rep.actions.append(f"notícias: {stats['novos']} novas, {stats['fontes_ok']} fonte(s) ok, {stats['fontes_falha']} com falha")
        # 2) sinais por token
        health = self.health.run()
        for tok in self.tokens.values():
            feats, why = self._features(tok, now)
            if feats is None:
                rep.actions.append(f"{tok.symbol}: sem decisão ({why})")
                continue
            any_data = True
            last = feats.iloc[-1]
            candle_ts = feats.index[-1].isoformat()
            if self.db.last_signal_ts(tok.mint) == candle_ts:
                continue  # candle já avaliado (idempotência após reinício)
            self._evaluate(tok, last.to_dict(), candle_ts, now, health, rep)
        # 3) governança
        self._failed_cycles = 0 if any_data else self._failed_cycles + 1
        if self._failed_cycles >= self.s.data.max_failed_cycles_before_kill:
            self.ks.trip(f"dados de mercado indisponíveis por {self._failed_cycles} ciclos")
        eq = self.equity()
        self.risk.observe_equity(now, eq)
        kr = self.risk.kill_reason(eq)
        if kr:
            self.ks.trip(kr)
        self.db.save_equity(eq, self.cash(), self.exposure())
        self._persist_state()
        if self.ks.active:
            rep.entries_blocked_reason = f"kill switch: {self.ks.reason}"
        elif self.entries_paused:
            rep.entries_blocked_reason = "novas entradas pausadas pelo operador"
        self._save_last_cycle(rep)
        return rep

    # ------------------------------------------------------------------ avaliação de sinal
    def _evaluate(self, tok: TokenInfo, row: dict, candle_ts: str, now: datetime, health: HealthReport, rep: CycleReport) -> None:
        xp = self.s.execution
        has_pos = tok.mint in self.positions
        liq = self.provider.liquidity_usd(tok, now)
        eq = self.equity()
        cost_bps = self.costs.one_way_cost_bps(max(eq * self.s.risk.risk_per_trade_pct / 100 * 10, self.s.risk.min_order_quote), liq)
        dec = decide(row, self.s.strategy, ExecContext(liq, max(xp.min_liquidity_usd, tok.min_liquidity_usd or 0.0), cost_bps), has_pos)
        self.db.save_signal(tok.mint, tok.symbol, candle_ts, dec.type.value, dec.score, dec.reasons, dec.rejections)
        if dec.type == SignalType.SELL and has_pos:
            self._exit(self.positions[tok.mint], tok, float(row["close"]), liq, now, "sinal: " + dec.reasons[0], rep)
        elif dec.type == SignalType.BUY and not has_pos:
            block = self._entry_block(health)
            if block:
                self.db.save_signal(tok.mint, tok.symbol, candle_ts + "#bloqueio", "NO_TRADE", dec.score, [], [block])
                rep.actions.append(f"{tok.symbol}: BUY bloqueado ({block})")
                return
            risk_scale, decision_id = 1.0, None
            if self.intel is not None and self.intel.active:
                assessment = self.intel.assess(tok.symbol, now, "BUY")
                enforce = self.intel.params.mode == "enforce"
                applied = enforce and assessment.would_change
                action = ("bloqueou a entrada" if enforce and assessment.block else "reduziu o risco" if enforce and assessment.risk_scale < 1.0
                          else "SHADOW: bloquearia" if assessment.block else "SHADOW: reduziria o risco" if assessment.risk_scale < 1.0 else "sem efeito")
                # AUDITORIA É SÓ OBSERVAÇÃO: se gravar falhar (disco cheio, permissão, banco travado), a decisão NÃO muda
                # (nem em shadow nem em enforce). A falha é contada, registrada no log e mostrada no painel.
                decision_id = self.intel.record_decision(lambda: {
                    "ts": now.isoformat(), "symbol": tok.symbol, "mint": tok.mint, "candle_ts": candle_ts, "technical": dec.type.value, "mode": assessment.mode,
                    "data_state": assessment.data_state, "action": action, "would_change": assessment.would_change, "applied": applied,
                    "risk_scale": assessment.risk_scale, "divergence": assessment.divergence, "alerts": [a.to_public() for a in assessment.alerts],
                    "rationale": assessment.rationale})
                if decision_id is None and self.intel.audit_failures:
                    rep.actions.append(f"{tok.symbol}: AUDITORIA DE NOTÍCIAS INDISPONÍVEL ({self.intel.audit_failures} falha(s)); a decisão de trading não foi afetada")
                if enforce and assessment.block:
                    why = "; ".join(assessment.block_reasons)
                    try:
                        self.db.save_signal(tok.mint, tok.symbol, candle_ts + "#noticias", "NO_TRADE", dec.score, [], ["notícias: " + why])
                    except Exception:  # noqa: BLE001 - o bloqueio (restrição) vale mesmo que o marcador não possa ser gravado
                        self.intel.note_audit_failure("marcador de sinal")
                    rep.actions.append(f"{tok.symbol}: BUY bloqueado pela camada de notícias ({why})")
                    return
                if enforce:
                    risk_scale = min(1.0, assessment.risk_scale)
                if assessment.divergence:
                    rep.actions.append(f"{tok.symbol}: {assessment.divergence}")
            before = self.positions.get(tok.mint)
            self._enter(tok, row, liq, now, rep, risk_scale=risk_scale)
            after = self.positions.get(tok.mint)
            if decision_id and after is not None and after is not before and self.intel is not None:
                self.intel.link_position(decision_id, after.id)

    def _entry_block(self, health: HealthReport) -> str | None:
        if self.entries_paused:
            return "novas entradas pausadas pelo operador"
        if self.ks.active:
            return f"kill switch ativo: {self.ks.reason}"
        if not health.ok:
            return "saúde crítica: " + "; ".join(c.name for c in health.failures if c.critical)
        return None

    # ------------------------------------------------------------------ entrada / saída
    def _enter(self, tok: TokenInfo, row: dict, liq: float | None, now: datetime, rep: CycleReport, risk_scale: float = 1.0) -> None:
        sp, rp, xp = self.s.strategy, self.s.risk, self.s.execution
        price, atr = float(row["close"]), float(row["atr"])
        st = initial_stop(price, atr, rp)
        eq, cash = self.equity(), self.cash()
        dec = self.risk.check_entry(
            now=now, equity=eq, cash=max(cash * 0.995 - xp.network_fee_quote, 0.0), mint=tok.mint, entry_price=price, stop_price=st.stop,
            liquidity_usd=liq, positions=list(self.positions.values()), fee_reserve_ok=self.executor.fee_reserve_ok(),
            max_order_quote=xp.max_order_quote if self.is_live else None, risk_scale=risk_scale,
        )
        if dec.trip_kill_switch:
            self.ks.trip(dec.trip_kill_switch)
        if not dec.approved or not dec.sizing:
            rep.actions.append(f"{tok.symbol}: entrada rejeitada pelo risco ({'; '.join(dec.reasons)})")
            return
        intent_id = f"{'live' if self.is_live else 'paper'}-buy-{tok.symbol}-{uuid.uuid4().hex[:10]}"
        req = TradeRequest(intent_id, Side.BUY, tok, self.quote, dec.sizing.notional, price, liq, now)
        meta = {"atr": atr, "stop": st.to_dict(), "notional": dec.sizing.notional, "ref_price": price, "qty_planned": dec.sizing.qty}
        if not self.db.create_intent(intent_id, "BUY", tok.mint, tok.symbol, self.executor.mode.value, meta):
            rep.actions.append(f"{tok.symbol}: intenção duplicada/ativa — compra não enviada")
            return
        res = self.executor.execute(req)
        self._after_execution(res, tok, meta, now, rep)

    def _exit(self, p: Position, tok: TokenInfo, ref_price: float, liq: float | None, now: datetime, reason: str, rep: CycleReport) -> None:
        if self.db.intents_in_states((OrderState.UNKNOWN, OrderState.SUBMITTED, OrderState.SIGNED)):
            if any(r["mint"] == p.mint for r in self.db.intents_in_states((OrderState.UNKNOWN, OrderState.SUBMITTED, OrderState.SIGNED))):
                rep.actions.append(f"{tok.symbol}: saída adiada (intenção pendente neste token)")
                return
        intent_id = f"{'live' if self.is_live else 'paper'}-sell-{tok.symbol}-{uuid.uuid4().hex[:10]}"
        req = TradeRequest(intent_id, Side.SELL, tok, self.quote, p.qty, ref_price, liq, now)
        meta = {"position_id": p.id, "reason": reason, "ref_price": ref_price}
        if not self.db.create_intent(intent_id, "SELL", tok.mint, tok.symbol, self.executor.mode.value, meta):
            rep.actions.append(f"{tok.symbol}: venda já em andamento")
            return
        res = self.executor.execute(req)
        self._after_execution(res, tok, meta, now, rep, position=p)

    def _after_execution(self, res: ExecutionResult, tok: TokenInfo, meta: dict, now: datetime, rep: CycleReport, position: Position | None = None) -> None:
        if res.state == OrderState.CONFIRMED:
            self._exec_failures(reset=True)
            if res.side == Side.BUY:
                self._open_position(res, tok, meta, now)
                rep.actions.append(f"{tok.symbol}: COMPRA {res.base_qty:.6f} @ {res.avg_price:.6f} ({'simulada' if res.simulated else 'real'})")
            elif position:
                self._close_position(position, res, now, meta.get("reason", ""))
                rep.actions.append(f"{tok.symbol}: VENDA {res.base_qty:.6f} @ {res.avg_price:.6f} ({meta.get('reason', '')})")
        elif res.state in (OrderState.UNKNOWN, OrderState.SUBMITTED):
            self.alerts.warning(f"{tok.symbol}: resultado desconhecido ({res.intent_id}); será reconciliado — novas entradas bloqueadas")
            rep.actions.append(f"{tok.symbol}: resultado DESCONHECIDO ({res.signature})")
        else:
            n = self._exec_failures()
            rep.actions.append(f"{tok.symbol}: execução {res.state.value}: {res.error}")
            if n >= self.s.execution.max_consecutive_exec_failures:
                self.ks.trip(f"{n} falhas de execução consecutivas")

    def _exec_failures(self, reset: bool = False) -> int:
        n = 0 if reset else int(self.db.get_meta("exec_failures", 0)) + 1
        self.db.set_meta("exec_failures", n)
        return n

    def _open_position(self, res: ExecutionResult, tok: TokenInfo, meta: dict, now: datetime) -> Position:
        st = initial_stop(res.avg_price, float(meta["atr"]), self.s.risk)
        p = Position(
            id=res.intent_id, mint=tok.mint, symbol=tok.symbol, qty=res.base_qty, entry_price=res.avg_price,
            entry_ts=now.isoformat(), entry_cost_quote=res.fee_quote, stop=st, cost_total_quote=res.fee_quote,
        )
        self.positions[tok.mint] = p
        self.db.save_position(p)
        self.risk.record_trade_opened()
        self._persist_state()
        return p

    def _close_position(self, p: Position, res: ExecutionResult, now: datetime, reason: str) -> None:
        proceeds = res.base_qty * res.avg_price
        cost = p.qty * p.entry_price + p.entry_cost_quote
        pnl = proceeds - res.fee_quote - cost
        p.status, p.exit_price, p.exit_ts, p.exit_reason, p.pnl_quote = "CLOSED", res.avg_price, now.isoformat(), reason, pnl
        p.cost_total_quote += res.fee_quote
        self.db.save_position(p)
        self.positions.pop(p.mint, None)
        self.risk.record_trade_closed(now, pnl)
        self._persist_state()

    def _monitor_position(self, p: Position, tok: TokenInfo, now: datetime, rep: CycleReport) -> None:
        try:
            price = self.provider.latest_price(tok, now)
        except Exception:  # noqa: BLE001
            price = None
        if not price:
            self._data_ok[tok.symbol] = False
            self.alerts.warning(f"{tok.symbol}: sem preço para monitorar stop — proteção indisponível neste ciclo")
            return
        self._last_prices[tok.mint] = price
        ex = check_exit(p.stop, low=price, high=price, open_=price)
        liq = self.provider.liquidity_usd(tok, now)
        if ex:
            self._exit(p, tok, price, liq, now, ex[0], rep)
            return
        rt = p.entry_price * 2 * (self.s.execution.fee_bps + self.costs.slippage_bps(p.qty * p.entry_price, liq)) / 1e4 + 2 * self.s.execution.network_fee_quote / p.qty
        # ATR atual ≈ distância inicial/mult (conservador: não depende de recomputar features a cada tick)
        atr = p.stop.initial_risk_per_unit / self.s.risk.atr_stop_mult
        update_stop(p.stop, price, atr, self.s.risk, rt)
        self.db.save_position(p)

    # ------------------------------------------------------------------ reconciliação
    def reconcile_pending(self) -> bool:
        """Resolve intenções SUBMITTED/UNKNOWN. Retorna False se restar algo sem solução crítica."""
        ok = True
        rows = self.db.intents_in_states((OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.SIGNED))
        for row in rows:
            res = self.executor.reconcile(dict(row))
            if res is None:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_ts"])).total_seconds()
                if age > self.s.execution.unknown_kill_after_s:
                    self.ks.trip(f"intenção {row['id']} desconhecida por {age:.0f}s")
                ok = False
                continue
            tok = next((t for t in self.tokens.values() if t.mint == row["mint"]), None)
            if res.state == OrderState.CONFIRMED and tok:
                meta = json.loads(row["request_json"])
                now = self.clock()
                if res.side == Side.BUY and tok.mint not in self.positions:
                    self._open_position(res, tok, meta, now)
                elif res.side == Side.SELL and tok.mint in self.positions:
                    self._close_position(self.positions[tok.mint], res, now, meta.get("reason", "reconciliada"))
                self.alerts.warning(f"intenção {row['id']} reconciliada como CONFIRMADA")
            elif res.state in (OrderState.FAILED, OrderState.EXPIRED):
                self.alerts.warning(f"intenção {row['id']} reconciliada como {res.state.value}")
            else:
                ok = False
        return ok
