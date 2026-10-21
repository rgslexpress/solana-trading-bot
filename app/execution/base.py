"""Interface comum de execução. A estratégia/serviço só conhece esta interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.domain import Mode, OrderState, Side, TokenInfo

StateCallback = Callable[..., None]  # (intent_id, OrderState, **kw) -> None


@dataclass
class TradeRequest:
    """BUY: `amount` = quantidade de moeda de referência a gastar. SELL: `amount` = quantidade do token a vender."""

    intent_id: str
    side: Side
    token: TokenInfo
    quote: TokenInfo
    amount: float
    ref_price: float
    liquidity_usd: float | None
    ts: datetime


@dataclass
class ExecutionResult:
    intent_id: str
    state: OrderState
    side: Side
    base_qty: float = 0.0  # token comprado/vendido (efetivo)
    quote_amount: float = 0.0  # moeda de referência movimentada (bruto, antes de taxas explícitas)
    avg_price: float = 0.0
    fee_quote: float = 0.0  # taxas explícitas (rede + proporcionais), moeda de referência
    slippage_bps: float = 0.0
    signature: str | None = None
    error: str | None = None
    simulated: bool = False
    details: dict[str, Any] | None = None


class Executor(ABC):
    mode: Mode

    @abstractmethod
    def execute(self, req: TradeRequest) -> ExecutionResult:
        """Executa a intenção. NUNCA repete envio automaticamente; resultado incerto => UNKNOWN."""

    @abstractmethod
    def quote_balance(self) -> float:
        """Saldo disponível da moeda de referência."""

    @abstractmethod
    def fee_reserve_ok(self) -> bool:
        """True se há SOL suficiente para taxas (sempre True em simulação)."""

    def verify_token(self, token: TokenInfo) -> list[str]:
        """Problemas on-chain do token (autoridades/extensões/decimais). Vazio = aprovado ou sem verificação (simulação)."""
        return []

    def token_balance(self, token: TokenInfo) -> float | None:
        """Saldo on-chain do token (None se indisponível/simulação). Usado na checagem de divergência."""
        return None

    def reconcile(self, intent: dict[str, Any]) -> ExecutionResult | None:
        """Resolve intenções pendentes/desconhecidas. Padrão: nada a reconciliar."""
        return None
