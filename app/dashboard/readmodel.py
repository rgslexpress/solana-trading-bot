"""Leitura SOMENTE LEITURA do estado do robô para o painel.

Cada consulta abre uma conexão própria em modo somente leitura (SQLite WAL permite leitores concorrentes ao
escritor do robô). Métricas que não existem ou não podem ser calculadas corretamente retornam None (a interface exibe
"N/D"). Todos os números aqui são SIMULADOS (paper trading): não há resultados reais neste sistema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import TIMEFRAME_SECONDS, Settings
from app.domain import Position
from app.logging_config import redact_text


@contextmanager
def open_ro(db_path: str) -> Iterator[sqlite3.Connection | None]:
    p = Path(db_path)
    if not p.exists():
        yield None
        return
    conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)  # URI correta também no Windows (file:///C:/...)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _meta(c: sqlite3.Connection, key: str, default: Any = None) -> Any:
    try:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return json.loads(r["value"]) if r else default


def _rows(c: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(c.execute(sql, args))
    except sqlite3.OperationalError:  # tabela ainda não criada (base nova)
        return []


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def kill_switch_state(c: sqlite3.Connection | None) -> dict[str, Any]:
    if c is None:
        return {"ativo": False, "motivo": "", "desde": None}
    r = _rows(c, "SELECT ts, active, reason FROM kill_switch_events ORDER BY id DESC LIMIT 1")
    return {"ativo": bool(r[0]["active"]), "motivo": r[0]["reason"], "desde": r[0]["ts"]} if r else {"ativo": False, "motivo": "", "desde": None}


def positions(c: sqlite3.Connection | None, status: str) -> list[Position]:
    if c is None:
        return []
    return [Position.from_dict(json.loads(r["data_json"])) for r in _rows(c, "SELECT data_json FROM positions WHERE status=? ORDER BY updated_ts", (status,))]


def data_status(c: sqlite3.Connection | None, s: Settings, now: datetime) -> dict[str, Any]:
    """Idade da última cotação/candle e alertas de dados obsoletos, por token."""
    raw = _meta(c, "data_status", {}) if c else {}
    tf = TIMEFRAME_SECONDS[s.strategy.entry_timeframe]
    limit_s = s.data.max_data_staleness_candles * tf
    out: dict[str, Any] = {}
    alerts: list[str] = []
    for sym, d in raw.items():
        last = _parse(d.get("ultimo_candle_fechado"))
        chk = _parse(d.get("checado_em"))
        age_candle = (now - last).total_seconds() if last else None
        age_check = (now - chk).total_seconds() if chk else None
        stale = d.get("ok") is not True or age_candle is None or age_candle > limit_s
        out[sym] = {
            "ok": bool(d.get("ok")), "problemas": d.get("problemas", []), "fonte": d.get("fonte"),
            "idade_ultimo_candle_s": age_candle, "idade_ultima_verificacao_s": age_check, "ultimo_preco": d.get("ultimo_preco"),
            "obsoleto": bool(stale),
        }
        if not d.get("ok"):
            alerts.append(f"{sym}: dados inválidos/indisponíveis — {'; '.join(d.get('problemas', [])) or 'sem detalhe'}")
        elif stale:
            alerts.append(f"{sym}: dados OBSOLETOS (último candle fechado há {int(age_candle or 0)} s; limite {int(limit_s)} s)")
    return {"tokens": out, "alertas": alerts, "limite_obsolescencia_s": limit_s}


def metrics(c: sqlite3.Connection | None, s: Settings) -> dict[str, Any]:
    """Métricas SIMULADAS. Campos não calculáveis => None."""
    m: dict[str, Any] = {k: None for k in (
        "saldo_inicial", "patrimonio", "pnl_realizado", "pnl_nao_realizado", "pnl_total", "rentabilidade_pct", "drawdown_max_pct",
        "operacoes_encerradas", "taxa_acerto", "ganho_medio", "perda_media", "profit_factor", "custos_totais", "caixa", "exposicao",
        "ultimo_registro_patrimonio", "sharpe")}
    m["rotulo"] = "SIMULADO (paper trading) — não há resultados reais"
    if c is None:
        m["saldo_inicial"] = s.paper_capital
        return m
    risk = _meta(c, "risk_state", {}) or {}
    initial = risk.get("initial_equity") or None
    m["saldo_inicial"] = float(initial) if initial else s.paper_capital  # sem registro ainda: capital configurado
    m["saldo_inicial_origem"] = "registrado pelo robô" if initial else "capital configurado (BOT_PAPER_CAPITAL)"
    eq_rows = _rows(c, "SELECT ts, equity, cash, exposure FROM equity ORDER BY id")
    closed = positions(c, "CLOSED")
    openp = positions(c, "OPEN")
    if closed:
        pnls = [p.pnl_quote or 0.0 for p in closed]
        wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x <= 0]
        m.update(operacoes_encerradas=len(closed), pnl_realizado=sum(pnls), taxa_acerto=len(wins) / len(pnls),
                 ganho_medio=(sum(wins) / len(wins)) if wins else None, perda_media=(sum(losses) / len(losses)) if losses else None,
                 profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
                 custos_totais=sum(p.cost_total_quote for p in closed))
    else:
        m["operacoes_encerradas"] = 0
        m["pnl_realizado"] = 0.0 if eq_rows else None
    prices = _meta(c, "last_prices", {}) or {}
    if not openp:
        m["pnl_nao_realizado"] = 0.0 if eq_rows else None
    elif all(p.mint in prices for p in openp):
        m["pnl_nao_realizado"] = sum((prices[p.mint] - p.entry_price) * p.qty for p in openp)  # bruto de custos de saída
    if eq_rows:
        last = eq_rows[-1]
        m.update(patrimonio=last["equity"], caixa=last["cash"], exposicao=last["exposure"], ultimo_registro_patrimonio=last["ts"])
        if m["saldo_inicial"]:
            m["pnl_total"] = last["equity"] - m["saldo_inicial"]
            m["rentabilidade_pct"] = m["pnl_total"] / m["saldo_inicial"] * 100
        if len(eq_rows) >= 2:
            peak, dd = eq_rows[0]["equity"], 0.0
            for r in eq_rows:
                peak = max(peak, r["equity"])
                dd = min(dd, (r["equity"] - peak) / peak) if peak > 0 else dd
            m["drawdown_max_pct"] = dd * 100
    return m


def equity_curve(c: sqlite3.Connection | None, limit: int = 300) -> list[dict[str, Any]]:
    if c is None:
        return []
    rows = _rows(c, "SELECT ts, equity FROM equity ORDER BY id DESC LIMIT ?", (limit,))
    return [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(rows)]


def open_positions_view(c: sqlite3.Connection | None) -> list[dict[str, Any]]:
    prices = _meta(c, "last_prices", {}) if c else {}
    out = []
    for p in positions(c, "OPEN"):
        px = prices.get(p.mint)
        out.append({"id": p.id, "simbolo": p.symbol, "quantidade": p.qty, "preco_entrada": p.entry_price, "entrada_em": p.entry_ts,
                    "stop": p.stop.stop, "alvo": p.stop.take_profit, "preco_atual": px,
                    "pnl_nao_realizado": (px - p.entry_price) * p.qty if px is not None else None})
    return out


def closed_trades_view(c: sqlite3.Connection | None, limit: int = 100) -> list[dict[str, Any]]:
    out = []
    for p in reversed(positions(c, "CLOSED")[-limit:]):
        out.append({"id": p.id, "simbolo": p.symbol, "quantidade": p.qty, "preco_entrada": p.entry_price, "entrada_em": p.entry_ts,
                    "preco_saida": p.exit_price, "saida_em": p.exit_ts, "motivo_saida": p.exit_reason, "pnl_liquido": p.pnl_quote, "custos": p.cost_total_quote})
    return out


def decisions_view(c: sqlite3.Connection | None, limit: int = 100) -> list[dict[str, Any]]:
    if c is None:
        return []
    limit = max(1, min(int(limit), 500))
    return [{"ts": r["ts"], "simbolo": r["symbol"], "candle": r["candle_ts"], "tipo": r["type"], "pontuacao": r["score"],
             "motivos": json.loads(r["reasons"] or "[]"), "rejeicoes": json.loads(r["rejections"] or "[]")}
            for r in _rows(c, "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))]


def logs_view(c: sqlite3.Connection | None, log_file: str, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 300))
    alerts = [{"ts": r["ts"], "nivel": r["level"], "mensagem": redact_text(r["message"])} for r in _rows(c, "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))] if c else []
    events = [{"ts": r["ts"], "evento": r["event"], "detalhe": redact_text(r["detail"] or ""), "origem": r["source"]}
              for r in _rows(c, "SELECT * FROM bot_events ORDER BY id DESC LIMIT ?", (limit,))] if c else []
    lines: list[dict[str, Any]] = []
    p = Path(log_file) if log_file else None
    if p and p.exists():
        try:
            with open(p, "rb") as fh:  # lê só o final do arquivo
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 200_000))
                tail = fh.read().decode("utf-8", "replace").splitlines()[-limit:]
        except OSError:
            tail = []
        for ln in reversed(tail):
            try:
                d = json.loads(ln)
                lines.append({"ts": d.get("ts"), "nivel": d.get("nivel"), "origem": d.get("logger"), "mensagem": redact_text(str(d.get("msg", "")))[:500]})
            except ValueError:
                lines.append({"ts": None, "nivel": None, "origem": None, "mensagem": redact_text(ln)[:500]})
    errors = [x for x in lines if x["nivel"] in ("ERROR", "CRITICAL")]
    return {"alertas": alerts, "eventos": events, "log": lines, "erros": errors, "arquivo_de_log": log_file or None, "arquivo_existe": bool(p and p.exists())}


def config_view(s: Settings) -> dict[str, Any]:
    """Whitelist explícita: nenhum campo de carteira/credencial/RPC é exposto."""
    r, x, st, d = s.risk, s.execution, s.strategy, s.data
    return {
        "modo_configurado": "paper (o painel só executa PAPER)",
        "capital_paper": s.paper_capital, "moeda_de_referencia": s.quote_symbol, "intervalo_ciclo_s": s.poll_seconds,
        "risco": {k: getattr(r, k) for k in (
            "risk_per_trade_pct", "max_total_exposure_pct", "max_token_exposure_pct", "max_open_positions", "daily_loss_limit_pct",
            "max_cumulative_loss_pct", "max_trades_per_day", "max_consecutive_losses", "cooldown_minutes", "min_order_quote",
            "atr_stop_mult", "rr_target", "breakeven_trigger_r", "trailing_activation_r", "trailing_atr_mult", "on_limit_policy")},
        "execucao_simulada": {k: getattr(x, k) for k in (
            "fee_bps", "network_fee_quote", "base_slippage_bps", "max_slippage_bps", "max_price_impact_bps", "min_liquidity_usd", "max_consecutive_exec_failures")},
        "estrategia": {k: getattr(st, k) for k in (
            "entry_timeframe", "htf_timeframe", "ema_fast", "ema_slow", "ema_trend", "rsi_period", "rsi_buy_max", "min_score", "min_relative_volume", "max_cost_bps", "version")},
        "dados": {"fonte": d.source, "tolerancia_obsolescencia_candles": d.max_data_staleness_candles, "carencia_fechamento_s": d.close_grace_s,
                  "lacuna_maxima_candles": d.max_gap_candles},
    }


def positions_for(db_path: str, status: str) -> list[Position]:
    with open_ro(db_path) as c:
        return positions(c, status)
