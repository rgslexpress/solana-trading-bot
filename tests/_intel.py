"""Utilitários dos testes da camada News & Market Intelligence (tudo offline e determinístico)."""

from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

from app.config import IntelParams
from app.http import HttpResponse, TransportError
from app.intel.classifier import Classifier
from app.intel.models import NewsRecord, RawItem
from app.intel.sources import SourceConfig

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CLF = Classifier([("SOL", ()), ("JUP", ("Jupiter",))], IntelParams())

SOLANA_STATUS = SourceConfig("solana_status", "Solana Status", "rss", "https://status.solana.com/history.rss", 0.95, official=True, assets=("SOL",), domain="solana_status", critical=True, status_format="statuspage")
FED = SourceConfig("fed_monetary", "Federal Reserve", "rss", "https://www.federalreserve.gov/feeds/press_monetary.xml", 0.95, official=True, domain="macro_us")
MEDIA_A = SourceConfig("media_a", "Mídia A", "rss", "https://media-a.example.com/rss", 0.7)
MEDIA_B = SourceConfig("media_b", "Mídia B", "rss", "https://media-b.example.com/rss", 0.7)
GDELT = SourceConfig("gdelt", "GDELT", "gdelt", "https://api.gdeltproject.org/api/v2/doc/doc", 0.4, query="solana")


def params(**kw) -> IntelParams:
    return IntelParams(**{"mode": "enforce", **kw})


def rec(title: str, source: SourceConfig = SOLANA_STATUS, minutes_ago: float = 30, summary: str = "", url: str | None = None, now: datetime = NOW,
        collected_lag_min: float = 1.0, published: datetime | None = None, status_state: str | None = None) -> NewsRecord:
    pub = published if published is not None else now - timedelta(minutes=minutes_ago)
    coll = pub + timedelta(minutes=collected_lag_min)
    raw = RawItem(source.id, title, summary, url or f"https://{source.id}.example.com/{zlib.crc32(title.encode()) % 10**8}", pub, coll, status_state)
    return CLF.classify(raw, source, coll)


def rss(items: list[tuple[str, str, str, str]]) -> str:
    """items: (título, link, descrição, pubDate RFC 822)"""
    body = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description><pubDate>{p}</pubDate></item>" for t, l, d, p in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


def rfc822(d: datetime) -> str:
    return d.strftime("%a, %d %b %Y %H:%M:%S +0000")


class FakeFeeds:
    """Transporte falso por URL: valor = HttpResponse | Exception | lista (consumida em ordem; o último se repete)."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict, dict]] = []

    def request(self, method, url, *, headers=None, params=None, json=None, timeout=10.0, **_limits):  # noqa: A002
        self.calls.append((url, dict(headers or {}), dict(params or {})))
        self.last_kwargs = dict(timeout=timeout)
        item = self.routes.get(url)
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if item is None:
            return HttpResponse(404, "", {})
        if isinstance(item, Exception):
            raise item
        return item


def ok_xml(text: str) -> HttpResponse:
    return HttpResponse(200, text, {})


TIMEOUT = TransportError("ReadTimeout", "timeout")
