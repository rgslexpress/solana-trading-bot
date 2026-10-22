"""Replay/shadow SEM vazamento de futuro: cada decisão usa apenas notícias/dados macro publicados E disponíveis naquele instante.

Arquivo de notícias (JSON Lines, um objeto por linha): {"source_id","title","summary","url","published_at"[,"collected_at"]}.
Sem `collected_at`, presume-se disponibilidade = publicação + `assumed_latency_s` (premissa NÃO calibrada).
Arquivo macro (JSON Lines): ver app.intel.macro.load_macro_archive (valores/revisões/mercado só valem a partir de `observed_at`)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.config import IntelParams
from app.intel.classifier import Classifier
from app.intel.dedup import safe_url
from app.intel.engine import IntelEngine
from app.intel.macro import MacroEngine, MemoryMacroView, MacroEvent, MacroMarket, MacroValue
from app.intel.models import IntelAssessment, NewsRecord, RawItem
from app.intel.sources import SourceConfig, clean_text, parse_date
from app.intel.store import MemoryNewsView


def load_archive(path: str | Path, classifier: Classifier, sources: dict[str, SourceConfig], params: IntelParams) -> list[NewsRecord]:
    """Carrega e classifica um arquivo histórico. Linhas inválidas são descartadas e contadas no log do chamador."""
    records: list[NewsRecord] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            pub = parse_date(d.get("published_at"))
            url = safe_url(str(d.get("url", "")))
            title = clean_text(d.get("title"), 300)
            if pub is None or not url or not title:
                continue
            coll = parse_date(d.get("collected_at")) or (pub + timedelta(seconds=params.assumed_latency_s))
            coll = max(coll, pub)  # nunca disponível antes de publicada
            sid = str(d.get("source_id", "arquivo"))
            src = sources.get(sid) or SourceConfig(sid, sid, "rss", "https://arquivo.invalid/", 0.5)
            raw = RawItem(sid, title, clean_text(d.get("summary"), 600), url, pub, coll)
            records.append(classifier.classify(raw, src, coll))
        except (ValueError, TypeError, AttributeError):
            continue
    return records


class IntelReplay:
    """Portão de decisão para o backtest: `gate(símbolo, instante)` -> avaliação usando só o que existia naquele instante."""

    def __init__(self, records: list[NewsRecord], params: IntelParams, macro_events: list[MacroEvent] | None = None, macro_values: list[MacroValue] | None = None,
                 macro_market: list[MacroMarket] | None = None):
        replay_params = IntelParams(**{**params.__dict__, "mode": "enforce"})
        macro = None
        if macro_events or macro_values or macro_market:
            macro = MacroEngine(replay_params, MemoryMacroView(macro_events, macro_values, macro_market))
        self.engine = IntelEngine(replay_params, MemoryNewsView(records), macro)

    def gate(self, symbol: str, now: datetime) -> IntelAssessment:
        return self.engine.assess(symbol, now, "BUY")
