"""Análise SHADOW: compara decisões/resultados COM e SEM a camada de notícias, sem executar nada.

Atenção estatística: amostras pequenas e um único período NÃO provam que o módulo melhora (ou piora) o resultado.
Os números incluem as mesmas taxas/slippage/latência do simulador e, no backtest, só usam notícias disponíveis em cada instante."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestEngine, TokenData
from app.config import Settings
from app.intel.replay import IntelReplay

AVISO = ("Comparação descritiva. Não é evidência de melhora de rentabilidade: poucas operações, um único período e premissas de custo "
         "não calibradas. Só conclua com resultados robustos, fora da amostra, com dados e notícias reais.")
_KEYS = ["num_trades", "net_return_pct", "profit_factor", "max_drawdown_pct", "win_rate", "total_costs"]


def compare_backtests(settings: Settings, data: dict[str, TokenData], liquidity: float, replay: IntelReplay, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict[str, Any]:
    base = BacktestEngine(settings, data, liquidity, start=start, end=end).run()
    withi = BacktestEngine(settings, data, liquidity, start=start, end=end, intel_gate=replay.gate).run()
    pick = lambda m: {k: m.get(k) for k in _KEYS}  # noqa: E731
    diff = {k: (None if base.metrics.get(k) is None or withi.metrics.get(k) is None else withi.metrics[k] - base.metrics[k]) for k in ("num_trades", "net_return_pct", "max_drawdown_pct", "total_costs")}
    return {"sem_noticias": pick(base.metrics), "com_noticias": pick(withi.metrics), "diferenca": diff,
            "entradas_bloqueadas": sum(1 for e in withi.intel_events if e["bloqueou"]), "entradas_com_risco_reduzido": sum(1 for e in withi.intel_events if not e["bloqueou"]),
            "eventos": withi.intel_events[:200], "aviso": AVISO}


def live_shadow_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Relatório do paper trading em modo SHADOW: o que a camada TERIA feito e o que aconteceu com essas operações."""
    rows = list(conn.execute("SELECT * FROM intel_decisions ORDER BY id"))
    pos = {r["id"]: json.loads(r["data_json"]) for r in conn.execute("SELECT id, data_json FROM positions")}
    out: dict[str, Any] = {"decisoes": len(rows), "aviso": AVISO}
    groups = {"mudaria": [], "sem_mudanca": []}
    for r in rows:
        pnl = pos.get(r["position_id"], {}).get("pnl_quote") if r["position_id"] else None
        groups["mudaria" if r["would_change"] else "sem_mudanca"].append(pnl)
    for name, vals in groups.items():
        closed = [v for v in vals if v is not None]
        out[name] = {"decisoes": len(vals), "operacoes_encerradas": len(closed), "pnl_liquido_total": sum(closed) if closed else None,
                     "pnl_medio": (sum(closed) / len(closed)) if closed else None}
    out["bloquearia"] = sum(1 for r in rows if "bloque" in (r["action"] or "").lower())
    out["divergencias"] = sum(1 for r in rows if r["divergence"])
    # quantas decisões caíram dentro de uma janela macro / foram afetadas por fontes indisponíveis (para comparar com os eventos reais depois)
    out["em_janela_macro"] = sum(1 for r in rows if "macro_calendario" in (r["alerts"] or ""))
    out["com_dados_degradados"] = sum(1 for r in rows if (r["data_state"] or "") in ("unavailable", "stale", "invalid", "contingency"))
    return out
