"""Fontes de notícias: catálogo configurável, coleta (RSS/Atom e GDELT) e análise segura do conteúdo.

Segurança: HTTPS apenas; hosts privados/loopback recusados (SSRF); sem DTD/entidades XML (anti "billion laughs");
limite de tamanho; texto limpo de HTML e caracteres de controle; nada do conteúdo vira comando.
Contratos (RSS 2.0/Atom, GDELT DOC 2.0 `mode=artlist&format=json`) foram conferidos contra respostas REAIS do Solana Status e do Federal Reserve
(relatório de diagnóstico de 2026-10-22). GDELT, SEC e mídias NÃO foram validados contra respostas reais."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from app.http import Transport, TransportError
from app.intel.dedup import canonical_url, safe_url
from app.intel.models import DOMAINS, RawItem

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"
_TAGS = re.compile(r"<[^>]+>")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.I)
_HREF = re.compile(r"""href\s*=\s*["'](https?://[^"'\s>]+)["']""", re.I)
_BARE_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
MAX_TITLE, MAX_SUMMARY, MAX_FULL = 300, 600, 12_000
KINDS_NEWS = ("rss", "gdelt")
KINDS_MACRO = ("ics", "bls_api", "treasury_csv")
ALL_KINDS = KINDS_NEWS + KINDS_MACRO
STATUS_FORMATS = (None, "statuspage")

# Estados estruturados de uma página de status (Atlassian Statuspage). O progresso é MONÓTONO (não volta atrás), então o estado atual do
# incidente é o estágio mais avançado citado no texto, qualquer que seja a ordem de listagem das atualizações.
_STATUS_MARK = re.compile(r"\b(Investigating|Identified|Monitoring|Resolved|Completed|Verifying|In progress|Scheduled|Update)\s+[-–—]\s", re.I)
_STAGE_RANK = {"update": 0, "scheduled": 1, "investigating": 1, "identified": 2, "in progress": 2, "monitoring": 3, "verifying": 3, "resolved": 4, "completed": 4}


class SourceError(Exception):
    """Falha ao obter/analisar uma fonte (nunca ignorada em silêncio: vira estado da fonte).

    kind: http | timeout | network | rate_limit | parse | invalid | other."""

    def __init__(self, message: str, kind: str = "other", http_status: int | None = None, retry_after_s: float | None = None):
        super().__init__(message)
        self.kind, self.http_status, self.retry_after_s = kind, http_status, retry_after_s


@dataclass(frozen=True)
class SourceConfig:
    id: str
    name: str
    kind: str  # rss | gdelt | ics | bls_api | treasury_csv
    url: str
    reliability: float  # 0..1 (julgamento do operador sobre a fonte; NÃO vem de terceiros)
    official: bool = False  # fonte oficial (ex.: Fed, SEC, status page): itens são tratados como "confirmado"
    enabled: bool = True
    query: str = ""  # gdelt
    timespan: str = "3h"  # gdelt
    min_interval_s: float = 0.0
    verified: bool = False  # True somente se o operador já validou a URL/formato manualmente
    assets: tuple[str, ...] = ()  # ativos implícitos da fonte (ex.: status da Solana => "SOL"), mesmo sem citar o nome no texto
    domain: str = "crypto_news"  # solana_status | crypto_news | macro_us
    critical: bool = False  # fonte crítica: se estiver fora do ar, o módulo NÃO é apresentado como saudável
    status_format: str | None = None  # statuspage: descrição com atualizações "Investigating - ...", "Resolved - ..."
    stale_after_s: float | None = None  # sobrescreve intel.stale_after_s para esta fonte
    daily_cap: int | None = None  # máximo de requisições por dia UTC (ex.: BLS v1 sem chave: 25/dia)
    series: tuple[str, ...] = ()  # bls_api: identificadores de séries
    tz: str = "America/New_York"  # ics: fuso presumido para horários locais sem TZID
    default_indicator: str | None = None  # feed de um único tipo macro (ex.: "FED_SPEECH" para os discursos do Fed); o tipo vem do catálogo
    note: str = ""


_DNS_LABEL = re.compile(r"^(?:xn--[a-z0-9-]{1,59}|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$")


def _host_is_safe(host: str) -> bool:
    """Só NOMES DNS comuns (>= 2 rótulos, TLD alfabético): recusa IPs em qualquer notação (127.1, 2130706433, 0x7f.0.0.1, IPv6), host terminado em ponto
    ("localhost.", "metadata.google.internal.") e sufixos internos. Limitação: não resolve o DNS (um nome público que aponta para IP privado — "DNS rebinding" —
    não é detectado aqui; o catálogo é um arquivo do operador e redirecionamentos HTTP não são seguidos)."""
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".intranet")) or host.endswith("."):
        return False
    labels = host.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.match(x) for x in labels):
        return False
    return bool(re.fullmatch(r"[a-z]{2,24}|xn--[a-z0-9-]{1,59}", labels[-1]))  # TLD alfabético: exclui qualquer notação numérica de IP


def load_sources(path: str | Path) -> list[SourceConfig]:
    """Carrega e valida o catálogo. Entradas inválidas lançam SourceError (fail-closed: nada é coletado de fonte duvidosa)."""
    p = Path(path)
    if not p.exists():
        raise SourceError(f"catálogo de fontes não encontrado: {p}", "invalid")
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise SourceError(f"catálogo de fontes inválido (JSON): {exc}", "invalid") from exc
    out: list[SourceConfig] = []
    seen: set[str] = set()
    for d in raw.get("sources", []):
        try:
            sid, kind, url = str(d["id"]), str(d["kind"]), str(d["url"])
            rel = float(d["reliability"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"fonte malformada: {d!r}", "invalid") from exc
        parts = re.match(r"^https://([^/:@]+)(:\d+)?(/.*)?$", url)
        if not parts or not _host_is_safe(parts.group(1).lower()):
            raise SourceError(f"{sid}: URL deve ser https com host público (sem credenciais, localhost ou IP privado)", "invalid")
        if not re.fullmatch(r"[a-z0-9_\-]{2,40}", sid) or sid in seen:
            raise SourceError(f"id de fonte inválido ou duplicado: {sid!r}", "invalid")
        if kind not in ALL_KINDS or not (0.0 <= rel <= 1.0):
            raise SourceError(f"{sid}: kind deve ser {'|'.join(ALL_KINDS)} e reliability entre 0 e 1", "invalid")
        domain = str(d.get("domain", "macro_us" if kind in KINDS_MACRO else "crypto_news"))
        if domain not in DOMAINS:
            raise SourceError(f"{sid}: domain deve ser um de {DOMAINS}", "invalid")
        sf = d.get("status_format")
        if sf not in STATUS_FORMATS:
            raise SourceError(f"{sid}: status_format inválido: {sf!r}", "invalid")
        cap = d.get("daily_cap")
        if cap is not None and (not isinstance(cap, int) or cap < 1):
            raise SourceError(f"{sid}: daily_cap deve ser inteiro >= 1", "invalid")
        stale = d.get("stale_after_s")
        if stale is not None and (not isinstance(stale, (int, float)) or stale <= 0):
            raise SourceError(f"{sid}: stale_after_s deve ser positivo", "invalid")
        from app.intel.macro import INDICATORS

        di = d.get("default_indicator")
        if di is not None and di not in INDICATORS:
            raise SourceError(f"{sid}: default_indicator deve ser um de {sorted(INDICATORS)}", "invalid")
        series = tuple(s for s in d.get("series", []) if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_]{4,40}", s))
        seen.add(sid)
        out.append(SourceConfig(
            id=sid, name=str(d.get("name", sid))[:80], kind=kind, url=url, reliability=rel, official=bool(d.get("official", False)),
            enabled=bool(d.get("enabled", True)), query=str(d.get("query", ""))[:300], timespan=str(d.get("timespan", "3h"))[:10],
            min_interval_s=float(d.get("min_interval_s", 0.0)), verified=bool(d.get("verified", False)),
            assets=tuple(a for a in d.get("assets", []) if isinstance(a, str) and re.fullmatch(r"[A-Z0-9]{2,12}", a)),
            domain=domain, critical=bool(d.get("critical", False)), status_format=sf, stale_after_s=float(stale) if stale else None,
            daily_cap=cap, series=series, tz=str(d.get("tz", "America/New_York"))[:40], default_indicator=di, note=str(d.get("_nota", d.get("note", "")))[:300],
        ))
    return out


# --------------------------------------------------------------------------- limpeza e datas
def clean_text(text: str | None, limit: int) -> str:
    t = html.unescape(_TAGS.sub(" ", text or ""))
    t = _CTRL.sub("", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def parse_date(s: str | None) -> datetime | None:
    """RFC 822 (RSS), ISO 8601 (Atom) ou AAAAMMDDTHHMMSSZ (GDELT) => UTC. Falha => None (nunca inventa data)."""
    if not s:
        return None
    s = s.strip()
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", s):
            return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            d = parsedate_to_datetime(s)
    except (ValueError, TypeError, IndexError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After: segundos inteiros ou data HTTP. Valores inválidos/negativos => None."""
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{1,7}", v):
        return float(v)
    d = parse_date(v)
    if d is None:
        return None
    secs = (d - (now or datetime.now(timezone.utc))).total_seconds()
    return max(0.0, secs)


def _hdr(headers: dict[str, str] | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def status_state_from_text(text: str) -> str | None:
    """Estágio mais avançado de um incidente de página de status (ver nota sobre monotonia acima). None se não houver marcadores."""
    best: tuple[int, str] | None = None
    for m in _STATUS_MARK.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1).lower())
        rank = _STAGE_RANK[name]
        if best is None or rank > best[0]:
            best = (rank, name)
    return best[1].replace(" ", "_") if best else None


def extract_refs(raw_html: str, own_url: str, limit: int = 5) -> tuple[str, ...]:
    """Links citados no corpo do item (exceto o próprio): servem para detectar que uma matéria deriva de outra."""
    own = canonical_url(own_url)
    found: list[str] = []
    for u in _HREF.findall(raw_html or "") + _BARE_URL.findall(html.unescape(_TAGS.sub(" ", raw_html or ""))):
        u = u.rstrip(".,;")
        s = safe_url(u)
        if s and canonical_url(s) != own and canonical_url(s) not in found:
            found.append(canonical_url(s))
        if len(found) >= limit:
            break
    return tuple(found)


# --------------------------------------------------------------------------- RSS / Atom / RDF
def parse_feed(text: str, source: SourceConfig, collected_at: datetime, max_items: int) -> tuple[list[RawItem], int]:
    """Retorna (itens válidos, nº de itens malformados). Lança SourceError(kind="parse") se o documento for inaceitável."""
    if _DTD.search(text):
        raise SourceError("documento com DTD/entidades XML recusado", "parse")
    try:
        root = ET.fromstring(text.lstrip("﻿ \t\r\n"))
    except ET.ParseError as exc:
        raise SourceError(f"XML inválido: {exc}", "parse") from exc
    nodes = root.findall(".//item") or root.findall(f".//{ATOM}entry") or root.findall(f".//{RSS1}item")
    if not nodes and root.tag not in ("rss", f"{ATOM}feed", RDF):
        raise SourceError("documento não é RSS/Atom", "parse")
    items: list[RawItem] = []
    malformed = 0
    for n in nodes[:max_items]:
        def txt(*names: str) -> str:
            for nm in names:
                e = n.find(nm)
                if e is not None and (e.text or "").strip():
                    return e.text or ""
            return ""

        title = clean_text(txt("title", f"{ATOM}title", f"{RSS1}title"), MAX_TITLE)
        link = txt("link", f"{RSS1}link")
        if not link:
            # Atom: prefere rel="alternate" (ou sem rel); nunca rel="self"/"enclosure"/"edit"
            hrefs = [(le.get("rel") or "alternate", le.get("href") or "") for le in n.findall(f"{ATOM}link")]
            link = next((h for rel, h in hrefs if rel == "alternate" and h), "")
        url = safe_url(link.strip())
        if not title or not url:
            malformed += 1
            continue
        raw_desc = txt("description", f"{ATOM}summary", f"{ATOM}content", f"{RSS1}description")
        full = clean_text(raw_desc, MAX_FULL)
        status = status_state_from_text(full) if source.status_format == "statuspage" else None
        pub = parse_date(txt("pubDate", f"{ATOM}published", f"{ATOM}updated", "{http://purl.org/dc/elements/1.1/}date"))
        items.append(RawItem(source.id, title, full[:MAX_SUMMARY], url, pub, collected_at, status, extract_refs(raw_desc, url)))
    return items, malformed


# --------------------------------------------------------------------------- GDELT
def parse_gdelt(body: Any, source: SourceConfig, collected_at: datetime, max_items: int) -> tuple[list[RawItem], int]:
    if not isinstance(body, dict) or not isinstance(body.get("articles", None), list):
        raise SourceError("resposta GDELT inesperada (possível página HTML/erro com HTTP 200, ou limitação de taxa com corpo vazio)", "invalid")
    items: list[RawItem] = []
    malformed = 0
    for a in body["articles"][:max_items]:
        if not isinstance(a, dict):
            malformed += 1
            continue
        title, url = clean_text(a.get("title"), MAX_TITLE), safe_url(str(a.get("url", "")))
        if not title or not url:
            malformed += 1
            continue
        # `seendate` = quando o GDELT VIU o artigo (não a publicação original): conservador para expiração
        items.append(RawItem(source.id, title, "", url, parse_date(a.get("seendate")), collected_at))
    return items, malformed


# --------------------------------------------------------------------------- coleta
def http_get(transport: Transport, source: SourceConfig, *, user_agent: str, timeout: float, total_timeout: float | None, max_bytes: int,
             params: dict[str, Any] | None = None, url: str | None = None, accept: str = "*/*", now: datetime | None = None) -> Any:
    """Uma tentativa (sem retry interno: a próxima rodada tenta de novo; HTTP 429 vira backoff). Lança SourceError com motivo legível.
    Retorna o HttpResponse (status 200)."""
    headers = {"User-Agent": user_agent, "Accept": accept}
    kw: dict[str, Any] = {"headers": headers, "params": params, "timeout": timeout}
    if getattr(transport, "supports_limits", False):
        kw.update(total_timeout=total_timeout, max_bytes=max_bytes)  # prazo TOTAL e corte de tamanho durante o download
    try:
        r = transport.request("GET", url or source.url, **kw)
    except TransportError as exc:
        kind = getattr(exc, "kind", "network")
        if kind == "too_large":
            raise SourceError(f"resposta grande demais (> {max_bytes} bytes)", "invalid") from exc
        raise SourceError(f"{'tempo esgotado' if 'timeout' in kind else 'rede'}: {exc}", "timeout" if "timeout" in kind else "network") from exc
    if r.status == 429:
        ra = parse_retry_after(_hdr(r.headers, "Retry-After"), now)
        raise SourceError("limite de requisições (HTTP 429)" + (f"; Retry-After={ra:g}s" if ra is not None else ""), "rate_limit", 429, ra)
    if 300 <= r.status < 400:
        loc = _hdr(r.headers, "Location")
        dest = re.sub(r"[?#].*", "", loc)[:120] if loc else "?"
        raise SourceError(f"redirecionamento (HTTP {r.status}) NÃO seguido: atualize a URL do catálogo para {dest}", "http", r.status)
    if r.status >= 500:
        raise SourceError(f"erro do servidor (HTTP {r.status})", "http", r.status)
    if r.status != 200:
        raise SourceError(f"resposta inesperada (HTTP {r.status})", "http", r.status)
    size = len(r.body) if isinstance(r.body, str) else len(json.dumps(r.body, default=str)) if r.body is not None else 0
    if size > max_bytes:
        raise SourceError(f"resposta grande demais ({size} bytes > {max_bytes})", "invalid")
    return r


def fetch_source(transport: Transport, source: SourceConfig, *, user_agent: str, timeout: float, max_bytes: int, max_items: int, now: datetime,
                 total_timeout: float | None = None) -> tuple[list[RawItem], int]:
    """Notícias (rss/gdelt). Lança SourceError com `kind` para o diagnóstico de saúde da fonte."""
    params: dict[str, Any] | None = None
    if source.kind == "gdelt":
        params = {"query": source.query, "mode": "artlist", "format": "json", "maxrecords": min(max_items, 75), "sort": "datedesc", "timespan": source.timespan}
    r = http_get(transport, source, user_agent=user_agent, timeout=timeout, total_timeout=total_timeout, max_bytes=max_bytes, params=params, now=now,
                 accept="application/rss+xml, application/atom+xml, application/xml, application/json, text/xml;q=0.9")
    if source.kind == "gdelt":
        return parse_gdelt(r.body, source, now, max_items)
    if not isinstance(r.body, str):
        raise SourceError("feed RSS/Atom esperado como texto", "invalid")
    return parse_feed(r.body, source, now, max_items)


__all__ = ["SourceConfig", "SourceError", "load_sources", "parse_feed", "parse_gdelt", "fetch_source", "http_get", "clean_text", "parse_date",
           "parse_retry_after", "status_state_from_text", "extract_refs"]
