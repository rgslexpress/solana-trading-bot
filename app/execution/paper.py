"""Executor de paper trading: simula preenchimentos com o mesmo modelo de custos do backtest.

GARANTIA DE SEGURANÇA: este módulo não importa nem referencia carteira, assinatura, Jupiter ou HTTP.
Não possui métodos de assinatura/envio. (Verificado por testes em tests/security.)"""

from __future__ import annotations

from app.config import ExecutionParams
from app.domain import Mode, OrderState, Side
from app.execution.base import ExecutionResult, Executor, StateCallback, TradeRequest
from app.execution.costs import CostModel


class PaperExecutor(Executor):
    mode = Mode.PAPER

    def __init__(self, params: ExecutionParams, initial_cash: float, on_state: StateCallback | None = None):
        self.costs = CostModel.from_params(params)
        self.max_slip, self.min_liq = params.max_slippage_bps, params.min_liquidity_usd
        self.cash = initial_cash
        self._on_state = on_state or (lambda *a, **k: None)

    def quote_balance(self) -> float:
        return self.cash

    def fee_reserve_ok(self) -> bool:
        return True

    def execute(self, req: TradeRequest) -> ExecutionResult:
        s = self._on_state
        s(req.intent_id, OrderState.VALIDATED)
        if req.ref_price <= 0 or req.amount <= 0 or not req.liquidity_usd:
            s(req.intent_id, OrderState.FAILED, error="requisição inválida (preço/quantidade/liquidez)")
            return ExecutionResult(req.intent_id, OrderState.FAILED, req.side, error="requisição inválida", simulated=True)
        notional = req.amount if req.side == Side.BUY else req.amount * req.ref_price
        slip = self.costs.slippage_bps(notional, req.liquidity_usd)
        if slip > self.max_slip or req.liquidity_usd < self.min_liq:
            s(req.intent_id, OrderState.FAILED, error=f"slippage/liquidez fora do limite ({slip:.1f} bps)")
            return ExecutionResult(req.intent_id, OrderState.FAILED, req.side, slippage_bps=slip, error="slippage acima do limite", simulated=True)
        fill = self.costs.fill_price(req.side.value, req.ref_price, notional, req.liquidity_usd)
        s(req.intent_id, OrderState.QUOTED)
        if req.side == Side.BUY:
            fee = self.costs.fee_quote(notional)
            if notional + fee > self.cash + 1e-9:
                s(req.intent_id, OrderState.FAILED, error="saldo virtual insuficiente")
                return ExecutionResult(req.intent_id, OrderState.FAILED, req.side, error="saldo insuficiente", simulated=True)
            qty = notional / fill
            self.cash -= notional + fee
            res = ExecutionResult(req.intent_id, OrderState.CONFIRMED, Side.BUY, qty, notional, fill, fee, slip, simulated=True)
        else:
            gross = req.amount * fill
            fee = self.costs.fee_quote(gross)
            self.cash += gross - fee
            res = ExecutionResult(req.intent_id, OrderState.CONFIRMED, Side.SELL, req.amount, gross, fill, fee, slip, simulated=True)
        s(req.intent_id, OrderState.CONFIRMED, result={"fill": fill, "qty": res.base_qty, "fee": fee, "slippage_bps": slip, "simulated": True})
        return res
