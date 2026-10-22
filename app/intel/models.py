"""Modelos da camada News & Market Intelligence. Todo texto externo é DADO: nunca é interpretado como comando."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

IMPACT_NAMES = {0: "nenhum", 1: "baixo", 2: "médio", 3: "alto", 4: "crítico"}
CERTAINTY = ("confirmed", "claim", "rumor")  # fato confirmado | alegação | rumor
POLARITY = ("positive", "negative", "neutral", "inconclusive")
NATURES = ("fato", "expectativa", "opiniao", "especulacao", "desconhecido")  # decisão/dado anunciado | expectativa de mercado | opinião | boato
ENVIRONMENTS = ("mainnet", "rpc", "devnet", "testnet", "explorer", "site", "desconhecido")
DOMAINS = ("solana_status", "crypto_news", "macro_us")
# Itens com qualquer um destes sinalizadores NUNCA geram alerta nem bloqueio (continuam visíveis no painel).
# "atualizacao" (nova versão de um incidente de status) NÃO está aqui: atualizações oficiais são relevantes (severidade/resolução).
IGNORE_FLAGS = frozenset({"antiga", "reapresentada", "instrucao_suspeita", "data_futura", "sem_data", "duplicada"})
# Estados de saúde de uma fonte
SOURCE_STATES = ("ok", "stale", "unavailable", "invalid", "disabled")
RESOLVED_STATES = frozenset({"resolved", "completed"})


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RawItem:
    source_id: str
    title: str
    summary: str
    url: str
    published_at: datetime | None  # UTC (None = a fonte não informou data confiável)
    collected_at: datetime  # UTC
    status_state: str | None = None  # estado ESTRUTURADO de incidente (Statuspage): investigating|identified|monitoring|resolved|...
    refs: tuple[str, ...] = ()  # links citados no corpo do item (para detectar derivação de outra matéria)


@dataclass
class NewsRecord:
    id: str
    source_id: str
    source_name: str
    title: str
    summary: str
    url: str
    published_at: str | None
    collected_at: str
    first_seen: str
    assets: list[str] = field(default_factory=list)
    category: str = "outros"
    polarity: str = "neutral"
    certainty: str = "claim"
    relevance: float = 0.0
    reliability: float = 0.0
    impact: int = 0  # impacto do item (0-4), já considerando ambiente e severidade; o motor só ajusta por relevância ao ativo
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    resolution: bool = False
    duplicate_of: str | None = None
    # --- campos da versão 2 ---
    event_key: str | None = None  # identificador estável do evento (URL canônica do incidente); versões do mesmo evento compartilham a chave
    event_time: str | None = None  # quando esta VERSÃO do evento passou a valer (publicação, ou o instante da coleta de uma atualização)
    nature: str = "desconhecido"
    environment: str = "desconhecido"
    severity: int = 0
    status_state: str | None = None
    origin_key: str | None = None  # origem citada no texto ("according to X")
    origin_generic: bool = False  # origem genérica/anônima ("a tweet", "sources")
    refs: list[str] = field(default_factory=list)
    domain: str = "crypto_news"
    retraction: bool = False  # desmentido/correção de um evento anterior
    indicator: str | None = None  # macro: FOMC_DECISION, CPI, ...

    @property
    def published_dt(self) -> datetime | None:
        return parse_iso(self.published_at)

    @property
    def collected_dt(self) -> datetime:
        return parse_iso(self.collected_at) or datetime.now(timezone.utc)

    @property
    def event_dt(self) -> datetime | None:
        return parse_iso(self.event_time) or self.published_dt

    @property
    def available_dt(self) -> datetime:
        """Instante a partir do qual o item PODIA ser usado (nunca antes de ter sido coletado)."""
        return self.collected_dt

    @property
    def actionable(self) -> bool:
        return not (set(self.flags) & IGNORE_FLAGS) and self.duplicate_of is None

    @property
    def closed_episode(self) -> bool:
        """Incidente de fonte estruturada já RESOLVIDO: vale como evento (com fim) e não só como aviso de resolução."""
        return self.resolution and self.status_state in RESOLVED_STATES and self.event_key is not None

    def to_row(self) -> tuple[Any, ...]:
        return (self.id, self.source_id, self.source_name, self.title, self.summary, self.url, self.published_at, self.collected_at, self.first_seen,
                json.dumps(self.assets), self.category, self.polarity, self.certainty, self.relevance, self.reliability, self.impact,
                json.dumps(self.flags, ensure_ascii=False), json.dumps(self.reasons, ensure_ascii=False), int(self.resolution), self.duplicate_of,
                self.event_key, self.event_time, self.nature, self.environment, self.severity, self.status_state, self.origin_key, int(self.origin_generic),
                json.dumps(self.refs), self.domain, int(self.retraction), self.indicator)

    @classmethod
    def from_row(cls, r: Any) -> NewsRecord:
        keys = set(r.keys())

        def g(name: str, default: Any = None) -> Any:
            return r[name] if name in keys and r[name] is not None else default

        return cls(
            id=r["id"], source_id=r["source_id"], source_name=r["source_name"], title=r["title"], summary=r["summary"] or "", url=r["url"],
            published_at=r["published_at"], collected_at=r["collected_at"], first_seen=r["first_seen"], assets=json.loads(r["assets"] or "[]"),
            category=r["category"], polarity=r["polarity"], certainty=r["certainty"], relevance=r["relevance"] or 0.0, reliability=r["reliability"] or 0.0,
            impact=r["impact"] or 0, flags=json.loads(r["flags"] or "[]"), reasons=json.loads(r["reasons"] or "[]"), resolution=bool(r["resolution"]),
            duplicate_of=r["duplicate_of"], event_key=g("event_key"), event_time=g("event_time"), nature=g("nature", "desconhecido"),
            environment=g("environment", "desconhecido"), severity=int(g("severity", 0)), status_state=g("status_state"), origin_key=g("origin_key"),
            origin_generic=bool(g("origin_generic", 0)), refs=json.loads(g("refs", "[]")), domain=g("domain", "crypto_news"), retraction=bool(g("retraction", 0)),
            indicator=g("indicator"),
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id, "fonte": self.source_name, "fonte_id": self.source_id, "titulo": self.title, "resumo": self.summary, "link": self.url,
            "publicado_em": self.published_at, "coletado_em": self.collected_at, "ativos": self.assets, "categoria": self.category,
            "classificacao": self.polarity, "certeza": self.certainty, "relevancia": self.relevance, "confiabilidade": self.reliability,
            "impacto": IMPACT_NAMES.get(self.impact, "?"), "sinalizadores": self.flags, "motivos": self.reasons, "resolucao": self.resolution,
            "duplicada_de": self.duplicate_of, "natureza": self.nature, "ambiente": self.environment, "estado_incidente": self.status_state,
            "dominio": self.domain, "indicador": self.indicator, "desmentido": self.retraction,
        }


@dataclass
class SourceState:
    source_id: str
    name: str
    state: str  # ok | stale | unavailable | invalid | disabled
    last_ok_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_items: int = 0
    last_malformed: int = 0
    domain: str = "crypto_news"
    critical: bool = False
    error_kind: str | None = None  # http | timeout | network | rate_limit | parse | invalid | other
    http_status: int | None = None
    duration_ms: int | None = None
    down_since: str | None = None  # início da indisponibilidade/desatualização atual
    retry_at: str | None = None  # backoff (HTTP 429 / Retry-After)
    recovering: str | None = None  # "1/2": sucessos desde a volta, enquanto a recuperação não foi confirmada
    stale_after_s: float | None = None

    def to_public(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ActiveAlert:
    category: str
    asset: str
    impact: int  # 0-4 específico para o ativo avaliado
    certainty: str
    reliability: float
    corroboration: int  # nº de ORIGENS independentes (não de fontes: republicações contam uma vez)
    title: str
    url: str
    published_at: str | None
    expires_at: str
    blocks: bool
    reason: str
    domain: str = "crypto_news"
    independence: str = "n/a"  # confirmada | incerta | n/a
    environment: str = "desconhecido"

    def to_public(self) -> dict[str, Any]:
        return {
            "categoria": self.category, "ativo": self.asset, "impacto": IMPACT_NAMES.get(self.impact, "?"), "certeza": self.certainty,
            "confiabilidade": round(self.reliability, 2), "fontes_distintas": self.corroboration, "titulo": self.title, "link": self.url,
            "publicado_em": self.published_at, "expira_em": self.expires_at, "bloqueia": self.blocks, "motivo": self.reason,
            "dominio": self.domain, "independencia": self.independence, "ambiente": self.environment,
        }


@dataclass
class IntelAssessment:
    mode: str
    data_state: str  # ok | stale | unavailable | invalid | contingency | disabled
    no_events: bool  # True = fontes OK e nenhum evento relevante (≠ fontes indisponíveis)
    alerts: list[ActiveAlert] = field(default_factory=list)
    block: bool = False
    block_reasons: list[str] = field(default_factory=list)
    caution: bool = False
    caution_reasons: list[str] = field(default_factory=list)
    risk_scale: float = 1.0  # sempre em (0, 1]
    divergence: str | None = None
    rationale: list[str] = field(default_factory=list)
    contingency: dict[str, Any] | None = None  # {"since", "hours", "policy", "sources": [...]}
    macro: dict[str, Any] | None = None  # resumo público da camada macro (janelas, próximos eventos)

    @property
    def would_change(self) -> bool:
        """O que o modo ENFORCE alteraria nesta decisão (usado também em shadow)."""
        return self.block or self.risk_scale < 1.0

    def to_public(self) -> dict[str, Any]:
        return {
            "modo": self.mode, "estado_dados": self.data_state, "nenhum_evento": self.no_events, "alertas": [a.to_public() for a in self.alerts],
            "bloqueia": self.block, "motivos_bloqueio": self.block_reasons, "cautela": self.caution, "motivos_cautela": self.caution_reasons,
            "escala_de_risco": self.risk_scale, "divergencia": self.divergence, "justificativa": self.rationale, "contingencia": self.contingency,
            "macro": self.macro,
        }
