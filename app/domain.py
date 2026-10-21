"""Tipos de domínio compartilhados (sem dependências de I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class SignalType(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    QUOTED = "QUOTED"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"


S = OrderState
# Transições válidas da máquina de estados de uma intenção de operação.
ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.CREATED: frozenset({S.VALIDATED, S.FAILED}),
    S.VALIDATED: frozenset({S.QUOTED, S.FAILED}),
    S.QUOTED: frozenset({S.SIGNED, S.CONFIRMED, S.FAILED, S.EXPIRED}),  # CONFIRMED direto: simulação (paper)
    S.SIGNED: frozenset({S.SUBMITTED, S.FAILED, S.EXPIRED}),
    S.SUBMITTED: frozenset({S.CONFIRMED, S.FAILED, S.EXPIRED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.CONFIRMED, S.FAILED, S.EXPIRED, S.UNKNOWN}),
    S.CONFIRMED: frozenset({S.RECONCILED}),
    S.FAILED: frozenset(),
    S.EXPIRED: frozenset(),
    S.RECONCILED: frozenset(),
}
TERMINAL_STATES = frozenset({S.CONFIRMED, S.FAILED, S.EXPIRED, S.RECONCILED})


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenInfo:
    """Token identificado por endereço mint (nunca apenas por símbolo)."""

    symbol: str
    mint: str
    decimals: int
    pool: str | None = None  # pool de referência para provedores de candles
    min_liquidity_usd: float | None = None
    allow_mint_authority: bool = False  # exceção explícita (ex.: stablecoin centralizada)
    allow_freeze_authority: bool = False

    def to_raw(self, amount: float) -> int:
        """Converte quantidade humana em unidades mínimas (inteiro, arredondando para baixo)."""
        from decimal import ROUND_DOWN, Decimal

        scaled = (Decimal(str(amount)) * (Decimal(10) ** self.decimals)).to_integral_value(ROUND_DOWN)
        return int(scaled)

    def from_raw(self, raw: int) -> float:
        return raw / (10**self.decimals)


@dataclass
class SignalDecision:
    type: SignalType
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)  # por que o sinal foi emitido
    rejections: list[str] = field(default_factory=list)  # por que NÃO operou
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StopState:
    entry_price: float
    initial_stop: float
    stop: float
    take_profit: float
    initial_risk_per_unit: float
    breakeven_done: bool = False
    trailing_active: bool = False
    highest_price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StopState:
        return cls(**d)


@dataclass
class Position:
    id: str
    mint: str
    symbol: str
    qty: float
    entry_price: float
    entry_ts: str
    entry_cost_quote: float  # taxas+rede pagas na entrada
    stop: StopState
    status: str = "OPEN"  # OPEN | CLOSED
    exit_price: float | None = None
    exit_ts: str | None = None
    exit_reason: str | None = None
    pnl_quote: float | None = None  # líquido de custos
    cost_total_quote: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stop"] = self.stop.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        d = dict(d)
        d["stop"] = StopState.from_dict(d["stop"])
        return cls(**d)

    @property
    def cost_basis(self) -> float:
        return self.qty * self.entry_price
