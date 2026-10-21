"""Relatórios operacionais (paper/live) a partir do banco."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage.database import Database


def performance_summary(db: Database) -> dict[str, Any]:
    closed = db.load_positions("CLOSED")
    openp = db.load_positions("OPEN")
    pnls = [p.pnl_quote or 0.0 for p in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    eq = db.conn.execute("SELECT ts, equity, cash, exposure FROM equity ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "operacoes_encerradas": len(closed),
        "operacoes_abertas": len(openp),
        "pnl_liquido": sum(pnls),
        "taxa_de_acerto": (len(wins) / len(pnls)) if pnls else None,
        "ganho_medio": (sum(wins) / len(wins)) if wins else None,
        "perda_media": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "custos_totais": sum(p.cost_total_quote for p in closed),
        "ultimo_patrimonio": dict(eq) if eq else None,
        "sinais": {r["type"]: r["n"] for r in db.conn.execute("SELECT type, COUNT(*) n FROM signals GROUP BY type")},
        "intencoes": {r["state"]: r["n"] for r in db.conn.execute("SELECT state, COUNT(*) n FROM intents GROUP BY state")},
    }


def write_operational_report(db: Database, out_dir: str, title: str = "paper") -> str:
    s = performance_summary(db)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / f"operacional_{title}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    lines = [f"# Relatório operacional ({title})", "", "> Resultados simulados/históricos não garantem resultados futuros.", ""]
    for k, v in s.items():
        lines.append(f"- **{k}**: {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
