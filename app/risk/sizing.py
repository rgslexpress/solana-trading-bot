"""Dimensionamento de posição baseado em risco (não no saldo total)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.execution.costs import CostModel


@dataclass
class SizingResult:
    qty: float
    notional: float
    risk_amount: float
    approved: bool
    reasons: list[str] = field(default_factory=list)


def size_position(
    *,
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    costs: CostModel,
    liquidity_usd: float | None,
    cash_available: float,
    token_room_quote: float,
    total_room_quote: float,
    max_impact_bps: float,
    min_order_quote: float,
    max_order_quote: float | None = None,
) -> SizingResult:
    """Calcula a quantidade tal que, se o stop for atingido, a perda planejada
    (incluindo taxas e slippage de ida e volta) seja ≈ risk_pct% do patrimônio.

    Rejeita (qty=0) se qualquer dado for inválido, o stop for incoerente ou a liquidez
    não comportar a posição dentro do impacto máximo."""
    bad = [v for v in (equity, entry_price, stop_price) if not math.isfinite(v) or v <= 0]
    if bad or stop_price >= entry_price:
        return SizingResult(0.0, 0.0, 0.0, False, ["entrada/stop inválidos para dimensionamento"])
    if not liquidity_usd or liquidity_usd <= 0:
        return SizingResult(0.0, 0.0, 0.0, False, ["liquidez desconhecida"])
    risk_amount = equity * risk_pct / 100.0
    stop_dist = entry_price - stop_price

    # teto de notional por restrições estáticas
    caps = {
        "caixa disponível": cash_available,
        "exposição por token": token_room_quote,
        "exposição total": total_room_quote,
        "impacto máximo": max_impact_bps / 10_000.0 * liquidity_usd / max(costs.impact_k, 1e-9),
    }
    if max_order_quote is not None:
        caps["teto por ordem"] = max_order_quote
    binding = min(caps, key=lambda k: caps[k])
    cap = max(caps[binding], 0.0)

    # ponto fixo: slippage depende do notional, que depende da quantidade
    notional = min(cap, risk_amount / (stop_dist / entry_price + 2 * costs.fee_bps / 1e4 + 2 * costs.base_slippage_bps / 1e4))
    for _ in range(25):
        slip = costs.slippage_bps(notional, liquidity_usd) / 1e4
        rt_var = 2 * (costs.fee_bps / 1e4 + slip)  # custo proporcional de ida e volta
        denom = stop_dist / entry_price + rt_var
        target = (risk_amount - 2 * costs.network_fee_quote) / denom
        new = min(cap, max(target, 0.0))
        if abs(new - notional) < 1e-9 * max(1.0, notional):
            notional = new
            break
        notional = new
    if notional <= 0:
        return SizingResult(0.0, 0.0, risk_amount, False, ["risco permitido não cobre os custos fixos"])
    reasons = []
    if notional < risk_amount / (stop_dist / entry_price) - 1e-9 and binding:
        reasons.append(f"posição limitada por: {binding}" if notional >= cap - 1e-9 else "posição reduzida por custos")
    if notional < min_order_quote:
        return SizingResult(0.0, 0.0, risk_amount, False, reasons + [f"posição abaixo do mínimo ({notional:.2f} < {min_order_quote:.2f})"])
    return SizingResult(notional / entry_price, notional, risk_amount, True, reasons)
