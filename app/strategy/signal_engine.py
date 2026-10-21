"""Geração de sinais: features vetorizadas e causais + regras explícitas e explicáveis.

`compute_features` é usada IGUALMENTE por backtest, paper e live (paridade). O valor da linha t
depende só de candles fechados até t. Candles de timeframe superior só entram após seu fechamento.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from app.config import TIMEFRAME_SECONDS, StrategyParams
from app.domain import SignalDecision, SignalType
from app.strategy import indicators as ind


def compute_features(
    entry: pd.DataFrame,
    htf: pd.DataFrame | None,
    p: StrategyParams,
    ref: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Calcula todas as colunas usadas pelas regras. Índice = abertura do candle de entrada (UTC)."""
    tf = TIMEFRAME_SECONDS[p.entry_timeframe]
    htf_s = TIMEFRAME_SECONDS[p.htf_timeframe]
    df = entry[["open", "high", "low", "close", "volume"]].copy()
    c = df["close"]
    df["ema_fast"] = ind.ema(c, p.ema_fast)
    df["ema_slow"] = ind.ema(c, p.ema_slow)
    above = df["ema_fast"] > df["ema_slow"]
    cross_up = above & ~above.shift(1, fill_value=False)
    df["recent_cross"] = cross_up.rolling(p.cross_lookback, min_periods=1).max().astype(bool)
    m = ind.macd(c, p.macd_fast, p.macd_slow, p.macd_signal)
    df = df.join(m)
    df["macd_hist_prev"] = df["macd_hist"].shift(1)
    df["rsi"] = ind.rsi(c, p.rsi_period)
    df = df.join(ind.bollinger(c, p.bb_period, p.bb_std))
    df["atr"] = ind.atr(df["high"], df["low"], c, p.atr_period)
    df["rel_vol"] = ind.relative_volume(df["volume"], p.volume_ma_period)
    df["dollar_volume"] = df["close"] * df["volume"]
    df["close_time"] = df.index + pd.Timedelta(seconds=tf)

    # --- timeframe superior (somente candles FECHADOS no instante de fechamento do candle de entrada)
    if htf is not None and len(htf) > 0:
        h = pd.DataFrame(index=htf.index)
        h["htf_close"] = htf["close"]
        h["htf_ema_trend"] = ind.ema(htf["close"], p.ema_trend)
        h["htf_ema_trend_prev"] = h["htf_ema_trend"].shift(3)
        up = (h["htf_close"] > h["htf_ema_trend"]) & (h["htf_ema_trend"] >= h["htf_ema_trend_prev"])
        unknown = h["htf_ema_trend"].isna() | h["htf_ema_trend_prev"].isna()
        h["htf_up"] = up.astype(object).where(~unknown, None)
        h["htf_close_time"] = h.index + pd.Timedelta(seconds=htf_s)
        h = h.reset_index(drop=True).sort_values("htf_close_time")
        left = df.reset_index().rename(columns={df.index.name or "index": "open_time"})
        merged = pd.merge_asof(
            left.sort_values("close_time"),
            h[["htf_close_time", "htf_close", "htf_ema_trend", "htf_up"]],
            left_on="close_time",
            right_on="htf_close_time",
            direction="backward",
        )
        merged = merged.set_index("open_time").loc[df.index]
        df["htf_up"] = merged["htf_up"].astype(object)
        df["htf_ema_trend"] = merged["htf_ema_trend"]
    else:
        df["htf_up"] = pd.Series([pd.NA] * len(df), index=df.index, dtype=object)
        df["htf_ema_trend"] = float("nan")

    # --- ativo de referência (ex.: SOL): correlação de retornos e tendência
    if ref is not None and len(ref) > 0:
        rc = ref["close"].reindex(df.index)
        df["ref_corr"] = ind.rolling_return_corr(df["close"], rc, p.corr_window)
        df["ref_up"] = (rc > ind.ema(rc, p.ema_slow)).astype(object)
    else:
        df["ref_corr"] = float("nan")
        df["ref_up"] = pd.Series([pd.NA] * len(df), index=df.index, dtype=object)
    return df


@dataclass(frozen=True)
class ExecContext:
    """Informações de execução conhecidas no momento da decisão."""

    liquidity_usd: float | None
    min_liquidity_usd: float
    est_cost_bps: float  # custo estimado de ida (taxa + slippage)


_REQUIRED = ["ema_fast", "ema_slow", "macd_hist", "macd_hist_prev", "rsi", "bb_upper", "atr", "rel_vol"]


def _isnan(v: Any) -> bool:
    return v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v))


def decide(row: Mapping[str, Any], p: StrategyParams, ctx: ExecContext, has_position: bool) -> SignalDecision:
    """Decisão para UM candle fechado. Pura e determinística."""
    rej: list[str] = []
    if any(_isnan(row.get(k)) for k in _REQUIRED):
        return SignalDecision(SignalType.NO_TRADE, rejections=["indicadores em aquecimento/inválidos"])
    close = float(row["close"])
    ema_f, ema_s, atr_v = float(row["ema_fast"]), float(row["ema_slow"]), float(row["atr"])
    if atr_v <= 0 or close <= 0:
        return SignalDecision(SignalType.NO_TRADE, rejections=["ATR/preço inválido"])
    hist, hist_prev = float(row["macd_hist"]), float(row["macd_hist_prev"])
    rsi_v = float(row["rsi"])
    htf_up = row.get("htf_up")
    htf_known = not _isnan(htf_up)
    details = {
        "close": close, "ema_fast": ema_f, "ema_slow": ema_s, "rsi": rsi_v, "macd_hist": hist,
        "atr": atr_v, "rel_vol": float(row["rel_vol"]), "htf_up": None if not htf_known else bool(htf_up),
    }

    # ------------------------------------------------------------ saídas (se há posição)
    if has_position:
        reasons = []
        if ema_f < ema_s:
            reasons.append("EMA rápida abaixo da lenta (perda de tendência)")
        if hist < 0 and hist < hist_prev and close < ema_f:
            reasons.append("MACD negativo e em queda com preço abaixo da EMA rápida")
        if rsi_v >= p.rsi_overbought and close > float(row["bb_upper"]):
            reasons.append("RSI sobrecomprado com preço acima da banda superior")
        if p.require_htf_confirmation and htf_known and not bool(htf_up):
            reasons.append("tendência do timeframe superior virou desfavorável")
        if reasons:
            return SignalDecision(SignalType.SELL, score=float(len(reasons)), reasons=reasons, details=details)
        return SignalDecision(SignalType.HOLD, reasons=["sem condição de saída"], details=details)

    # ------------------------------------------------------------ filtros de qualidade (NO_TRADE)
    if ctx.liquidity_usd is None:
        rej.append("liquidez desconhecida")
    elif ctx.liquidity_usd < ctx.min_liquidity_usd:
        rej.append(f"liquidez insuficiente ({ctx.liquidity_usd:,.0f} < {ctx.min_liquidity_usd:,.0f} USD)")
    if ctx.est_cost_bps > p.max_cost_bps:
        rej.append(f"custo estimado alto ({ctx.est_cost_bps:.1f} bps > {p.max_cost_bps:.1f})")
    if float(row["rel_vol"]) < p.min_relative_volume:
        rej.append(f"volume relativo baixo ({float(row['rel_vol']):.2f})")
    if p.require_htf_confirmation and not htf_known:
        rej.append("sem confirmação de timeframe superior")
    if p.use_ref_filter:
        corr = row.get("ref_corr")
        if _isnan(corr) or _isnan(row.get("ref_up")):
            rej.append("filtro de referência sem dados")
        elif float(corr) >= p.corr_min and not bool(row["ref_up"]):
            rej.append("ativo de referência em tendência contrária (alta correlação)")
    if rej:
        return SignalDecision(SignalType.NO_TRADE, rejections=rej, details=details)

    # ------------------------------------------------------------ pontuação de entrada
    score, reasons = 0.0, []
    if htf_known and bool(htf_up):
        score += 2
        reasons.append("timeframe superior favorável")
    elif p.require_htf_confirmation:
        rej.append("timeframe superior desfavorável")
    if ema_f > ema_s:
        score += 1
        reasons.append("EMA rápida acima da lenta")
        if bool(row["recent_cross"]):
            score += 1
            reasons.append("cruzamento de alta recente")
        else:
            rej.append("sem cruzamento recente (evita entrada tardia)")
    else:
        rej.append("EMA rápida abaixo da lenta")
    if hist > 0:
        score += 1
        reasons.append("MACD positivo")
        if hist > hist_prev:
            score += 1
            reasons.append("histograma MACD crescente")
    else:
        rej.append("MACD não confirma momentum")
    if rsi_v <= p.rsi_buy_max:
        score += 1
        reasons.append(f"RSI {rsi_v:.0f} sem sobrecompra")
    else:
        rej.append(f"RSI {rsi_v:.0f} acima do máximo de entrada")
    extension = (close - ema_s) / atr_v
    if close <= float(row["bb_upper"]) and extension <= p.max_extension_atr:
        score += 1
        reasons.append("preço não estendido")
    else:
        rej.append(f"preço estendido ({extension:.1f} ATR acima da EMA lenta)")

    details["score"] = score
    # portões obrigatórios: nenhuma rejeição pendente e pontuação mínima
    if not rej and score >= p.min_score:
        return SignalDecision(SignalType.BUY, score=score, reasons=reasons, details=details)
    if score < p.min_score:
        rej.append(f"pontuação {score:.0f} < mínimo {p.min_score:.0f}")
    return SignalDecision(SignalType.NO_TRADE, score=score, reasons=reasons, rejections=rej, details=details)
