"""Deduplicação e normalização de notícias."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|cmpid$|ocid$)", re.I)
_STOP = frozenset("the a an of to in on for and or at by with from is are was were be been this that as it its has have had will would after before over into amid says say said new".split())
_WORD = re.compile(r"[a-z0-9áéíóúâêôãõç]+", re.I)


def safe_url(url: str) -> str | None:
    """Aceita apenas http/https, sem credenciais embutidas e com tamanho razoável."""
    url = (url or "").strip()
    if not url or len(url) > 2000:
        return None
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    return url


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)]
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), host, path, urlencode(sorted(q)), ""))


def normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(title.lower()) if len(w) >= 3 and w not in _STOP)


def item_id(url: str, title: str, version: str = "") -> str:
    """Identificador da VERSÃO do item. `version` (estado do incidente + resumo) permite que atualizações oficiais do MESMO incidente
    (mesma URL e mesmo título) sejam guardadas como versões distintas, em vez de descartadas como duplicatas."""
    return hashlib.sha256(f"{canonical_url(url)}|{normalize_title(title)}|{version}".encode()).hexdigest()[:24]


def content_fingerprint(summary: str, status_state: str | None) -> str:
    """Impressão do conteúdo que MUDA numa atualização de status (estado + resumo normalizado)."""
    return hashlib.sha256(f"{status_state or ''}|{normalize_title(summary)[:600]}".encode()).hexdigest()[:12]


def registrable_domain(url: str) -> str:
    """Aproximação do 'domínio registrável' (últimos 2 rótulos; 3 para sufixos comuns tipo .co.uk) para agrupar o mesmo veículo."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def text_tokens(title: str, summary: str) -> frozenset[str]:
    return title_tokens(title + " " + summary)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
