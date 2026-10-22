"""Montagem do runtime de notícias/macro e utilidades para o serviço de trading.

Garantias:
  * a coleta de rede roda em THREAD DE FUNDO (background_fetch=True): o ciclo de trading só recolhe o resultado pronto e nunca espera pela rede;
  * qualquer falha interna vira log/estado da fonte; nada daqui lança para o ciclo de trading;
  * a AUDITORIA é só observação: falha ao gravar um registro não pode alterar uma decisão (ver record_decision)."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import IntelParams, Settings
from app.domain import TokenInfo
from app.http import Transport
from app.intel.classifier import Classifier
from app.intel.collector import FetchBatch, NewsCollector
from app.intel.engine import IntelEngine
from app.intel.macro import DbMacroView, MacroEngine, MacroIngest, MacroStore
from app.intel.models import IntelAssessment
from app.intel.sources import SourceConfig, SourceError, load_sources
from app.intel.store import DbNewsView, IntelStore

log = logging.getLogger("intel")


class _FetchJob(threading.Thread):
    """Busca nas fontes fora do ciclo de trading (somente rede + parsing; sem acesso ao banco)."""

    def __init__(self, collector: NewsCollector, now: datetime, hints: dict[str, bool] | None):
        super().__init__(name="intel-fetch", daemon=True)
        self.collector, self.now, self.hints = collector, now, hints
        self.batch: FetchBatch | None = None
        self.error: BaseException | None = None
        self.abandoned = False
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            self.batch = self.collector.fetch_phase(self.now, self.hints)
        except BaseException as exc:  # noqa: BLE001 - nunca propaga
            self.error = exc
        finally:
            self.finished.set()


@dataclass
class IntelRuntime:
    params: IntelParams
    store: IntelStore
    engine: IntelEngine
    collector: NewsCollector | None
    sources: list[SourceConfig]
    notes: list[str]
    macro_store: MacroStore | None = None
    macro_ingest: MacroIngest | None = None
    audit_failures: int = 0
    last_audit_error: str | None = None
    _job: _FetchJob | None = field(default=None, repr=False)
    _state_loaded: bool = field(default=False, repr=False)
    _last_overall: str | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.params.mode != "off"

    # ------------------------------------------------------------------ coleta
    def refresh(self, now: datetime) -> dict[str, Any] | None:
        """Não bloqueia: dispara a busca em segundo plano e aplica o resultado da rodada ANTERIOR, se já estiver pronto."""
        if not self.active:
            return None
        try:
            if self.macro_ingest is not None:
                self.macro_ingest.reload_if_changed(now)
            if self.collector is None:
                return None
            if not self._state_loaded:
                self.collector.load_state(now)
                self._state_loaded = True
            hints = self._hints(now)
            if not self.params.background_fetch:
                stats = self.collector.refresh_if_due(now, hints)
                if stats is not None:
                    self._after_apply(now)
                return stats
            stats = None
            job = self._job
            if job is not None and not job.finished.is_set() and (now - job.now).total_seconds() > self._watchdog_s():
                # Busca presa (ex.: resolução de DNS pendurada, que os timeouts do httpx não cobrem): abandona a thread (daemon) e permite nova busca.
                job.abandoned = True
                self._job = None
                job = None
                try:
                    self.store.log_event(now, "busca_travada", None, "alerta", f"busca em segundo plano abandonada após {self._watchdog_s():g} s; nova busca será iniciada")
                except Exception:  # noqa: BLE001
                    log.exception("falha ao registrar busca travada")
                log.error("busca de notícias em segundo plano travada: abandonada")
            if job is not None and job.finished.is_set():
                self._job = None
                if job.batch is not None:
                    stats = self.collector.apply_phase(job.batch, now)
                    self._after_apply(now)
                elif job.error is not None:
                    log.error("falha na busca de notícias em segundo plano: %s", type(job.error).__name__)
            if self._job is None and self.collector.due(now):
                self._job = _FetchJob(self.collector, now, hints)
                self._job.start()
            return stats
        except Exception:  # noqa: BLE001 - coleta nunca derruba o ciclo de trading
            log.exception("falha inesperada na coleta de notícias")
            return None

    def _watchdog_s(self) -> float:
        p = self.params
        return max(120.0, 3.0 * (p.refresh_budget_s + p.total_timeout_s))

    def flush(self, now: datetime, timeout: float = 30.0) -> dict[str, Any] | None:
        """Espera a busca em segundo plano terminar e aplica o resultado (usado em testes, scripts e no encerramento)."""
        job = self._job
        if job is not None:
            job.finished.wait(timeout)
        return self.refresh(now)

    def _hints(self, now: datetime) -> dict[str, bool] | None:
        if self.macro_ingest is None or self.collector is None:
            return None
        try:
            return self.macro_ingest.poll_hints(self.sources, now)
        except Exception:  # noqa: BLE001
            log.exception("falha ao calcular necessidade de consulta de valores macro")
            return None

    def _after_apply(self, now: datetime) -> None:
        """Registra mudanças de SAÚDE do módulo (incluindo entrada/saída de contingência) no log de auditoria. Só observação."""
        try:
            h = self.engine.health(now)
            overall = h["geral"]
            if overall != self._last_overall:
                if self._last_overall is not None or overall != "saudavel":
                    kind = {"contingencia": "contingencia_iniciada", "critico": "fonte_critica_com_problema"}.get(overall, "saude_modulo")
                    if self._last_overall == "contingencia" and overall != "contingencia":
                        kind = "contingencia_encerrada"
                    sev = "alerta" if overall in ("contingencia", "critico") else "info"
                    detail = h["mensagem"] + (f" Política aplicada: {self.params.contingency_policy}; unavailable_policy={self.params.unavailable_policy}." if overall == "contingencia" else "")
                    self.store.log_event(now, kind, None, sev, detail)
                self._last_overall = overall
        except Exception:  # noqa: BLE001
            log.exception("falha ao registrar a saúde do módulo")

    # ------------------------------------------------------------------ avaliação
    def assess(self, symbol: str, now: datetime, technical: str = "BUY") -> IntelAssessment:
        """Nunca lança: em caso de erro interno devolve uma avaliação CONSERVADORA (só pode restringir)."""
        try:
            return self.engine.assess(symbol, now, technical)
        except Exception:  # noqa: BLE001
            log.exception("erro na avaliação de notícias")
            block = self.params.unavailable_policy == "block_entries"
            return IntelAssessment(
                mode=self.params.mode, data_state="invalid", no_events=False, block=block,
                block_reasons=["erro interno na camada de notícias (política conservadora)"] if block else [],
                caution=True, caution_reasons=["erro interno na camada de notícias (cautela aplicada por segurança)"],
                risk_scale=max(1e-6, min(1.0, self.params.caution_risk_multiplier)),
                rationale=["erro interno na camada de notícias; ver logs"],
            )

    # ------------------------------------------------------------------ auditoria (SÓ OBSERVAÇÃO)
    def note_audit_failure(self, what: str) -> None:
        self.audit_failures += 1
        self.last_audit_error = what
        log.error("AUDITORIA DE NOTÍCIAS FALHOU (%s). A decisão de trading NÃO foi afetada. Total de falhas: %d", what, self.audit_failures)

    def record_decision(self, d: dict[str, Any] | Callable[[], dict[str, Any]]) -> int | None:
        """Grava a decisão da camada de notícias. Se a gravação falhar (disco cheio, permissão, banco travado, etc.), NÃO propaga:
        a falha é contada e a decisão de trading segue exatamente como seguiria com a auditoria funcionando."""
        try:
            return self.store.add_decision(d() if callable(d) else d)  # a montagem do registro também fica dentro do try
        except BaseException as exc:  # noqa: BLE001 - inclui OSError/sqlite3.Error/qualquer falha de persistência
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.note_audit_failure(f"decisão: {type(exc).__name__}")
            return None

    def link_position(self, decision_id: int, position_id: str) -> None:
        try:
            self.store.link_position(decision_id, position_id)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.note_audit_failure(f"vínculo com posição: {type(exc).__name__}")


def build_intel(s: Settings, conn: sqlite3.Connection, tokens: dict[str, TokenInfo], transport: Transport | None = None,
                transport_factory: Callable[[], Transport] | None = None) -> IntelRuntime | None:
    """None se o modo for `off`. Nunca solicita credenciais; a coleta usa apenas fontes públicas do catálogo."""
    p = s.intel
    if p.mode == "off":
        return None
    notes: list[str] = []
    try:
        sources = load_sources(p.sources_file)
    except SourceError as exc:
        sources = []
        notes.append(f"catálogo de fontes indisponível: {exc}")
    store = IntelStore(conn)
    view = DbNewsView(store, sources, p)
    macro_store = macro_ingest = macro_engine = None
    if p.macro_enabled:
        macro_store = MacroStore(conn)
        macro_ingest = MacroIngest(macro_store, p)
        macro_engine = MacroEngine(p, DbMacroView(macro_store))
        try:
            n = macro_ingest.load_calendar_file(datetime.now(timezone.utc))
            if n == 0 and not Path(p.macro_calendar_file).exists():
                notes.append(f"calendário macro não encontrado ({p.macro_calendar_file}): sem janelas de risco macro até que ele exista")
        except (ValueError, OSError) as exc:
            notes.append(f"calendário macro inválido: {exc}")
    engine = IntelEngine(p, view, macro_engine)
    collector = None
    if sources:
        try:
            if transport is None:
                if transport_factory is not None:
                    transport = transport_factory()
                else:
                    from app.http import HttpxTransport

                    transport = HttpxTransport()
            clf = Classifier([("SOL", ())] + [(t.symbol, t.aliases) for t in tokens.values()], p)
            collector = NewsCollector(p, sources, transport, store, clf, macro=macro_ingest)
        except ImportError:
            notes.append("httpx ausente: coleta de notícias desabilitada")
    return IntelRuntime(p, store, engine, collector, sources, notes, macro_store, macro_ingest)
