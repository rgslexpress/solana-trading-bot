"""Gestor de risco: NENHUMA entrada passa sem aprovação aqui.

Definições (documentadas no README):
* Patrimônio = caixa + posições marcadas a mercado (moeda de referência).
* Perda diária = (patrimônio de referência do dia UTC − patrimônio atual) / referência. A referência
  é fixada na primeira observação de cada dia UTC e reinicia à 00:00 UTC.
* Perda acumulada = (patrimônio inicial − atual) / inicial; ao atingir o limite o kill switch é acionado.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import ExecutionParams, RiskParams
from app.domain import Position
from app.execution.costs import CostModel
from app.risk.sizing import SizingResult, size_position


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    initial_equity: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    cooldown_until: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RiskState:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class EntryDecision:
    approved: bool
    sizing: SizingResult | None = None
    reasons: list[str] = field(default_factory=list)
    trip_kill_switch: str | None = None


class RiskManager:
    def __init__(self, risk: RiskParams, execution: ExecutionParams, state: RiskState | None = None):
        self.p, self.x = risk, execution
        self.costs = CostModel.from_params(execution)
        self.state = state or RiskState()

    # ------------------------------------------------------------------ estado
    def observe_equity(self, now: datetime, equity: float) -> None:
        """Atualiza a referência diária (reinicia à 00:00 UTC) e o patrimônio inicial."""
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if self.state.initial_equity <= 0:
            self.state.initial_equity = equity
        if self.state.day != day or self.state.day_start_equity <= 0:
            self.state.day, self.state.day_start_equity, self.state.trades_today = day, equity, 0

    def daily_loss_pct(self, equity: float) -> float:
        ref = self.state.day_start_equity
        return max(0.0, (ref - equity) / ref * 100.0) if ref > 0 else 0.0

    def cumulative_loss_pct(self, equity: float) -> float:
        ref = self.state.initial_equity
        return max(0.0, (ref - equity) / ref * 100.0) if ref > 0 else 0.0

    def record_trade_opened(self) -> None:
        self.state.trades_today += 1

    def record_trade_closed(self, now: datetime, pnl: float) -> None:
        if pnl < 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.p.max_consecutive_losses:
                until = now + timedelta(minutes=self.p.cooldown_minutes)
                self.state.cooldown_until = until.astimezone(timezone.utc).isoformat()
        else:
            self.state.consecutive_losses = 0

    # ------------------------------------------------------------------ bloqueios
    def entry_block_reasons(self, now: datetime, equity: float) -> list[str]:
        r: list[str] = []
        if self.daily_loss_pct(equity) >= self.p.daily_loss_limit_pct:
            r.append(f"limite de perda diária atingido ({self.daily_loss_pct(equity):.2f}%)")
        if self.state.trades_today >= self.p.max_trades_per_day:
            r.append("limite de operações por dia atingido")
        if self.state.cooldown_until and now < datetime.fromisoformat(self.state.cooldown_until):
            r.append(f"em período de espera após {self.state.consecutive_losses} perdas consecutivas")
        return r

    def kill_reason(self, equity: float) -> str | None:
        if self.cumulative_loss_pct(equity) >= self.p.max_cumulative_loss_pct:
            return f"perda acumulada {self.cumulative_loss_pct(equity):.2f}% >= limite {self.p.max_cumulative_loss_pct}%"
        return None

    # ------------------------------------------------------------------ aprovação
    def check_entry(
        self,
        *,
        now: datetime,
        equity: float,
        cash: float,
        mint: str,
        entry_price: float,
        stop_price: float,
        liquidity_usd: float | None,
        positions: list[Position],
        fee_reserve_ok: bool = True,
        max_order_quote: float | None = None,
    ) -> EntryDecision:
        reasons = self.entry_block_reasons(now, equity)
        kr = self.kill_reason(equity)
        if kr:
            return EntryDecision(False, reasons=[kr], trip_kill_switch=kr)
        if not fee_reserve_ok:
            reasons.append("saldo de SOL abaixo da reserva para taxas")
        open_pos = [p for p in positions if p.status == "OPEN"]
        if len(open_pos) >= self.p.max_open_positions:
            reasons.append("máximo de posições simultâneas atingido")
        if any(p.mint == mint for p in open_pos):
            reasons.append("já existe posição aberta neste token")
        if reasons:
            return EntryDecision(False, reasons=reasons)

        exposure = sum(p.qty * p.entry_price for p in open_pos)  # custo de entrada como proxy conservador
        token_exp = sum(p.qty * p.entry_price for p in open_pos if p.mint == mint)
        total_room = equity * self.p.max_total_exposure_pct / 100.0 - exposure
        token_room = equity * self.p.max_token_exposure_pct / 100.0 - token_exp
        s = size_position(
            equity=equity,
            risk_pct=min(self.p.risk_per_trade_pct, self.p.absolute_risk_cap_pct),
            entry_price=entry_price,
            stop_price=stop_price,
            costs=self.costs,
            liquidity_usd=liquidity_usd,
            cash_available=cash,
            token_room_quote=token_room,
            total_room_quote=total_room,
            max_impact_bps=self.x.max_price_impact_bps,
            min_order_quote=self.p.min_order_quote,
            max_order_quote=max_order_quote,
        )
        if not s.approved:
            return EntryDecision(False, s, s.reasons or ["dimensionamento rejeitou a operação"])
        return EntryDecision(True, s, s.reasons)
