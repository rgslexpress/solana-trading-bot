"""Fontes macroeconômicas (somente leitura, HTTP GET, sem chave): calendário BLS (iCalendar), valores BLS (API pública v1) e rendimentos do Tesouro (CSV).

ATENÇÃO — estado de validação: nenhuma destas três fontes foi acessada no ambiente de desenvolvimento (rede bloqueada). Os formatos abaixo vêm de
documentação encontrada por busca; os testes usam amostras sintéticas. Valide com `scripts/diagnostico_fontes.py`/o diagnóstico de fontes antes de habilitar.
  * BLS iCalendar: https://www.bls.gov/schedule/news_release/bls.ics  (documentada; formato de DTSTART não confirmado)
  * BLS API v1:    https://api.bls.gov/publicAPI/v1/timeseries/data/<SERIES>  (GET sem chave; limite 25 consultas/dia segundo terceiros; 10 anos por consulta)
  * Tesouro CSV:   URL exata do CSV NÃO confirmada (a página TextView oferece 'Download CSV'); configure a URL no catálogo."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timezone
from typing import Any

from app.config import IntelParams
from app.http import Transport
from app.intel.macro import BLS_INDICATORS, VALUE_UNITS, MacroEvent, MacroMarket, MacroValue, make_event
from app.intel.sources import SourceConfig, SourceError, http_get

# SUMMARY do iCalendar do BLS -> tipo de evento (comparação por trecho, sem diferenciar maiúsculas)
ICS_MAP: list[tuple[re.Pattern[str], str]] = [
    # ANCORADOS no início do SUMMARY (o BLS também publica "State Job Openings...", "Employment Situation of Veterans" etc., que NÃO são o evento principal).
    # O calendário REAL do BLS (conferido em 2026-10-22) usa SUMMARY como "Employment Situation" e "Job Openings and Labor Turnover Survey".
    (re.compile(r"^\s*employment situation\s*(?:\(.*\))?\s*$", re.I), "EMPLOYMENT_SITUATION"),
    (re.compile(r"^\s*consumer price index\s*(?:\(.*\))?\s*$", re.I), "CPI"),
    (re.compile(r"^\s*producer price index\s*(?:\(.*\))?\s*$", re.I), "PPI"),
    (re.compile(r"^\s*(?:job openings and labor turnover survey|jolts)\s*(?:\(.*\))?\s*$", re.I), "JOLTS"),
]
# série BLS -> (modo de cálculo, casas decimais)
BLS_MODES = {"LNS14000000": "level", "CES0000000001": "diff", "CUUR0000SA0": "yoy", "CUUR0000SA0L1E": "yoy", "WPSFD4": "yoy", "JTS000000000000000JOL": "level"}
_EASTERN = re.compile(r"eastern|new[_ /-]?york|est5edt", re.I)  # o BLS real usa TZID="US-Eastern"


# --------------------------------------------------------------------------------------------------------- iCalendar
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    return lines


def parse_ics(text: str, src: SourceConfig, now: datetime) -> tuple[list[MacroEvent], int]:
    """Retorna (eventos reconhecidos, nº de eventos ignorados/malformados). Lança SourceError se não for um iCalendar."""
    if "BEGIN:VCALENDAR" not in text:
        raise SourceError("resposta não é um iCalendar (BEGIN:VCALENDAR ausente)", "invalid")
    events: list[MacroEvent] = []
    ignored = 0
    cur: dict[str, tuple[dict[str, str], str]] | None = None
    for ln in _unfold(text):
        if ln.startswith("BEGIN:VEVENT"):
            cur = {}
        elif ln.startswith("END:VEVENT") and cur is not None:
            ev = _ics_event(cur, src, now)
            if ev is None:
                ignored += 1
            else:
                events.append(ev)
            cur = None
        elif cur is not None and ":" in ln:
            head, value = ln.split(":", 1)
            parts = head.split(";")
            params = {p.split("=", 1)[0].upper(): p.split("=", 1)[1] for p in parts[1:] if "=" in p}
            cur[parts[0].upper()] = (params, value)
    return events, ignored


def _ics_event(d: dict[str, tuple[dict[str, str], str]], src: SourceConfig, now: datetime) -> MacroEvent | None:
    summ = d.get("SUMMARY", ({}, ""))[1]
    kind = next((k for rx, k in ICS_MAP if rx.search(summ)), None)
    if kind is None or "DTSTART" not in d:
        return None
    params, raw = d["DTSTART"]
    raw = raw.strip()
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", raw):
            # dia inteiro (sem horário): não inventa hora — usa 08:30 ET como ESTIMATIVA e marca como "estimado"
            dt = datetime.strptime(raw[:8], "%Y%m%d")
            return make_event(kind, dt.strftime("%Y-%m-%d"), "08:30", "America/New_York", source_id=src.id, source_url=src.url, confidence="estimado")
        if raw.endswith("Z"):
            utc = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            from app.intel.ustime import utc_to_et

            local = utc_to_et(utc)
            ev = make_event(kind, local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), "America/New_York", source_id=src.id, source_url=src.url, confidence="oficial")
            ev.scheduled_utc = utc  # o instante UTC informado pela fonte é o que vale
            return MacroEvent(**{**ev.__dict__, "id": _eid(kind, utc)})
        tz = params.get("TZID", src.tz)
        if not _EASTERN.search(tz) and tz.upper() not in ("UTC", "GMT"):
            return None
        dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
        return make_event(kind, dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), "UTC" if tz.upper() in ("UTC", "GMT") else "America/New_York", source_id=src.id, source_url=src.url,
                          confidence="oficial" if "TZID" in params else "estimado")
    except (ValueError, KeyError):
        return None


def _eid(kind: str, utc: datetime) -> str:
    from app.intel.macro import event_id

    return event_id(kind, utc)


# --------------------------------------------------------------------------------------------------------- BLS (valores)
def parse_bls_series(body: Any, series_id: str, source_id: str, observed_at: datetime) -> MacroValue | None:
    """Converte uma resposta da API pública do BLS em UM valor (o período mais recente). Sem consenso: `forecast` fica vazio. None se faltar dado suficiente."""
    if not isinstance(body, dict):
        raise SourceError("resposta BLS inesperada (não é JSON)", "invalid")
    if body.get("status") != "REQUEST_SUCCEEDED":
        msgs = " ".join(str(m) for m in (body.get("message") or []))[:160]
        kind = "rate_limit" if re.search(r"threshold|limit|exceed", msgs, re.I) else "invalid"
        raise SourceError(f"BLS não atendeu a requisição: {body.get('status')} {msgs}", kind)
    try:
        data = body["Results"]["series"][0]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SourceError("resposta BLS sem Results.series[0].data", "invalid") from exc
    pts: dict[tuple[int, int], float] = {}
    for d in data:
        m = re.fullmatch(r"M(\d{2})", str(d.get("period", "")))
        try:
            if m and 1 <= int(m.group(1)) <= 12:
                pts[(int(d["year"]), int(m.group(1)))] = float(str(d["value"]).replace(",", ""))
        except (KeyError, ValueError, TypeError):
            continue
    if not pts:
        return None
    ordered = sorted(pts)  # crescente por (ano, mês)
    y, mo = ordered[-1]
    mode = BLS_MODES.get(series_id, "level")
    ind = BLS_INDICATORS.get(series_id, series_id)

    def prev_key(k: tuple[int, int]) -> tuple[int, int]:
        return (k[0] - 1, 12) if k[1] == 1 else (k[0], k[1] - 1)

    def value_at(k: tuple[int, int]) -> float | None:
        if k not in pts:
            return None
        if mode == "level":
            return pts[k]
        if mode == "diff":
            pk = prev_key(k)
            return pts[k] - pts[pk] if pk in pts else None
        yk = (k[0] - 1, k[1])  # yoy
        return (pts[k] / pts[yk] - 1) * 100 if yk in pts and pts[yk] else None

    actual = value_at((y, mo))
    if actual is None:
        return None
    prev = value_at(prev_key((y, mo)))
    return MacroValue(ind, f"{y}-{mo:02d}", observed_at, round(actual, 4), None, round(prev, 4) if prev is not None else None, None, VALUE_UNITS.get(ind, ""), source_id, False,
                      "BLS não publica expectativa de mercado (expectativa N/D)")


# --------------------------------------------------------------------------------------------------------- Tesouro (rendimentos)
def parse_treasury_csv(text: str, source_id: str, observed_at: datetime) -> list[MacroMarket]:
    """CSV do Tesouro (colunas 'Date', '2 Yr', '10 Yr', ...). Usa as duas últimas datas para calcular a variação diária em pontos-base."""
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(rdr)
    except StopIteration:
        raise SourceError("CSV do Tesouro vazio", "invalid") from None
    norm = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in header]

    def col(*names: str) -> int | None:
        return next((norm.index(n) for n in names if n in norm), None)

    c_date, c2, c10 = col("date"), col("2yr", "2year", "2y"), col("10yr", "10year", "10y")
    if c_date is None or (c2 is None and c10 is None):
        raise SourceError("CSV do Tesouro sem as colunas Date/2 Yr/10 Yr esperadas", "invalid")
    rows: list[tuple[date, dict[str, float]]] = []
    for r in rdr:
        if len(r) <= c_date:
            continue
        ds = r[c_date].strip()
        d = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                d = datetime.strptime(ds, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            continue
        vals: dict[str, float] = {}
        for name, c in (("UST_2Y", c2), ("UST_10Y", c10)):
            if c is not None and len(r) > c and r[c].strip() not in ("", "N/A"):
                try:
                    vals[name] = float(r[c])
                except ValueError:
                    pass
        if vals:
            rows.append((d, vals))
    rows.sort(key=lambda x: x[0])
    out: list[MacroMarket] = []
    for name in ("UST_2Y", "UST_10Y"):
        series = [(d, v[name]) for d, v in rows if name in v]
        if not series:
            continue
        d, val = series[-1]
        pv = series[-2][1] if len(series) >= 2 else None
        out.append(MacroMarket(name, d.isoformat(), val, pv, round((val - pv) * 100, 2) if pv is not None else None, observed_at, source_id))
    return out


# --------------------------------------------------------------------------------------------------------- coleta (usada pelo coletor)
def fetch_macro(transport: Transport, src: SourceConfig, p: IntelParams, now: datetime) -> Any:
    """Uma rodada de requisições GET desta fonte; devolve a carga analisada (eventos, valores ou mercado). Lança SourceError com `kind`."""
    kw = dict(user_agent=p.user_agent, timeout=p.source_timeout_s, total_timeout=p.total_timeout_s, max_bytes=p.max_feed_bytes, now=now)
    if src.kind == "ics":
        r = http_get(transport, src, accept="text/calendar, text/plain;q=0.8", **kw)  # type: ignore[arg-type]
        if not isinstance(r.body, str):
            raise SourceError("iCalendar esperado como texto", "invalid")
        events, _ign = parse_ics(r.body, src, now)
        return events
    if src.kind == "treasury_csv":
        r = http_get(transport, src, accept="text/csv, text/plain;q=0.8", **kw)  # type: ignore[arg-type]
        if not isinstance(r.body, str):
            raise SourceError("CSV esperado como texto", "invalid")
        return parse_treasury_csv(r.body, src.id, now)
    if src.kind == "bls_api":
        values: list[MacroValue] = []
        for sid in src.series:
            r = http_get(transport, src, url=src.url.rstrip("/") + "/" + sid, accept="application/json", **kw)  # type: ignore[arg-type]
            body = r.body if not isinstance(r.body, str) else _json_or_raise(r.body)
            v = parse_bls_series(body, sid, src.id, now)
            if v is not None:
                values.append(v)
        return values
    raise SourceError(f"tipo de fonte macro não suportado: {src.kind}", "invalid")


def _json_or_raise(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SourceError("resposta BLS não é JSON (possível página de erro com HTTP 200)", "invalid") from exc


__all__ = ["parse_ics", "parse_bls_series", "parse_treasury_csv", "fetch_macro"]
