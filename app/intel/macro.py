"""Inteligência macroeconômica dos EUA: calendário de eventos, valores divulgados, condições de mercado e janelas de risco.

Separação (ver docs/NEWS_INTEL.md): este módulo NÃO executa nada. Ele só devolve restrições conservadoras (risk_scale em (0,1] e, se o operador
escolher explicitamente, bloqueio de NOVAS entradas dentro da janela de uma divulgação de alta importância).

Regras de honestidade dos dados:
  * evento FUTURO = só "risco de evento futuro": o resultado é DESCONHECIDO e nunca é inferido;
  * expectativa (consenso), valor anterior e revisão só existem se a fonte os FORNECEU (BLS não publica consenso: forecast fica vazio);
  * cada valor/revisão guarda `observed_at` (instante em que ficou DISPONÍVEL para o robô): no replay/backtest, só vale a partir daí;
  * horários dos EUA usam a regra de horário de verão (app.intel.ustime), nunca offsets fixos."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from app.config import IntelParams
from app.intel.models import ActiveAlert, parse_iso
from app.intel.ustime import et_to_utc, to_utc

log = logging.getLogger("intel.macro")

# código -> (nome, importância 1-3, indicadores com valor numérico acompanhados)
INDICATORS: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "FOMC_DECISION": ("Decisão de juros do FOMC (federal funds rate)", 3, ()),
    "FOMC_MINUTES": ("Ata do FOMC", 2, ()),
    "FED_PRESSER": ("Coletiva de imprensa do Presidente do Fed", 3, ()),
    "FED_SPEECH": ("Discurso/entrevista de dirigente do Fed", 2, ()),
    "EMPLOYMENT_SITUATION": ("Relatório de emprego (Nonfarm Payrolls e taxa de desemprego)", 3, ("NFP", "UNEMPLOYMENT")),
    "JOBLESS_CLAIMS": ("Pedidos de seguro-desemprego (Initial/Continuing Jobless Claims)", 2, ("JOBLESS_CLAIMS",)),
    "JOLTS": ("JOLTS (vagas de emprego e desligamentos)", 2, ("JOLTS",)),
    "CPI": ("CPI (inflação ao consumidor; inclui Core CPI)", 3, ("CPI", "CORE_CPI")),
    "PPI": ("PPI (inflação ao produtor)", 2, ("PPI",)),
    "PCE": ("PCE / Core PCE (despesas de consumo pessoal)", 3, ("PCE", "CORE_PCE")),
}
VALUE_UNITS = {"NFP": "mil empregos (variação mensal)", "UNEMPLOYMENT": "%", "CPI": "% a/a", "CORE_CPI": "% a/a", "PPI": "% a/a", "JOLTS": "mil vagas",
               "JOBLESS_CLAIMS": "mil pedidos", "PCE": "% a/a", "CORE_PCE": "% a/a"}
PENDING_MACRO_SOURCES = [
    "S&P 500 e Nasdaq: sem fonte gratuita, sem chave e com termos de uso confirmados (FRED exige chave; Stooq/Yahoo: termos não confirmados). NÃO integrados.",
    "Índice do dólar (DXY): sem fonte adequada e com permissão de uso confirmada. NÃO integrado.",
    "Jobless Claims (valores) e PCE (valores): sem API oficial gratuita sem chave integrada (DOL publica PDF; BEA/FRED exigem chave). O CALENDÁRIO existe; os valores ficam N/D.",
    "Consenso/expectativa de mercado: nenhuma fonte oficial gratuita. A expectativa fica N/D (nunca inventada).",
    "Valor da taxa-alvo do FOMC: não estruturado (texto do comunicado). Só a decisão/horário são acompanhados.",
]
CALENDAR_FILE_SOURCE = "calendario_manual"  # eventos vindos de config/macro_calendar.json (o arquivo é a fonte da verdade desses eventos)
CONFIDENCES = ("oficial", "estimado")  # horário confirmado por fonte oficial | estimado (recorrência/calendário manual a confirmar)


# --------------------------------------------------------------------------------------------------------- modelos
@dataclass
class MacroEvent:
    id: str
    kind: str
    name: str
    source_id: str
    source_url: str
    scheduled_utc: datetime
    tz_original: str
    local_time: str  # "AAAA-MM-DD HH:MM" no fuso original
    region: str = "US"
    importance: int = 2
    indicators: tuple[str, ...] = ()
    confidence: str = "estimado"
    status: str = "scheduled"  # scheduled | cancelled
    first_seen: datetime | None = None
    known_at: datetime | None = None  # quando o agendamento passou a ser conhecido (None = divulgado com antecedência, conhecido desde sempre)

    def to_public(self, now: datetime | None = None) -> dict[str, Any]:
        d = {"id": self.id, "tipo": self.kind, "nome": self.name, "fonte": self.source_id, "link": self.source_url, "horario_utc": self.scheduled_utc.isoformat(),
             "fuso_original": self.tz_original, "horario_local": self.local_time, "regiao": self.region, "importancia": self.importance,
             "indicadores": list(self.indicators), "confianca_do_horario": self.confidence, "status": self.status}
        if now is not None:
            d["faltam_horas"] = round((self.scheduled_utc - now).total_seconds() / 3600, 2)
        return d


@dataclass
class MacroValue:
    indicator: str
    period: str
    observed_at: datetime  # instante em que o valor ficou DISPONÍVEL
    actual: float
    forecast: float | None = None  # só se a fonte fornecer (BLS não fornece)
    previous: float | None = None
    revised_previous: float | None = None  # o valor anterior foi REVISADO nesta divulgação (valor novo do período anterior)
    unit: str = ""
    source_id: str = ""
    is_revision: bool = False  # revisão do próprio período
    note: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"indicador": self.indicator, "periodo": self.period, "divulgado_em": self.observed_at.isoformat(), "valor_divulgado": self.actual, "expectativa": self.forecast,
                "anterior": self.previous, "anterior_revisado": self.revised_previous, "unidade": self.unit, "fonte": self.source_id, "revisao": self.is_revision, "nota": self.note,
                "surpresa": (self.actual - self.forecast) if self.forecast is not None else None}


@dataclass
class MacroMarket:
    indicator: str  # UST_2Y | UST_10Y
    obs_date: str  # AAAA-MM-DD (data de referência do dado)
    value: float
    prev_value: float | None
    change_bps: float | None
    observed_at: datetime  # disponível a partir daqui
    source_id: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"indicador": self.indicator, "data": self.obs_date, "valor": self.value, "anterior": self.prev_value, "variacao_bps": self.change_bps,
                "disponivel_em": self.observed_at.isoformat(), "fonte": self.source_id}


def event_id(kind: str, scheduled_utc: datetime) -> str:
    return hashlib.sha256(f"{kind}|{scheduled_utc.astimezone(timezone.utc).isoformat()}".encode()).hexdigest()[:16]


def make_event(kind: str, local_date: str, local_time: str, tz: str, *, source_id: str, source_url: str = "", confidence: str = "estimado", importance: int | None = None,
               name: str | None = None, known_at: datetime | None = None, fold: int = 0) -> MacroEvent:
    """Cria um evento a partir de data/hora LOCAIS (com fuso). Lança ValueError se o horário local não existir (salto de horário de verão)."""
    if kind not in INDICATORS:
        raise ValueError(f"tipo de evento desconhecido: {kind}")
    if confidence not in CONFIDENCES:
        raise ValueError("confidence deve ser oficial ou estimado")
    d = date.fromisoformat(local_date)
    hh, mm = (int(x) for x in local_time.split(":"))
    local = datetime(d.year, d.month, d.day, hh, mm)
    utc = to_utc(local, tz, fold)
    nm, imp, inds = INDICATORS[kind]
    return MacroEvent(event_id(kind, utc), kind, name or nm, source_id, source_url, utc, tz, f"{local_date} {local_time}", "US", importance if importance is not None else imp,
                      inds, confidence, "scheduled", None, known_at)


# --------------------------------------------------------------------------------------------------------- calendário (arquivo do operador)
def load_calendar(path: str | Path, max_days: int = 800) -> list[MacroEvent]:
    """Lê config/macro_calendar.json. Formato:
      {"events":[{"kind":"FOMC_DECISION","date":"2026-10-28","time":"14:00","tz":"America/New_York","confidence":"estimado","source":"..."}],
       "recurring":[{"kind":"JOBLESS_CLAIMS","weekday":3,"time":"08:30","tz":"America/New_York","from":"2026-10-01","to":"2026-12-31"}]}
    Entradas inválidas lançam ValueError (fail-closed: nenhum evento duvidoso entra silenciosamente)."""
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8-sig"))
    out: dict[str, MacroEvent] = {}
    for e in raw.get("events", []):
        try:
            ev = make_event(e["kind"], e["date"], e["time"], e.get("tz", "America/New_York"), source_id=CALENDAR_FILE_SOURCE,
                            source_url=str(e.get("source", ""))[:200], confidence=e.get("confidence", "estimado"), importance=e.get("importance"), name=e.get("name"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"evento de calendário inválido {e!r}: {exc}") from exc
        out[ev.id] = ev
    for r in raw.get("recurring", []):
        try:
            d0, d1 = date.fromisoformat(r["from"]), date.fromisoformat(r["to"])
            wd = int(r["weekday"])
            exclude = {date.fromisoformat(x) for x in r.get("exclude", [])}  # feriados em que a divulgação muda de dia (cadastre o dia real em "events")
            if not (0 <= wd <= 6) or (d1 - d0).days > max_days or d1 < d0:
                raise ValueError("intervalo/dia da semana inválido")
            d = d0
            while d <= d1:
                if d.weekday() == wd and d not in exclude:
                    try:
                        ev = make_event(r["kind"], d.isoformat(), r["time"], r.get("tz", "America/New_York"), source_id=CALENDAR_FILE_SOURCE,
                                        source_url=str(r.get("source", ""))[:200], confidence="estimado", importance=r.get("importance"), name=r.get("name"))
                        out[ev.id] = ev
                    except ValueError:
                        pass  # horário inexistente nesse dia (salto de horário de verão): ignora a ocorrência
                d += timedelta(days=1)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"recorrência de calendário inválida {r!r}: {exc}") from exc
    return sorted(out.values(), key=lambda e: e.scheduled_utc)


# --------------------------------------------------------------------------------------------------------- persistência
class MacroStore:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            return list(self.c.execute(sql, args))
        except sqlite3.OperationalError:
            return []

    # ---- transações (a conexão do robô é autocommit; operações compostas precisam ser atômicas)
    def _begin(self) -> bool:
        if self.c.in_transaction:
            return False
        self.c.execute("BEGIN IMMEDIATE")
        return True

    def replace_events_from(self, source_id: str, events: list["MacroEvent"], now: datetime, only_future_from: datetime | None = None) -> int:
        """O conjunto atual de eventos de `source_id` SUBSTITUI o anterior: horário corrigido, evento removido/cancelado na fonte desaparece
        (sem janelas fantasmas). Com `only_future_from`, só eventos futuros daquela fonte são removidos (o histórico passado é preservado)."""
        own = self._begin()
        try:
            keep = {e.id for e in events}
            q = "SELECT id FROM macro_events WHERE source_id=?" + (" AND scheduled_utc >= ?" if only_future_from else "")
            args: tuple = (source_id, only_future_from.isoformat()) if only_future_from else (source_id,)
            removed = 0
            for r in self._rows(q, args):
                if r["id"] not in keep:
                    self.c.execute("DELETE FROM macro_events WHERE id=?", (r["id"],))
                    removed += 1
            changed = sum(int(self.upsert_event(e, now)) for e in events)
            if own:
                self.c.execute("COMMIT")
            return changed + removed
        except BaseException:
            if own:
                self.c.execute("ROLLBACK")
            raise

    # ---- eventos
    def upsert_event(self, ev: MacroEvent, now: datetime) -> bool:
        """Insere/atualiza. Um evento OFICIAL substitui um ESTIMADO do mesmo tipo e do mesmo dia local. Retorna True se algo mudou."""
        own = self._begin()
        try:
            return self._upsert(ev, now)
        except BaseException:
            if own:
                self.c.execute("ROLLBACK")
            raise
        finally:
            if own and self.c.in_transaction:
                self.c.execute("COMMIT")

    def _upsert(self, ev: MacroEvent, now: datetime) -> bool:
        if ev.confidence == "oficial":
            self.c.execute("DELETE FROM macro_events WHERE kind=? AND confidence='estimado' AND substr(local_time,1,10)=?", (ev.kind, ev.local_time[:10]))
        elif self._rows("SELECT 1 FROM macro_events WHERE kind=? AND confidence='oficial' AND substr(local_time,1,10)=?", (ev.kind, ev.local_time[:10])):
            return False  # já existe horário oficial neste dia: o estimado não entra
        cur = self.c.execute(
            "INSERT INTO macro_events(id,kind,name,source_id,source_url,scheduled_utc,tz_original,local_time,region,importance,indicators,confidence,status,first_seen,known_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_url=excluded.source_url,importance=excluded.importance,"
            "confidence=excluded.confidence,status=excluded.status WHERE macro_events.confidence!=excluded.confidence OR macro_events.status!=excluded.status "
            "OR macro_events.importance!=excluded.importance OR macro_events.name!=excluded.name",
            (ev.id, ev.kind, ev.name, ev.source_id, ev.source_url, ev.scheduled_utc.isoformat(), ev.tz_original, ev.local_time, ev.region, ev.importance,
             json.dumps(list(ev.indicators)), ev.confidence, ev.status, (ev.first_seen or now).isoformat(), ev.known_at.isoformat() if ev.known_at else None))
        return cur.rowcount > 0

    def events(self) -> list[MacroEvent]:
        out = []
        for r in self._rows("SELECT * FROM macro_events ORDER BY scheduled_utc"):
            out.append(MacroEvent(r["id"], r["kind"], r["name"], r["source_id"], r["source_url"] or "", parse_iso(r["scheduled_utc"]) or datetime.now(timezone.utc), r["tz_original"],
                                  r["local_time"], r["region"], int(r["importance"] or 2), tuple(json.loads(r["indicators"] or "[]")), r["confidence"], r["status"],
                                  parse_iso(r["first_seen"]), parse_iso(r["known_at"])))
        return out

    # ---- valores
    def latest_value(self, indicator: str, period: str | None = None) -> MacroValue | None:
        rows = self._rows("SELECT * FROM macro_values WHERE indicator=? " + ("AND period=? " if period else "") + "ORDER BY observed_at DESC, id DESC LIMIT 1",
                          (indicator, period) if period else (indicator,))
        return self._value(rows[0]) if rows else None

    @staticmethod
    def _value(r: sqlite3.Row) -> MacroValue:
        return MacroValue(r["indicator"], r["period"], parse_iso(r["observed_at"]) or datetime.now(timezone.utc), r["actual"], r["forecast"], r["previous"], r["revised_previous"],
                          r["unit"] or "", r["source_id"] or "", bool(r["is_revision"]), r["note"] or "")

    def values(self) -> list[MacroValue]:
        return [self._value(r) for r in self._rows("SELECT * FROM macro_values ORDER BY observed_at, id")]

    def insert_value(self, v: MacroValue) -> bool:
        """Grava um valor. Se o período já existe com valor diferente => REVISÃO (nova linha, `is_revision`). Se o valor anterior difere do que foi
        divulgado para o período anterior => `revised_previous`. Nada é inventado: previsão e revisão só existem se os dados mostrarem."""
        same = self.latest_value(v.indicator, v.period)
        if same is not None:
            if abs(same.actual - v.actual) < 1e-9 and (same.previous == v.previous or (same.previous is not None and v.previous is not None and abs(same.previous - v.previous) < 1e-9)):
                return False
            v.is_revision = True
            v.note = (v.note + " " if v.note else "") + f"revisão do período {v.period}: {same.actual:g} → {v.actual:g}"
        else:
            prev_period = _previous_period(v.period)
            old = self.latest_value(v.indicator, prev_period) if prev_period else None
            if old is not None and v.previous is not None and abs(old.actual - v.previous) > 1e-9:
                v.revised_previous = v.previous
                v.note = (v.note + " " if v.note else "") + f"valor anterior revisado: {old.actual:g} → {v.previous:g}"
        self.c.execute("INSERT INTO macro_values(indicator,period,observed_at,actual,forecast,previous,revised_previous,unit,source_id,is_revision,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (v.indicator, v.period, v.observed_at.isoformat(), v.actual, v.forecast, v.previous, v.revised_previous, v.unit, v.source_id, int(v.is_revision), v.note))
        return True

    # ---- mercado
    def add_market(self, m: MacroMarket) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO macro_market(indicator,obs_date,value,prev_value,change_bps,observed_at,source_id) VALUES(?,?,?,?,?,?,?)",
                             (m.indicator, m.obs_date, m.value, m.prev_value, m.change_bps, m.observed_at.isoformat(), m.source_id))
        return cur.rowcount == 1

    def market(self) -> list[MacroMarket]:
        return [MacroMarket(r["indicator"], r["obs_date"], r["value"], r["prev_value"], r["change_bps"], parse_iso(r["observed_at"]) or datetime.now(timezone.utc), r["source_id"] or "")
                for r in self._rows("SELECT * FROM macro_market ORDER BY obs_date, id")]


def _previous_period(period: str) -> str | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"


# --------------------------------------------------------------------------------------------------------- visões (sem vazamento de futuro)
class MacroView(Protocol):
    def events(self, now: datetime) -> list[MacroEvent]: ...
    def values(self, now: datetime) -> list[MacroValue]: ...
    def market(self, now: datetime) -> list[MacroMarket]: ...


def _latest_by(items: list[Any], key: Any) -> list[Any]:
    best: dict[Any, Any] = {}
    for it in sorted(items, key=lambda x: x.observed_at):
        best[key(it)] = it
    return list(best.values())


class MemoryMacroView:
    """REPLAY/backtest: o agendamento só vale a partir de `known_at` (ausente = conhecido com antecedência) e valores/revisões/mercado só a
    partir de `observed_at` (momento em que ficaram DISPONÍVEIS). Nada de futuro vaza."""

    def __init__(self, events: list[MacroEvent] | None = None, values: list[MacroValue] | None = None, market: list[MacroMarket] | None = None):
        self._events, self._values, self._market = events or [], values or [], market or []

    def events(self, now: datetime) -> list[MacroEvent]:
        return [e for e in self._events if e.known_at is None or e.known_at <= now]

    def values(self, now: datetime) -> list[MacroValue]:
        return _latest_by([v for v in self._values if v.observed_at <= now], lambda v: (v.indicator, v.period))

    def market(self, now: datetime) -> list[MacroMarket]:
        return _latest_by([m for m in self._market if m.observed_at <= now], lambda m: m.indicator)


class DbMacroView:
    def __init__(self, store: MacroStore):
        self.store = store

    def events(self, now: datetime) -> list[MacroEvent]:
        return [e for e in self.store.events() if e.known_at is None or e.known_at <= now]

    def values(self, now: datetime) -> list[MacroValue]:
        return _latest_by([v for v in self.store.values() if v.observed_at <= now], lambda v: (v.indicator, v.period))

    def market(self, now: datetime) -> list[MacroMarket]:
        return _latest_by([m for m in self.store.market() if m.observed_at <= now], lambda m: m.indicator)


# --------------------------------------------------------------------------------------------------------- motor
@dataclass
class MacroAssessment:
    risk_scale: float = 1.0
    block: bool = False
    reasons: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    alerts: list[ActiveAlert] = field(default_factory=list)
    windows: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[dict[str, Any]] = field(default_factory=list)
    released: list[dict[str, Any]] = field(default_factory=list)
    market: list[dict[str, Any]] = field(default_factory=list)
    calendar_empty: bool = False

    def to_public(self) -> dict[str, Any]:
        return {"escala_de_risco": self.risk_scale, "bloqueia": self.block, "motivos": self.reasons, "janelas_ativas": self.windows, "proximos": self.upcoming,
                "divulgados": self.released, "mercado": self.market, "calendario_vazio": self.calendar_empty,
                "aviso": "Evento futuro = apenas risco de volatilidade; o resultado é desconhecido. Valores divulgados nunca são interpretados como bons/ruins para cripto."}


class MacroEngine:
    """Só RESTRINGE: janela de risco antes/depois de divulgações relevantes. Nunca interpreta o valor divulgado como positivo/negativo."""

    def __init__(self, params: IntelParams, view: MacroView):
        self.p, self.view = params, view

    def _window(self, importance: int) -> tuple[float, float]:
        if importance >= 3:
            return self.p.macro_high_pre_s, self.p.macro_high_post_s
        if importance == 2:
            return self.p.macro_medium_pre_s, self.p.macro_medium_post_s
        return 0.0, 0.0

    def assess(self, now: datetime) -> MacroAssessment:
        out = MacroAssessment()
        events = [e for e in self.view.events(now) if e.status != "cancelled"]
        values = self.view.values(now)
        out.calendar_empty = not events
        look = now + timedelta(hours=self.p.macro_lookahead_h)
        for e in sorted(events, key=lambda x: x.scheduled_utc):
            pre, post = self._window(e.importance)
            start, end = e.scheduled_utc - timedelta(seconds=pre), e.scheduled_utc + timedelta(seconds=post)
            local = f"{e.local_time} {e.tz_original}"
            if e.scheduled_utc >= now and e.scheduled_utc <= look:
                out.upcoming.append(e.to_public(now) | {"janela_inicio_utc": start.isoformat(), "janela_fim_utc": end.isoformat(), "resultado": "desconhecido (evento futuro)"})
            if pre or post:
                if start <= now <= end and e.importance >= 2:
                    phase = "antes" if now < e.scheduled_utc else "depois"
                    mins = abs((e.scheduled_utc - now).total_seconds()) / 60
                    got = next((v for v in values if v.indicator in e.indicators and v.observed_at >= e.scheduled_utc - timedelta(hours=1)), None)
                    if phase == "antes":
                        state = f"evento FUTURO em {mins:.0f} min: o resultado é DESCONHECIDO; a janela cobre apenas o risco de volatilidade"
                    elif got is not None:
                        state = f"divulgado há {mins:.0f} min; valor disponível desde {got.observed_at.isoformat(timespec='minutes')} (não interpretado como bom/ruim)"
                    elif e.indicators:
                        state = f"divulgação prevista há {mins:.0f} min; valor ainda NÃO disponível para o robô"
                    else:
                        state = f"evento ocorreu há {mins:.0f} min"
                    note = "" if e.confidence == "oficial" else " [horário ESTIMADO: confirme no calendário oficial]"
                    reason = f"{e.name} — {local} ({e.scheduled_utc.isoformat(timespec='minutes')}): {state}{note}"
                    w = {"evento": e.name, "tipo": e.kind, "fase": phase, "importancia": e.importance, "horario_utc": e.scheduled_utc.isoformat(), "horario_local": local,
                         "inicio_utc": start.isoformat(), "fim_utc": end.isoformat(), "confianca_do_horario": e.confidence, "motivo": reason}
                    out.windows.append(w)
                    high = e.importance >= 3
                    blocks = high and self.p.macro_high_policy == "block"
                    out.alerts.append(ActiveAlert("macro_calendario", "MACRO", 3 if high else 2, "confirmed" if e.confidence == "oficial" else "claim", 0.9 if e.confidence == "oficial" else 0.6,
                                                  1, f"{e.name} ({phase} da divulgação)", e.source_url, None, end.isoformat(), blocks, reason, domain="macro_us", independence="n/a"))
                    if high:
                        out.risk_scale = min(out.risk_scale, self.p.macro_window_risk_multiplier)
                        out.reasons.append(reason)
                        if blocks:
                            out.block = True
                            out.block_reasons.append(reason + " (política macro_high_policy=block)")
        # valores divulgados recentes (informativo)
        for v in sorted(values, key=lambda x: x.observed_at, reverse=True)[:12]:
            out.released.append(v.to_public() | {"tem_expectativa": v.forecast is not None})
        # condições de mercado (informativo; cautela só se configurada)
        for m in self.view.market(now):
            out.market.append(m.to_public())
            thr = self.p.macro_yield_move_bps
            if thr > 0 and m.change_bps is not None and abs(m.change_bps) >= thr:
                reason = f"rendimento {m.indicator} variou {m.change_bps:+.1f} bps em {m.obs_date} (limite configurado {thr:g} bps)"
                out.alerts.append(ActiveAlert("macro_mercado", "MACRO", 3, "confirmed", 0.8, 1, reason, "", None, (now + timedelta(hours=24)).isoformat(), False, reason, domain="macro_us"))
                out.risk_scale = min(out.risk_scale, self.p.macro_window_risk_multiplier)
                out.reasons.append(reason)
        out.upcoming = out.upcoming[:20]
        return out


# --------------------------------------------------------------------------------------------------------- escrita (ingestão)
class MacroIngest:
    """Lado de ESCRITA: calendário do operador, eventos oficiais (ICS), valores (BLS) e mercado (Tesouro). Roda no thread principal."""

    def __init__(self, store: MacroStore, params: IntelParams):
        self.store, self.p = store, params
        self._cal_mtime: float | None = None

    def load_calendar_file(self, now: datetime) -> int:
        path = Path(self.p.macro_calendar_file)
        if not path.exists():
            self._cal_mtime = None
            return 0
        self._cal_mtime = path.stat().st_mtime
        return self.store.replace_events_from(CALENDAR_FILE_SOURCE, load_calendar(path), now)

    def reload_if_changed(self, now: datetime) -> int:
        path = Path(self.p.macro_calendar_file)
        try:
            mt = path.stat().st_mtime if path.exists() else None
        except OSError:
            return 0
        if mt is None or mt == self._cal_mtime:
            return 0
        try:
            return self.load_calendar_file(now)
        except ValueError:
            log.exception("calendário macro inválido (mantido o anterior)")
            self._cal_mtime = mt
            return 0

    def ingest(self, src: Any, payload: Any, when: datetime) -> int:
        n = 0
        if src.kind == "ics":
            n += self.store.replace_events_from(src.id, list(payload or []), when, only_future_from=when)  # mudanças/cancelamentos do calendário oficial valem; o passado fica
        elif src.kind == "bls_api":
            for v in payload or []:
                n += int(self.store.insert_value(v))
        elif src.kind == "treasury_csv":
            for m in payload or []:
                n += int(self.store.add_market(m))
        return n

    def poll_hints(self, sources: list[Any], now: datetime) -> dict[str, bool]:
        """Quais fontes de VALORES (BLS) valem uma requisição agora: há divulgação esperada nas últimas 6 h sem valor posterior, ou nunca houve valor
        (uma consulta inicial). Poupa o limite diário (BLS v1 sem chave: 25 consultas/dia, informado por terceiros)."""
        hints: dict[str, bool] = {}
        events = self.store.events()
        for s in sources:
            if s.kind != "bls_api":
                continue
            inds = {BLS_INDICATORS.get(x, x) for x in s.series}
            due = False
            for e in events:
                if e.status != "cancelled" and e.indicators and now - timedelta(hours=6) <= e.scheduled_utc <= now:
                    last = [self.store.latest_value(i) for i in e.indicators]
                    if not any(v is not None and v.observed_at >= e.scheduled_utc for v in last):
                        due = True
            if not due and all(self.store.latest_value(i) is None for i in inds):
                due = True
            hints[s.id] = due
        return hints


# identificador de série BLS -> indicador (declarado aqui para evitar import circular com macro_sources)
BLS_INDICATORS = {"LNS14000000": "UNEMPLOYMENT", "CES0000000001": "NFP", "CUUR0000SA0": "CPI", "CUUR0000SA0L1E": "CORE_CPI", "WPSFD4": "PPI", "JTS000000000000000JOL": "JOLTS"}


def load_macro_archive(path: str | Path, params: IntelParams) -> tuple[list[MacroEvent], list[MacroValue], list[MacroMarket]]:
    """Arquivo histórico (JSON Lines) para replay/backtest. Cada linha: {"type":"event"|"value"|"market", ...}.
      event : {"kind","date","time","tz","importance"?,"known_at"?,"confidence"?}
      value : {"indicator","period","actual","previous"?,"forecast"?,"revised_previous"?,"released_at"|"observed_at"}  (sem observed_at: released_at + latência presumida)
      market: {"indicator","obs_date","value","prev_value"?,"observed_at"?}  (sem observed_at: obs_date + hora de publicação presumida, ET)
    Linhas inválidas são descartadas. Disponibilidade nunca precede a divulgação."""
    events: list[MacroEvent] = []
    values: list[MacroValue] = []
    market: list[MacroMarket] = []
    hh, mm = (int(x) for x in params.treasury_publication_et.split(":"))
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            t = d.get("type")
            if t == "event":
                events.append(make_event(d["kind"], d["date"], d["time"], d.get("tz", "America/New_York"), source_id="arquivo", confidence=d.get("confidence", "oficial"),
                                         importance=d.get("importance"), known_at=parse_iso(d["known_at"]) if d.get("known_at") else None))
            elif t == "value":
                rel = parse_iso(d.get("released_at"))
                obs = parse_iso(d.get("observed_at")) or (rel + timedelta(seconds=params.assumed_latency_s) if rel else None)
                if obs is None:
                    continue
                if rel is not None:
                    obs = max(obs, rel)
                values.append(MacroValue(d["indicator"], d["period"], obs, float(d["actual"]), d.get("forecast"), d.get("previous"), d.get("revised_previous"),
                                         VALUE_UNITS.get(d["indicator"], ""), "arquivo", bool(d.get("is_revision", False)), str(d.get("note", ""))[:200]))
            elif t == "market":
                od = date.fromisoformat(d["obs_date"])
                obs = parse_iso(d.get("observed_at")) or et_to_utc(datetime(od.year, od.month, od.day, hh, mm))
                pv = d.get("prev_value")
                market.append(MacroMarket(d["indicator"], d["obs_date"], float(d["value"]), pv, (float(d["value"]) - pv) * 100 if pv is not None else None, obs, "arquivo"))
        except (KeyError, ValueError, TypeError):
            continue
    return events, values, market


__all__ = ["MacroEvent", "MacroValue", "MacroMarket", "MacroStore", "MacroEngine", "MacroIngest", "MacroAssessment", "MemoryMacroView", "DbMacroView", "make_event", "load_calendar",
           "load_macro_archive", "INDICATORS"]
