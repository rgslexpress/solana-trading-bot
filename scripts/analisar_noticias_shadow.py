"""Análise SHADOW da camada News & Market Intelligence (somente leitura/simulação; sem ordens, sem transações).

  # 1) Backtest COM vs SEM notícias (exige candles em CSV e um arquivo de notícias históricas .jsonl)
  python scripts\\analisar_noticias_shadow.py --backtest --arquivo data\\noticias.jsonl --liquidity 500000

  # 1b) Idem, incluindo macroeconomia dos EUA (eventos/valores/mercado com timestamps de DISPONIBILIDADE; ver docs/NEWS_INTEL.md)
  python scripts\\analisar_noticias_shadow.py --backtest --arquivo data\\noticias.jsonl --macro data\\macro.jsonl --liquidity 500000

  # 2) Relatório do paper trading em modo SHADOW (usa a base SQLite do robô)
  python scripts\\analisar_noticias_shadow.py --paper

Formato do arquivo (uma linha JSON por notícia): {"source_id","title","summary","url","published_at"[,"collected_at"]}.
Sem "collected_at" presume-se disponibilidade = publicação + BOT_INTEL_ASSUMED_LATENCY_S. NÃO conclui que o módulo melhora o resultado."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.runner import load_history  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.factory import build_provider  # noqa: E402
from app.intel.classifier import Classifier  # noqa: E402
from app.intel.macro import load_macro_archive  # noqa: E402
from app.intel.replay import IntelReplay, load_archive  # noqa: E402
from app.intel.shadow import AVISO, compare_backtests, live_shadow_report  # noqa: E402
from app.intel.sources import SourceError, load_sources  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--paper", action="store_true")
    ap.add_argument("--arquivo", help="notícias históricas (.jsonl)")
    ap.add_argument("--macro", help="eventos/valores/mercado macro históricos (.jsonl; valores só valem a partir de observed_at)")
    ap.add_argument("--liquidity", type=float, default=5_000_000.0)
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()
    s = load_settings(a.env_file if Path(a.env_file).exists() else None)
    if a.paper:
        c = sqlite3.connect(f"{Path(s.db_path).resolve().as_uri()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        print(json.dumps(live_shadow_report(c), indent=2, ensure_ascii=False, default=str))
        return 0
    if a.backtest:
        if not a.arquivo:
            print("Informe --arquivo com as notícias históricas (.jsonl).")
            return 2
        tokens, _ = s.load_tokens()
        data, _hashes = load_history(s, build_provider(s), tokens)
        try:
            sources = {x.id: x for x in load_sources(s.intel.sources_file)}
        except SourceError:
            sources = {}
        clf = Classifier([("SOL", ())] + [(t.symbol, t.aliases) for t in tokens.values()], s.intel)
        records = load_archive(a.arquivo, clf, sources, s.intel)
        print(f"{len(records)} notícias carregadas e classificadas.")
        m_events, m_values, m_market = load_macro_archive(a.macro, s.intel) if a.macro else ([], [], [])
        if a.macro:
            print(f"macro: {len(m_events)} evento(s), {len(m_values)} valor(es), {len(m_market)} ponto(s) de mercado carregados.")
        res = compare_backtests(s, data, a.liquidity, IntelReplay(records, s.intel, macro_events=m_events, macro_values=m_values, macro_market=m_market))
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return 0
    print(AVISO)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
