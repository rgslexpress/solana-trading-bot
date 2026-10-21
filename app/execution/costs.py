"""Modelo de custos compartilhado por sizing, backtest e paper (mesmas premissas em todos)."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import ExecutionParams


@dataclass(frozen=True)
class CostModel:
    fee_bps: float
    network_fee_quote: float
    base_slippage_bps: float
    impact_k: float

    @classmethod
    def from_params(cls, x: ExecutionParams) -> CostModel:
        return cls(x.fee_bps, x.network_fee_quote, x.base_slippage_bps, x.impact_k)

    def impact_bps(self, notional: float, liquidity_usd: float | None) -> float:
        """Impacto aproximado: k * notional/liquidez (em bps). Liquidez desconhecida => muito alto."""
        if not liquidity_usd or liquidity_usd <= 0:
            return 10_000.0
        return self.impact_k * (notional / liquidity_usd) * 10_000.0

    def slippage_bps(self, notional: float, liquidity_usd: float | None) -> float:
        return self.base_slippage_bps + self.impact_bps(notional, liquidity_usd)

    def fill_price(self, side: str, ref_price: float, notional: float, liquidity_usd: float | None) -> float:
        s = self.slippage_bps(notional, liquidity_usd) / 10_000.0
        return ref_price * (1.0 + s) if side == "BUY" else ref_price * (1.0 - s)

    def fee_quote(self, notional: float) -> float:
        """Taxa proporcional (rota/protocolo) + taxa de rede fixa, em moeda de referência."""
        return notional * self.fee_bps / 10_000.0 + self.network_fee_quote

    def one_way_cost_bps(self, notional: float, liquidity_usd: float | None) -> float:
        """Custo percentual de ida (taxa proporcional + slippage + taxa fixa diluída)."""
        fixed = (self.network_fee_quote / notional * 10_000.0) if notional > 0 else 0.0
        return self.fee_bps + self.slippage_bps(notional, liquidity_usd) + fixed
