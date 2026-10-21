"""Stop-loss por ATR, take-profit por R:R, break-even líquido de custos e trailing stop.

ATENÇÃO: estes stops são monitorados PELO PROCESSO. Não são ordens on-chain. Se o processo ficar
offline, o stop não executa (ver docs/LIMITACOES.md)."""

from __future__ import annotations

from app.config import RiskParams
from app.domain import StopState


def initial_stop(entry_price: float, atr_value: float, p: RiskParams) -> StopState:
    stop = entry_price - p.atr_stop_mult * atr_value
    risk = entry_price - stop
    return StopState(
        entry_price=entry_price,
        initial_stop=stop,
        stop=stop,
        take_profit=entry_price + p.rr_target * risk,
        initial_risk_per_unit=risk,
        highest_price=entry_price,
    )


def update_stop(st: StopState, price_high: float, atr_value: float, p: RiskParams, roundtrip_cost_per_unit: float) -> StopState:
    """Atualiza break-even e trailing com a máxima observada. O stop NUNCA diminui.

    Break-even = preço de entrada + custos de ida e volta por unidade (não apenas o preço de entrada)."""
    st.highest_price = max(st.highest_price, price_high)
    r = st.initial_risk_per_unit
    gain_r = (st.highest_price - st.entry_price) / r if r > 0 else 0.0
    new_stop = st.stop
    if not st.breakeven_done and gain_r >= p.breakeven_trigger_r:
        be = st.entry_price + roundtrip_cost_per_unit
        new_stop = max(new_stop, be)
        st.breakeven_done = True
    if gain_r >= p.trailing_activation_r and atr_value > 0:
        st.trailing_active = True
    if st.trailing_active and atr_value > 0:
        new_stop = max(new_stop, st.highest_price - p.trailing_atr_mult * atr_value)
    st.stop = max(st.stop, new_stop)
    return st


def check_exit(st: StopState, low: float, high: float, open_: float) -> tuple[str, float] | None:
    """Verifica saída intra-candle. Se ambos forem tocados no mesmo candle, assume o STOP (conservador).

    Retorna (motivo, preço de referência) — gaps contra a posição executam na abertura."""
    if low <= st.stop:
        return ("stop", min(open_, st.stop))
    if high >= st.take_profit:
        return ("take_profit", max(open_, st.take_profit))
    return None
