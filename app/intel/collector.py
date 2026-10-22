"""Coletor: busca as fontes, valida, deduplica, classifica e persiste. Nenhuma exceção sai daqui (fail-safe).

Duas FASES (para que a rede nunca atrase o ciclo de trading):
  * fetch_phase: só rede e parsing (sem tocar no banco) — pode rodar em thread de fundo;
  * apply_phase: classifica, deduplica e grava (rápido, no thread principal).
`refresh()` junta as duas de forma síncrona (usado em testes, scripts e quando background_fetch=False)."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import IntelParams
from app.http import Transport
from app.intel.classifier import Classifier
from app.intel.dedup import canonical_url, content_fingerprint, item_id, jaccard, title_tokens
from app.intel.models import NewsRecord, RawItem, parse_iso
from app.intel.sources import KINDS_MACRO, SourceConfig, SourceError, fetch_source
from app.intel.store import IntelStore
from app.resilience import CircuitBreaker

log = logging.getLogger("intel.coletor")


@dataclass
class FetchResult:
    src: SourceConfig
    ok: bool
    items: list[RawItem] = field(default_factory=list)
    malformed: int = 0
    payload: Any = None  # macro: dados já analisados (eventos/valores/mercado)
    error: str | None = None
    kind: str | None = None
    http_status: int | None = None
    duration_ms: int = 0
    retry_after_s: float | None = None
    backoff_until: datetime | None = None
    requests: int = 1
    at: datetime | None = None


@dataclass
class FetchBatch:
    results: list[FetchResult] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=lambda: {"orcamento": 0, "circuito": 0, "intervalo": 0, "backoff": 0, "limite_diario": 0, "sem_necessidade": 0})
    at: datetime | None = None
    duration_ms: int = 0


class NewsCollector:
    def __init__(
        self,
        params: IntelParams,
        sources: list[SourceConfig],
        transport: Transport,
        store: IntelStore,
        classifier: Classifier,
        mono: Callable[[], float] = time.monotonic,
        macro: Any = None,
    ):
        self.p, self.sources, self.t, self.store, self.clf, self._mono = params, sources, transport, store, classifier, mono
        self.macro = macro  # MacroIngest | None
        self._breakers = {s.id: CircuitBreaker(3, 600.0, clock=mono) for s in sources}
        self._last_refresh: datetime | None = None
        self._last_fetch: dict[str, float] = {}
        self._backoff_until: dict[str, datetime] = {}
        self._backoff_level: dict[str, int] = {}
        self._requests_today: dict[str, tuple[str, int]] = {}

    # ------------------------------------------------------------------ estado persistido (thread principal)
    def load_state(self, now: datetime) -> None:
        """Restaura backoff de HTTP 429 e contagem diária de requisições do banco (um reinício NÃO pode ignorar um limite já recebido)."""
        try:
            for sid, r in self.store.status_rows().items():
                keys = set(r.keys())
                until = parse_iso(r["rate_limited_until"]) if "rate_limited_until" in keys else None
                if until and until > now:
                    self._backoff_until[sid] = until
                if "backoff_level" in keys and r["backoff_level"]:
                    self._backoff_level[sid] = int(r["backoff_level"])
                if "day_key" in keys and r["day_key"]:
                    self._requests_today[sid] = (r["day_key"], int(r["day_requests"] or 0))
        except Exception:  # noqa: BLE001
            log.exception("não foi possível restaurar o estado das fontes")

    def due(self, now: datetime) -> bool:
        return self._last_refresh is None or (now - self._last_refresh).total_seconds() >= self.p.refresh_interval_s

    def refresh_if_due(self, now: datetime, poll_hints: dict[str, bool] | None = None) -> dict[str, Any] | None:
        if not self.due(now):
            return None
        return self.refresh(now, poll_hints)

    def refresh(self, now: datetime, poll_hints: dict[str, bool] | None = None) -> dict[str, Any]:
        return self.apply_phase(self.fetch_phase(now, poll_hints), now)

    # ------------------------------------------------------------------ fase 1: rede (sem banco)
    def fetch_phase(self, now: datetime, poll_hints: dict[str, bool] | None = None) -> FetchBatch:
        self._last_refresh = now
        batch = FetchBatch(at=now)
        started = self._mono()
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        for src in self.sources:
            if not src.enabled:
                continue
            if self._mono() - started > self.p.refresh_budget_s:
                batch.skipped["orcamento"] += 1  # orçamento de tempo esgotado: tenta na próxima rodada
                continue
            if src.kind == "bls_api" and poll_hints is not None and not poll_hints.get(src.id, False):
                batch.skipped["sem_necessidade"] += 1  # valores só são buscados quando há divulgação recente esperada (poupa o limite diário)
                continue
            if self._backoff_until.get(src.id) and self._backoff_until[src.id] > now:
                batch.skipped["backoff"] += 1  # HTTP 429/Retry-After: respeita a espera (sem tentativas agressivas)
                continue
            br = self._breakers[src.id]
            if br.is_open:
                batch.skipped["circuito"] += 1
                continue
            if src.min_interval_s and self._mono() - self._last_fetch.get(src.id, -1e9) < src.min_interval_s:
                batch.skipped["intervalo"] += 1
                continue
            day, used = self._requests_today.get(src.id, (today, 0))
            used = used if day == today else 0
            need = max(1, len(src.series)) if src.kind == "bls_api" else 1
            if src.daily_cap is not None and used + need > src.daily_cap:
                batch.skipped["limite_diario"] += 1
                continue
            self._last_fetch[src.id] = self._mono()
            t0 = self._mono()
            res = FetchResult(src, False, at=now, requests=need)
            try:
                if src.kind in KINDS_MACRO:
                    from app.intel.macro_sources import fetch_macro

                    res.payload = fetch_macro(self.t, src, self.p, now)
                    res.items = []
                else:
                    res.items, res.malformed = fetch_source(self.t, src, user_agent=self.p.user_agent, timeout=self.p.source_timeout_s, max_bytes=self.p.max_feed_bytes,
                                                            max_items=self.p.max_items_per_source, now=now, total_timeout=self.p.total_timeout_s)
                res.ok = True
                br.success()
                self._backoff_level[src.id] = 0
                self._backoff_until.pop(src.id, None)
            except SourceError as exc:
                br.failure()
                res.error, res.kind, res.http_status, res.retry_after_s = str(exc), exc.kind, exc.http_status, exc.retry_after_s
                if exc.kind == "rate_limit":
                    lvl = self._backoff_level.get(src.id, 0)
                    delay = max(exc.retry_after_s or 0.0, min(self.p.backoff_max_s, self.p.backoff_base_s * (2 ** lvl)))
                    delay = min(delay, self.p.backoff_max_s) if exc.retry_after_s is None else delay
                    self._backoff_level[src.id] = lvl + 1
                    self._backoff_until[src.id] = now + timedelta(seconds=delay)
                    res.backoff_until = self._backoff_until[src.id]
            except Exception as exc:  # noqa: BLE001 - o coletor nunca derruba o robô
                br.failure()
                res.error, res.kind = f"erro inesperado: {type(exc).__name__}", "other"
                log.exception("erro inesperado ao coletar %s", src.id)
            res.duration_ms = int((self._mono() - t0) * 1000)
            self._requests_today[src.id] = (today, used + need)
            batch.results.append(res)
            log.info("fonte %s: %s em %d ms%s", src.id, "ok" if res.ok else f"FALHA({res.kind})", res.duration_ms, f" — {res.error}" if res.error else "")
        batch.duration_ms = int((self._mono() - started) * 1000)
        return batch

    # ------------------------------------------------------------------ fase 2: classificação e gravação
    def apply_phase(self, batch: FetchBatch, now: datetime) -> dict[str, Any]:
        stats: dict[str, Any] = {"novos": 0, "duplicados": 0, "malformados": 0, "fontes_ok": 0, "fontes_falha": 0, "puladas": sum(batch.skipped.values()),
                                 "puladas_detalhe": {k: v for k, v in batch.skipped.items() if v}, "duracao_ms": batch.duration_ms, "eventos_macro": 0}
        for res in batch.results:
            src, when = res.src, res.at or now
            try:
                if res.ok:
                    new = dup = bad = 0
                    if src.kind in KINDS_MACRO:
                        if self.macro is not None:
                            stats["eventos_macro"] += self.macro.ingest(src, res.payload, when)
                    else:
                        new, dup, bad = self._ingest(src, res.items, when)
                    events = self.store.record_attempt(src, when, True, None, len(res.items), res.malformed + bad, duration_ms=res.duration_ms, http_status=200,
                                                       recovery_min=self.p.recovery_min_successes, requests=res.requests)
                    stats["novos"] += new
                    stats["duplicados"] += dup
                    stats["malformados"] += res.malformed + bad
                    stats["fontes_ok"] += 1
                else:
                    events = self.store.record_attempt(src, when, False, res.error, 0, 0, error_kind=res.kind, http_status=res.http_status, duration_ms=res.duration_ms,
                                                       retry_at=res.backoff_until, recovery_min=self.p.recovery_min_successes, requests=res.requests)
                    stats["fontes_falha"] += 1
                    if res.backoff_until is not None:
                        events = [*events, "backoff_limite"]
                self._log_events(src, when, events, res)
            except Exception:  # noqa: BLE001 - falha de gravação de uma fonte não derruba as demais
                log.exception("falha ao gravar o resultado da fonte %s", src.id)
        if self.p.retention_days > 0:
            try:
                self.store.purge(now - timedelta(days=self.p.retention_days))
            except Exception:  # noqa: BLE001
                log.exception("falha ao limpar notícias antigas")
        return stats

    def _log_events(self, src: SourceConfig, when: datetime, events: list[str], res: FetchResult) -> None:
        texts = {"fonte_indisponivel": ("alerta", f"{src.name}: indisponível após 3 falhas seguidas ({res.error})"),
                 "recuperacao_parcial": ("info", f"{src.name}: voltou a responder; recuperação ainda não confirmada"),
                 "fonte_recuperada": ("info", f"{src.name}: recuperada (sucessos consecutivos confirmados)"),
                 "backoff_limite": ("aviso", f"{src.name}: limite de requisições (HTTP {res.http_status}); nova tentativa só após {res.backoff_until.isoformat(timespec='minutes') if res.backoff_until else '?'}")}
        for ev in events:
            sev, detail = texts.get(ev, ("info", ev))
            try:
                self.store.log_event(when, ev, src.id, sev, detail)
            except Exception:  # noqa: BLE001 - log de eventos é só observação
                log.exception("falha ao gravar evento %s", ev)

    # ------------------------------------------------------------------ processamento
    def _ingest(self, src: SourceConfig, items: list, now: datetime) -> tuple[int, int, int]:
        recent = self.store.recent(now - timedelta(hours=self.p.dedup_window_h))
        by_id = {r.id: r for r in recent}
        by_url: dict[str, list[NewsRecord]] = {}
        by_event: dict[str, list[NewsRecord]] = {}
        for r in recent:
            by_url.setdefault(canonical_url(r.url), []).append(r)
            if r.event_key:
                by_event.setdefault(r.event_key, []).append(r)
        toks = [(r, title_tokens(r.title)) for r in recent]
        url_counts = Counter(canonical_url(i.url) for i in items)
        structured = src.status_format == "statuspage"
        new = dup = bad = 0
        for raw in items:
            try:
                rec = self.clf.classify(raw, src, now)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            canon = canonical_url(rec.url)
            if url_counts[canon] > 1:
                # Sem identificador confiável (o mesmo link repetido para vários itens do feed): estratégia CONSERVADORA — cada item distinto
                # (por título + data + conteúdo) é tratado como evento distinto; nada é descartado por coincidência de URL.
                rec.event_key = None
                rec.id = item_id(rec.url, rec.title, content_fingerprint(rec.summary, rec.status_state) + "|" + (rec.published_at or ""))
                url_reliable = False
            else:
                url_reliable = True
            if rec.id in by_id:
                dup += 1
                continue
            pub = rec.published_dt
            # data impossível (futuro) / antiga demais
            if pub is not None and pub > now + timedelta(seconds=self.p.future_tolerance_s):
                # Data impossível (relógio adiantado/erro da fonte): NÃO é gravado — assim, quando o relógio alcançar a data, o item é aceito normalmente
                # numa próxima coleta (marcar "para sempre" ignoraria um incidente oficial real). Conta como malformado.
                log.warning("item de %s com data no futuro (%s) ignorado nesta rodada", src.id, rec.published_at)
                bad += 1
                continue
            elif pub is not None and now - pub > timedelta(hours=self.p.item_max_age_h):
                rec.flags.append("antiga")
                rec.reasons.append(f"publicada há mais de {self.p.item_max_age_h:g} h")
            if structured and rec.event_key:
                # Página de status: o MESMO incidente (mesma URL) muda de estado ao longo do tempo. Cada mudança é uma NOVA VERSÃO (guardada),
                # nunca descartada por ter o mesmo título. Títulos repetidos entre incidentes diferentes NÃO são duplicatas.
                versions = by_event.get(rec.event_key, [])
                if versions:
                    last = max(versions, key=lambda r: (r.collected_dt, r.event_dt or r.collected_dt))
                    rec.flags.append("atualizacao")
                    rec.reasons.append(f"nova versão do incidente (estado anterior: {last.status_state or '?'} → {rec.status_state or '?'})")
                    worst = max(v.severity for v in versions)
                    if rec.category == "incidente_rede" and rec.environment == last.environment and (worst > rec.severity or max(v.impact for v in versions) > rec.impact):
                        # a gravidade já observada neste incidente NÃO diminui só porque a atualização seguinte é mais curta (conservador);
                        # o fim do incidente é sinalizado pelo ESTADO "resolved", não por texto mais ameno
                        rec.severity = max(rec.severity, worst)
                        rec.impact = max(rec.impact, max(v.impact for v in versions))
                        rec.reasons.append("gravidade mantida no maior valor já observado para este incidente")
                    if not (pub and last.published_dt and pub > last.published_dt + timedelta(seconds=1)):
                        rec.event_time = now.astimezone(timezone.utc).isoformat()  # a fonte não atualizou a data: a mudança só é conhecida a partir de agora
            else:
                same_url = by_url.get(canon, []) if url_reliable else []
                if same_url:
                    old_pub = min((r.published_dt for r in same_url if r.published_dt), default=None)
                    if old_pub and pub and pub - old_pub > timedelta(hours=1):
                        rec.flags.append("reapresentada")
                        rec.reasons.append("mesmo link já visto com data anterior (possível republicação)")
                    else:
                        dup += 1
                        continue
                # títulos quase iguais (só entre itens publicados próximos no tempo: o mesmo título meses depois NÃO é duplicata)
                t_new = title_tokens(rec.title)
                for other, t_old in toks:
                    if jaccard(t_new, t_old) < self.p.near_dup_jaccard:
                        continue
                    if other.source_id == rec.source_id:
                        gap_ok = not (pub and other.published_dt) or abs(pub - other.published_dt) <= timedelta(hours=self.p.near_dup_max_gap_h)
                        if gap_ok:
                            rec.duplicate_of = other.id
                            rec.flags.append("duplicada")
                            rec.reasons.append("título quase idêntico na mesma fonte")
                    elif other.published_dt and pub and pub - other.published_dt > timedelta(hours=self.p.resurface_lag_h):
                        rec.flags.append("similar_antigo")
                        rec.reasons.append("título muito parecido com notícia mais antiga de outra fonte (não conta como corroboração)")
                    break
            if self.store.insert(rec):
                by_id[rec.id] = rec
                toks.append((rec, title_tokens(rec.title)))
                by_url.setdefault(canon, []).append(rec)
                if rec.event_key:
                    by_event.setdefault(rec.event_key, []).append(rec)
                new += 1
        return new, dup, bad
