"""Persistência (SQLite) e visões de leitura das notícias. Recebe uma conexão sqlite3 (escrita pelo robô; somente leitura no painel)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.config import IntelParams
from app.intel.models import NewsRecord, SourceState, parse_iso
from app.intel.sources import SourceConfig

HARD_FAILURES = 3  # falhas consecutivas que caracterizam indisponibilidade (1–2 falhas com dados ainda frescos são toleradas)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


class NewsView(Protocol):
    def records(self, now: datetime) -> list[NewsRecord]: ...
    def source_states(self, now: datetime) -> list[SourceState]: ...


_INSERT = ("INSERT OR IGNORE INTO news_items(id,source_id,source_name,title,summary,url,published_at,collected_at,first_seen,assets,category,polarity,"
           "certainty,relevance,reliability,impact,flags,reasons,resolution,duplicate_of,event_key,event_time,nature,environment,severity,status_state,"
           "origin_key,origin_generic,refs,domain,retraction,indicator) VALUES(" + ",".join("?" * 32) + ")")


class IntelStore:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    # ----------------------------------------------------------------- notícias
    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            return list(self.c.execute(sql, args))
        except sqlite3.OperationalError:  # tabela ainda não existe (base criada antes do módulo)
            return []

    def recent(self, since: datetime) -> list[NewsRecord]:
        return [NewsRecord.from_row(r) for r in self._rows("SELECT * FROM news_items WHERE collected_at >= ? ORDER BY collected_at", (_iso(since),))]

    def latest(self, limit: int = 50) -> list[NewsRecord]:
        return [NewsRecord.from_row(r) for r in self._rows("SELECT * FROM news_items ORDER BY COALESCE(published_at, collected_at) DESC LIMIT ?", (max(1, min(limit, 300)),))]

    def insert(self, rec: NewsRecord) -> bool:
        cur = self.c.execute(_INSERT, rec.to_row())
        return cur.rowcount == 1

    def purge(self, before: datetime) -> int:
        return self.c.execute("DELETE FROM news_items WHERE collected_at < ?", (_iso(before),)).rowcount

    # ----------------------------------------------------------------- estado das fontes
    def status_rows(self) -> dict[str, sqlite3.Row]:
        return {r["source_id"]: r for r in self._rows("SELECT * FROM news_source_status")}

    def record_attempt(self, src: SourceConfig, now: datetime, ok: bool, error: str | None, items: int, malformed: int, *, error_kind: str | None = None,
                       http_status: int | None = None, duration_ms: int | None = None, retry_at: datetime | None = None, recovery_min: int = 2,
                       requests: int = 1) -> list[str]:
        """Registra uma tentativa de coleta. Devolve os EVENTOS de saúde ocorridos (para o log de auditoria):
        'fonte_indisponivel' (3ª falha seguida), 'recuperacao_parcial' (voltou, ainda sem confirmação) e 'fonte_recuperada'."""
        prev = self.status_rows().get(src.id)
        pv = (lambda k, d=None: (prev[k] if prev is not None and k in prev.keys() and prev[k] is not None else d))  # noqa: E731
        prev_fails, prev_succ, prev_since = int(pv("consecutive_failures", 0)), int(pv("consecutive_successes", 0)), pv("unavailable_since")
        events: list[str] = []
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        day_requests = (int(pv("day_requests", 0)) if pv("day_key") == today else 0) + max(0, requests)
        if ok:
            fails, last_ok = 0, _iso(now)
            hard_before = prev_fails >= HARD_FAILURES or (prev_since is not None and prev_fails == 0 and prev_succ < recovery_min)
            succ = prev_succ + 1
            if hard_before and succ < recovery_min:
                since = prev_since or _iso(now)
                events.append("recuperacao_parcial")
            else:
                since = None
                if hard_before:
                    events.append("fonte_recuperada")
            backoff, rl_until = 0, None
            err, kind, status = None, None, http_status
        else:
            # falha DURANTE uma recuperação ainda não confirmada: a indisponibilidade volta a ser "firme" (a fonte oscilou)
            fails = HARD_FAILURES if (prev_fails == 0 and prev_since is not None) else prev_fails + 1
            last_ok = pv("last_ok_at")
            succ, since = 0, prev_since or _iso(now)
            if fails == HARD_FAILURES:
                events.append("fonte_indisponivel")
            backoff = int(pv("backoff_level", 0)) + (1 if error_kind == "rate_limit" else 0)
            rl_until = _iso(retry_at) if retry_at is not None else pv("rate_limited_until")
            err, kind, status = (error or "")[:300] or None, error_kind, http_status
        self.c.execute(
            "INSERT INTO news_source_status(source_id,name,last_attempt_at,last_ok_at,consecutive_failures,last_error,last_items,last_malformed,last_error_kind,last_http_status,"
            "last_duration_ms,unavailable_since,consecutive_successes,rate_limited_until,backoff_level,day_key,day_requests) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET name=excluded.name,last_attempt_at=excluded.last_attempt_at,last_ok_at=excluded.last_ok_at,"
            "consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error,last_items=excluded.last_items,last_malformed=excluded.last_malformed,"
            "last_error_kind=excluded.last_error_kind,last_http_status=excluded.last_http_status,last_duration_ms=excluded.last_duration_ms,"
            "unavailable_since=excluded.unavailable_since,consecutive_successes=excluded.consecutive_successes,rate_limited_until=excluded.rate_limited_until,"
            "backoff_level=excluded.backoff_level,day_key=excluded.day_key,day_requests=excluded.day_requests",
            (src.id, src.name, _iso(now), last_ok, fails, err, items, malformed, kind, status, duration_ms, since, succ, rl_until, backoff, today, day_requests))
        return events

    def requests_today(self, source_id: str, now: datetime) -> int:
        r = self.status_rows().get(source_id)
        if r is None or "day_key" not in r.keys():
            return 0
        return int(r["day_requests"] or 0) if r["day_key"] == now.astimezone(timezone.utc).strftime("%Y-%m-%d") else 0

    # ----------------------------------------------------------------- eventos de auditoria (saúde/contingência)
    def log_event(self, ts: datetime, kind: str, source_id: str | None, severity: str, detail: str) -> None:
        self.c.execute("INSERT INTO intel_events(ts,kind,source_id,severity,detail) VALUES(?,?,?,?,?)", (_iso(ts), kind, source_id, severity, detail[:500]))

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"id": r["id"], "ts": r["ts"], "tipo": r["kind"], "fonte": r["source_id"], "gravidade": r["severity"], "detalhe": r["detail"]}
                for r in self._rows("SELECT * FROM intel_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 300)),))]

    def last_event_kind(self, source_id: str, kinds: tuple[str, ...]) -> str | None:
        q = ",".join("?" * len(kinds))
        rows = self._rows(f"SELECT kind FROM intel_events WHERE source_id=? AND kind IN ({q}) ORDER BY id DESC LIMIT 1", (source_id, *kinds))
        return rows[0]["kind"] if rows else None

    # ----------------------------------------------------------------- decisões
    def add_decision(self, d: dict[str, Any]) -> int:
        cur = self.c.execute(
            "INSERT INTO intel_decisions(ts,symbol,mint,candle_ts,technical,mode,data_state,action,would_change,applied,risk_scale,divergence,alerts,rationale,position_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["ts"], d["symbol"], d.get("mint"), d.get("candle_ts"), d["technical"], d["mode"], d["data_state"], d["action"], int(d["would_change"]), int(d["applied"]),
             d["risk_scale"], d.get("divergence"), json.dumps(d.get("alerts", []), ensure_ascii=False), json.dumps(d.get("rationale", []), ensure_ascii=False), d.get("position_id")))
        return int(cur.lastrowid or 0)

    def link_position(self, decision_id: int, position_id: str) -> None:
        self.c.execute("UPDATE intel_decisions SET position_id=? WHERE id=?", (position_id, decision_id))

    def decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for r in self._rows("SELECT * FROM intel_decisions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 300)),)):
            out.append({"id": r["id"], "ts": r["ts"], "simbolo": r["symbol"], "candle": r["candle_ts"], "tecnico": r["technical"], "modo": r["mode"],
                        "estado_dados": r["data_state"], "acao": r["action"], "mudaria": bool(r["would_change"]), "aplicado": bool(r["applied"]),
                        "escala_de_risco": r["risk_scale"], "divergencia": r["divergence"], "alertas": json.loads(r["alerts"] or "[]"),
                        "justificativa": json.loads(r["rationale"] or "[]"), "posicao": r["position_id"]})
        return out

    def affected_decisions(self, since: datetime) -> int:
        """Quantas decisões de entrada foram afetadas por dados indisponíveis/desatualizados/contingência desde `since`."""
        rows = self._rows("SELECT COUNT(*) n FROM intel_decisions WHERE ts >= ? AND data_state IN ('unavailable','stale','invalid','contingency')", (_iso(since),))
        return int(rows[0]["n"]) if rows else 0


def compute_source_states(sources: list[SourceConfig], rows: dict[str, Any], now: datetime, params: IntelParams) -> list[SourceState]:
    """Estado de saúde de CADA fonte. Critérios (todos configuráveis em IntelParams):
      * ok: sucesso recente (≤ stale_after_s) e <3 falhas seguidas e resposta válida;
      * stale (desatualizada): sem sucesso há mais que stale_after_s, porém com <3 falhas seguidas (ex.: robô parado ou coleta pulada);
      * unavailable: ≥3 falhas seguidas (rede/timeout/HTTP/limite de requisições) ou nunca coletou;
      * invalid (resposta inválida): ≥3 falhas seguidas cujo motivo é conteúdo inválido (XML/JSON/HTML inesperado) ou >50% dos itens malformados;
      * recuperação só é confirmada após `recovery_min_successes` sucessos seguidos (até lá a fonte continua marcada como indisponível)."""
    out: list[SourceState] = []
    for s in sources:
        r = rows.get(s.id)
        base = dict(domain=s.domain, critical=s.critical, stale_after_s=s.stale_after_s or params.stale_after_s)
        if not s.enabled:
            out.append(SourceState(s.id, s.name, "disabled", **base))
            continue
        if r is None or not r["last_attempt_at"]:
            out.append(SourceState(s.id, s.name, "unavailable", last_error="sem coleta ainda", **base))
            continue
        keys = set(r.keys())
        g = lambda k, d=None: (r[k] if k in keys and r[k] is not None else d)  # noqa: E731
        stale_s = float(base["stale_after_s"])
        last_ok = parse_iso(r["last_ok_at"])
        age = (now - last_ok).total_seconds() if last_ok else None
        fails, succ = int(r["consecutive_failures"] or 0), int(g("consecutive_successes", 0))
        items, bad = r["last_items"] or 0, r["last_malformed"] or 0
        kind, since = g("last_error_kind"), g("unavailable_since")
        rl = parse_iso(g("rate_limited_until"))
        retry_at = rl.isoformat() if rl and rl > now else None
        recovering = None
        down_since = None
        hard = fails >= HARD_FAILURES
        if last_ok is None:
            state = "invalid" if (hard and kind in ("parse", "invalid")) else "unavailable"
            down_since = since or r["last_attempt_at"]
        elif hard:
            state = "invalid" if kind in ("parse", "invalid") else "unavailable"
            down_since = since or r["last_ok_at"]
        elif fails == 0 and since is not None and succ < params.recovery_min_successes:
            state, recovering = "unavailable", f"{succ}/{params.recovery_min_successes}"
            down_since = since
        elif age is not None and age > stale_s:
            state = "stale"
            down_since = (last_ok + timedelta(seconds=stale_s)).isoformat()
        elif items + bad > 0 and bad / (items + bad) >= 0.5:
            state = "invalid"
            down_since = r["last_ok_at"]
        else:
            state = "ok"
        out.append(SourceState(s.id, s.name, state, r["last_ok_at"], r["last_attempt_at"], r["last_error"], fails, items, bad, error_kind=kind,
                               http_status=g("last_http_status"), duration_ms=g("last_duration_ms"), down_since=down_since if state != "ok" else None,
                               retry_at=retry_at, recovering=recovering, **base))
    return out


class DbNewsView:
    """Visão AO VIVO: só enxerga o que já foi coletado até `now` (e publicado até `now`)."""

    def __init__(self, store: IntelStore, sources: list[SourceConfig], params: IntelParams):
        self.store, self.sources, self.params = store, sources, params

    def records(self, now: datetime) -> list[NewsRecord]:
        horizon = now - timedelta(hours=max(self.params.item_max_age_h, 72.0))
        return [r for r in self.store.recent(horizon) if r.available_dt <= now and (r.published_dt is None or r.published_dt <= now + timedelta(seconds=self.params.future_tolerance_s))]

    def source_states(self, now: datetime) -> list[SourceState]:
        return compute_source_states(self.sources, self.store.status_rows(), now, self.params)


class MemoryNewsView:
    """Visão para REPLAY/backtest: itens com `collected_at` = momento em que teriam estado disponíveis. Nada de futuro vaza."""

    def __init__(self, records: list[NewsRecord], source_ids: list[str] | None = None):
        self._records = sorted(records, key=lambda r: r.collected_dt)
        self._sources = source_ids or sorted({r.source_id for r in records}) or ["arquivo"]

    def records(self, now: datetime) -> list[NewsRecord]:
        return [r for r in self._records if r.available_dt <= now and (r.published_dt is None or r.published_dt <= now)]

    def source_states(self, now: datetime) -> list[SourceState]:
        return [SourceState(s, s, "ok") for s in self._sources]
