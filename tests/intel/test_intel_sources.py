import json
import os
import tempfile
from datetime import timedelta

from app.http import HttpResponse
from app.intel.dedup import canonical_url, item_id, jaccard, normalize_title, safe_url, title_tokens
from app.intel.sources import SourceError, clean_text, fetch_source, load_sources, parse_date, parse_feed, parse_gdelt
from _helpers import raises
from _intel import FakeFeeds, GDELT, MEDIA_A, NOW, TIMEOUT, ok_xml, rfc822, rss

FETCH = dict(user_agent="teste/1.0", timeout=5.0, max_bytes=100_000, max_items=50, now=NOW)


def _catalog(sources):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.json")
    open(p, "w").write(json.dumps({"sources": sources}))
    return p


def test_catalog_validation_rejects_unsafe_urls_ids_and_ranges():
    good = {"id": "ok_src", "kind": "rss", "url": "https://example.com/feed", "reliability": 0.7}
    assert load_sources(_catalog([good]))[0].id == "ok_src"
    bad_urls = ["http://example.com/feed", "https://localhost/feed", "https://127.0.0.1/feed", "https://10.0.0.5/feed", "https://192.168.1.1/x", "https://169.254.169.254/latest",
                "https://user:pw@example.com/feed", "ftp://example.com/f", "file:///etc/passwd", "https://printer.local/f", "javascript:alert(1)"]
    for u in bad_urls:
        with raises(SourceError):
            load_sources(_catalog([{**good, "url": u}]))
    for patch in ({"reliability": 1.5}, {"reliability": -0.1}, {"kind": "scrape"}, {"id": "Bad Id!"}, {"id": ""}):
        with raises(SourceError):
            load_sources(_catalog([{**good, **patch}]))
    with raises(SourceError, "duplicado"):
        load_sources(_catalog([good, good]))
    with raises(SourceError):
        load_sources(os.path.join(tempfile.mkdtemp(), "nao-existe.json"))
    p = _catalog([good])
    open(p, "w").write("{ isto nao e json")
    with raises(SourceError):
        load_sources(p)


def test_catalog_example_in_repo_is_valid_and_conservative():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    sources = load_sources(root / "config" / "intel_sources.example.json")
    assert sources and all(s.url.startswith("https://") for s in sources)
    # só foi declarado "verified" o que foi conferido contra resposta REAL (diagnóstico de 2026-10-22)
    assert {s.id for s in sources if s.verified} == {"solana_status", "fed_monetary", "fed_speeches", "bls_calendar", "treasury_yields"}  # as 5 conferidas em 2026-10-22
    assert next(s for s in sources if s.id == "fed_speeches").default_indicator == "FED_SPEECH"
    enabled = {s.id for s in sources if s.enabled}
    assert enabled <= {"solana_status", "fed_monetary"}  # só as validadas habilitadas por padrão; macro/mídia/GDELT/SEC exigem decisão do operador
    assert [s.id for s in sources if s.critical] == ["solana_status"] and next(s for s in sources if s.id == "solana_status").status_format == "statuspage"
    assert {s.kind for s in sources} >= {"rss", "gdelt", "ics", "bls_api", "treasury_csv"}


def test_parse_rss_and_atom_extract_fields_and_clean_text():
    text = rss([("Solana &lt;b&gt;outage&lt;/b&gt; resolved", "https://x.example.com/a?utm_source=t", "&lt;p&gt;Detalhes &amp;amp; mais&lt;/p&gt;", rfc822(NOW - timedelta(hours=1)))])
    items, bad = parse_feed(text, MEDIA_A, NOW, 10)
    assert bad == 0 and len(items) == 1
    it = items[0]
    assert it.title == "Solana outage resolved" and it.summary == "Detalhes & mais" and it.published_at == NOW - timedelta(hours=1) and it.published_at.tzinfo is not None
    atom = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom título</title><link rel="alternate" href="https://a.example.com/1"/>'
            '<updated>2026-10-22T10:00:00Z</updated><summary>resumo</summary></entry></feed>')
    items, bad = parse_feed(atom, MEDIA_A, NOW, 10)
    assert len(items) == 1 and items[0].url == "https://a.example.com/1" and items[0].published_at.hour == 10


def test_malformed_items_are_counted_not_invented():
    text = rss([("", "https://x.example.com/1", "", rfc822(NOW)), ("Sem link", "", "", rfc822(NOW)), ("Link perigoso", "javascript:alert(1)", "", rfc822(NOW)),
                ("Ok sem data", "https://x.example.com/2", "", "isto não é data"), ("Ok", "https://x.example.com/3", "", rfc822(NOW))])
    items, bad = parse_feed(text, MEDIA_A, NOW, 10)
    assert bad == 3 and len(items) == 2
    assert [i.published_at is None for i in items] == [True, False]  # data inválida => None (nunca "agora")


def test_hostile_xml_is_rejected_before_parsing():
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><rss><channel><item><title>&lol2;</title></channel></rss>'
    for hostile in (bomb, '<!DOCTYPE rss SYSTEM "http://evil.example/x.dtd"><rss/>', "<!ENTITY x 'y'><rss/>"):
        with raises(SourceError, "DTD"):
            parse_feed(hostile, MEDIA_A, NOW, 10)
    with raises(SourceError, "inválido"):
        parse_feed("<rss><channel><item>", MEDIA_A, NOW, 10)
    with raises(SourceError, "não é RSS"):
        parse_feed("<html><body>Bem-vindo</body></html>", MEDIA_A, NOW, 10)
    items, bad = parse_feed('<rss version="2.0"><channel><title>vazio</title></channel></rss>', MEDIA_A, NOW, 10)
    assert items == [] and bad == 0  # feed vazio é válido (≠ erro)


def test_gdelt_parse_and_html_with_http_200_is_an_error():
    body = {"articles": [{"url": "https://n.example.com/1", "title": "Solana halt", "seendate": "20261022T110000Z", "domain": "n.example.com"}, {"url": "", "title": "sem url"}, "lixo"]}
    items, bad = parse_gdelt(body, GDELT, NOW, 10)
    assert len(items) == 1 and bad == 2 and items[0].published_at == NOW.replace(hour=11)
    for not_json in ("<html>erro</html>", {"status": "x"}, None, ["a"]):
        with raises(SourceError):
            parse_gdelt(not_json, GDELT, NOW, 10)


def test_date_parsing_variants():
    assert parse_date("Thu, 22 Oct 2026 10:00:00 GMT") == NOW.replace(hour=10)
    assert parse_date("Thu, 22 Oct 2026 07:00:00 -0300") == NOW.replace(hour=10)
    assert parse_date("2026-10-22T10:00:00Z") == NOW.replace(hour=10)
    assert parse_date("2026-10-22T10:00:00+02:00") == NOW.replace(hour=8)
    assert parse_date("20261022T100000Z") == NOW.replace(hour=10)
    for junk in (None, "", "ontem", "99/99/9999", "2026-13-45T00:00:00Z"):
        assert parse_date(junk) is None


def test_fetch_source_error_classification_headers_and_limits():
    url = MEDIA_A.url
    feed = rss([("T", "https://x.example.com/1", "", rfc822(NOW))])
    t = FakeFeeds({url: ok_xml(feed)})
    items, _ = fetch_source(t, MEDIA_A, **FETCH)
    assert len(items) == 1 and t.calls[0][1]["User-Agent"] == "teste/1.0"
    for resp, frag in ((TIMEOUT, "tempo esgotado"), (HttpResponse(429, "", {}), "429"), (HttpResponse(503, "", {}), "servidor"), (HttpResponse(403, "", {}), "403"),
                       (HttpResponse(404, "", {}), "404"), (HttpResponse(200, {"x": 1}, {}), "texto"), (ok_xml("x" * 200_000), "grande demais")):
        with raises(SourceError, frag):
            fetch_source(FakeFeeds({url: resp}), MEDIA_A, **FETCH)
    g = FakeFeeds({GDELT.url: HttpResponse(200, {"articles": []}, {})})
    assert fetch_source(g, GDELT, **FETCH)[0] == []
    q = g.calls[0][2]
    assert q["mode"] == "artlist" and q["format"] == "json" and q["sort"] == "datedesc" and q["query"] == "solana" and q["maxrecords"] <= 75


def test_clean_text_strips_html_control_chars_and_truncates():
    assert clean_text("<script>alert(1)</script>Olá\x07 &lt;b&gt;", 100) == "alert(1) Olá <b>"  # tags removidas, caractere de controle removido; entidades viram texto inerte
    assert len(clean_text("a" * 5000, 300)) == 300 and clean_text(None, 10) == ""


def test_dedup_helpers_url_title_tokens_and_safe_url():
    assert canonical_url("https://www.Example.com/a/?utm_source=x&b=2&a=1&fbclid=z#frag") == "https://example.com/a?a=1&b=2"
    assert item_id("https://x.com/a?utm_medium=1", "Solana  Halts!") == item_id("https://www.x.com/a", "solana halts")
    assert normalize_title("  SOL: Rede, parada!! ") == "sol rede parada"
    a, b = title_tokens("Solana network halts after validator bug"), title_tokens("Solana network halted after validator bug")
    assert jaccard(a, b) > 0.5 and jaccard(a, title_tokens("Fed holds rates steady")) == 0.0 and jaccard(frozenset(), a) == 0.0
    for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.com", "https://u:p@x.com/", "", "https://", "a" * 2100):
        assert safe_url(bad) is None
    assert safe_url("https://x.example.com/ok") == "https://x.example.com/ok"
