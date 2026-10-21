"""Relatórios reproduzíveis de backtest (JSON + CSV de operações + Markdown)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestResult
from app.logging_config import redact_obj

DISCLAIMER = (
    "AVISO: resultados de backtest são históricos e dependem de premissas de custo/liquidez. "
    "Não são evidência de rentabilidade futura."
)


def dataframe_hash(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=True).values.tobytes()).hexdigest()[:16]


def _fmt(v: Any) -> str:
    if v is None:
        return "n/d"
    if isinstance(v, float):
        return f"{v:,.4f}"
    return str(v)


def write_report(
    out_dir: str,
    name: str,
    results: dict[str, BacktestResult],
    params: dict[str, Any],
    data_info: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    """`results`: {nome_do_periodo: BacktestResult}. Retorna os caminhos gerados."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = out / f"{name}_{stamp}"
    payload = {
        "gerado_em": stamp,
        "aviso": DISCLAIMER,
        "parametros": redact_obj(params),
        "dados": data_info,
        "periodos": {k: {"metricas": r.metrics, "sinais": r.signal_counts, "rejeicoes": r.rejection_counts, "kill_events": r.kill_events} for k, r in results.items()},
        "extra": extra or {},
    }
    (base.with_suffix(".json")).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    all_trades = pd.concat([r.trades.assign(periodo=k) for k, r in results.items()], ignore_index=True)
    all_trades.to_csv(base.with_name(base.name + "_trades.csv"), index=False)

    lines = [f"# Relatório de backtest — {name}", "", f"> {DISCLAIMER}", "", f"Gerado em {stamp} (UTC).", ""]
    lines += ["## Dados e parâmetros", "", "```json", json.dumps({"dados": data_info, "parametros": redact_obj(params)}, indent=2, ensure_ascii=False, default=str), "```", ""]
    keys = ["initial_equity", "final_equity", "net_return_pct", "gross_return", "num_trades", "win_rate", "avg_win", "avg_loss",
            "profit_factor", "expectancy_per_trade", "max_drawdown_pct", "sharpe", "sortino", "avg_bars_held", "total_costs",
            "avg_exposure_pct", "max_exposure_pct", "benchmark_buy_hold_return_pct"]
    lines += ["## Métricas por período", "", "| Métrica | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(_fmt(r.metrics.get(k)) for r in results.values()) + " |")
    lines.append("")
    for pname, r in results.items():
        lines += [f"### {pname}: resultado por token / condição / motivo de saída", ""]
        for grp in ("by_token", "by_timeframe", "by_condition", "by_exit_reason"):
            g = r.metrics.get(grp) or {}
            for key, v in g.items():
                lines.append(f"- {grp}[{key}]: {v['trades']} operações, líquido {_fmt(v['net_pnl'])}, acerto {_fmt(v['win_rate'])}")
        if r.kill_events:
            lines.append(f"- kill switch: {r.kill_events}")
        lines.append("")
    if extra:
        for title, df in extra.items():
            if isinstance(df, pd.DataFrame):
                lines += [f"## {title}", "", df.to_markdown(index=False) if hasattr(df, "to_markdown") and _has_tabulate() else df.to_string(index=False), ""]
    lines += ["## Metodologia", "", "- Sinal no fechamento do candle t; execução na abertura de t+1 com slippage e taxas.",
              "- Stop/TP intra-candle; se ambos tocados, assume-se stop. Gaps executam na abertura.",
              "- Sharpe/Sortino: ver campo `methodology` no JSON.", "- Liquidez é premissa fixa (sem histórico de pools).", ""]
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(base.with_suffix(".json")), "md": str(base.with_suffix(".md")), "trades": str(base.with_name(base.name + "_trades.csv"))}


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False
