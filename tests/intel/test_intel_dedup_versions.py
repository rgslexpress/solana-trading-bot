"""P0.2 — Deduplicação: títulos repetidos NÃO descartam incidentes novos; atualizações oficiais do mesmo incidente são preservadas como versões.

SIMULADO: o formato do corpo de incidente imita o padrão Atlassian Statuspage ("<strong>Resolved</strong> - ...") descrito na documentação;
o formato de um incidente ATIVO no feed real do Solana Status não foi observado (o feed real só continha incidentes já resolvidos)."""

import html
from datetime import timedelta

from app.http import HttpResponse
from app.intel.collector import NewsCollector
from app.intel.engine import IntelEngine
from app.intel.store import DbNewsView, IntelStore
from app.storage.database import Database
from _intel import CLF, FakeFeeds, MEDIA_A, NOW, SOLANA_STATUS, ok_xml, params, rfc822, rss

T = "Solana Mainnet Beta Degraded Performance"


def desc(stage: str, text: str) -> str:
    """Descrição no estilo Statuspage (HTML escapado dentro do XML)."""
    return html.escape(f"<p><small>Oct 22, 10:00 UTC</small><br><strong>{stage}</strong> - {text}</p>")


def _setup(feeds, src=SOLANA_STATUS, **kw):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    t = FakeFeeds({src.url: feeds})
    P = params(stale_after_s=10**8, **kw)
    c = NewsCollector(P, [src], t, store, CLF)
    return c, store, t, IntelEngine(P, DbNewsView(store, [src], P)), P


def _feed(*items):
    return ok_xml(rss([(title, link, d, rfc822(pub)) for title, link, d, pub in items]))


def test_identical_titles_with_different_urls_and_dates_are_all_kept_and_the_new_incident_blocks():
    old = _feed((T, "https://status.solana.com/incidents/aaa", desc("Resolved", "fixed"), NOW - timedelta(hours=30)))
    c, store, t, eng, P = _setup([old])
    c.refresh(NOW - timedelta(hours=29))
    new = _feed((T, "https://status.solana.com/incidents/bbb", desc("Investigating", "validators are not producing blocks, network halted"), NOW - timedelta(minutes=5)),
                (T, "https://status.solana.com/incidents/aaa", desc("Resolved", "fixed"), NOW - timedelta(hours=30)))
    t.routes[SOLANA_STATUS.url] = [new]
    st = c.refresh(NOW)
    recs = {r.url[-3:]: r for r in store.latest()}
    assert st["novos"] == 1 and set(recs) == {"aaa", "bbb"}  # o incidente novo NÃO foi descartado por ter o mesmo título
    assert "duplicada" not in recs["bbb"].flags and recs["bbb"].actionable
    a = eng.assess("JUP", NOW + timedelta(minutes=1))
    assert a.block and a.alerts[0].impact == 4  # parada total confirmada na fonte oficial: bloqueio


def test_regression_titles_repeated_within_the_old_72h_window_no_longer_hide_a_new_incident():
    """Reproduz o defeito A1 da auditoria: título repetido 30 h depois era marcado 'duplicada' e ignorado."""
    c, store, t, eng, _ = _setup([_feed((T, "https://status.solana.com/incidents/aaa", desc("Resolved", "ok"), NOW - timedelta(hours=31)))])
    c.refresh(NOW - timedelta(hours=30))
    t.routes[SOLANA_STATUS.url] = [_feed((T, "https://status.solana.com/incidents/bbb", desc("Identified", "mainnet outage: validators offline"), NOW - timedelta(minutes=2)))]
    c.refresh(NOW)
    new = next(r for r in store.latest() if r.url.endswith("bbb"))
    assert new.flags == [] and new.actionable


def test_successive_updates_of_the_same_incident_are_preserved_as_versions_including_severity_and_resolution():
    url = "https://status.solana.com/incidents/ccc"
    pub = NOW - timedelta(hours=2)
    steps = [("Investigating", "we are investigating reports of delays", 0), ("Identified", "Solana mainnet outage: validators offline, block production halted", 20),
             ("Monitoring", "a fix was deployed; monitoring", 60), ("Resolved", "the incident is resolved", 100)]
    c, store, t, eng, P = _setup([_feed((T, url, desc(steps[0][0], steps[0][1]), pub))])  # a pubDate NÃO muda entre as versões (pior caso)
    states = {}
    for stage, text, minutes in steps:
        now = NOW - timedelta(minutes=120 - minutes)
        t.routes[SOLANA_STATUS.url] = [_feed((T, url, desc(stage, text), pub))]
        c.refresh(now)
        a = eng.assess("JUP", now + timedelta(seconds=30))
        states[stage] = (a.block, a.caution, bool(a.alerts))
    versions = [r for r in store.latest() if r.url == url]
    assert sorted(r.status_state for r in versions) == ["identified", "investigating", "monitoring", "resolved"]  # todas as versões guardadas
    assert all("atualizacao" in r.flags for r in versions if r.status_state != "investigating") and all(r.event_key for r in versions)
    # severidade MUDOU na versão "Identified" (parada da mainnet): só aí passa a bloquear; "Resolved" encerra o bloqueio mas mantém a cautela
    assert states["Investigating"][0] is False
    assert states["Identified"][0] is True and states["Monitoring"][0] is True
    assert states["Resolved"][0] is False and states["Resolved"][1] is True  # resolvido: sem bloqueio; janela de cautela pós-evento
    # a versão "Resolved" usa o instante da coleta (a fonte não atualizou a data): o fim do incidente não é antedatado
    resolved = next(r for r in versions if r.status_state == "resolved")
    assert resolved.event_dt > resolved.published_dt


def test_identical_content_in_the_next_round_is_a_duplicate_not_a_new_version():
    url = "https://status.solana.com/incidents/ddd"
    f = _feed((T, url, desc("Investigating", "x outage"), NOW - timedelta(minutes=10)))
    c, store, t, _, _ = _setup([f])
    assert c.refresh(NOW)["novos"] == 1
    s2 = c.refresh(NOW + timedelta(minutes=6))
    assert s2["novos"] == 0 and s2["duplicados"] == 1 and len(store.latest()) == 1


def test_without_a_reliable_identifier_every_distinct_item_is_kept_conservatively():
    """O mesmo link repetido para vários itens do feed (identificador não confiável): nenhum item é descartado por coincidência de URL."""
    same = "https://status.solana.com/"
    f = _feed(("Solana mainnet outage A", same, desc("Investigating", "a"), NOW - timedelta(minutes=30)), ("Solana mainnet outage B", same, desc("Investigating", "b"), NOW - timedelta(minutes=20)),
              ("Solana mainnet outage C", same, desc("Resolved", "c"), NOW - timedelta(minutes=10)))
    c, store, _, _, _ = _setup([f])
    assert c.refresh(NOW)["novos"] == 3 and all(r.event_key is None for r in store.latest())


def test_news_near_duplicates_only_within_the_gap_and_same_url_with_other_date_is_a_republication():
    title = "Solana network halts after validator bug, exchange suspends deposits"
    pub1, pub2 = NOW - timedelta(minutes=50), NOW - timedelta(minutes=40)
    f = ok_xml(rss([(title, "https://m.example.com/1", "", rfc822(pub1)), (title, "https://m.example.com/2", "", rfc822(pub2))]))
    c, store, _, _, _ = _setup([f], src=MEDIA_A)
    c.refresh(NOW)
    flags = {r.url[-1]: r.flags for r in store.latest()}
    assert flags["1"] == [] and "duplicada" in flags["2"]  # mesma fonte, mesmo título, 10 min de diferença: duplicata
    # o MESMO título 36 h depois (dentro da janela de 72 h, mas acima de near_dup_max_gap_h=24 h) NÃO é duplicata: é outro evento
    t36 = NOW + timedelta(hours=36)
    later = ok_xml(rss([(title, "https://m.example.com/3", "", rfc822(t36 - timedelta(minutes=5)))]))
    c.t.routes[MEDIA_A.url] = [later]
    c.refresh(t36)
    assert next(r for r in store.latest() if r.url.endswith("/3")).flags == []
    # mesma URL republicada com data muito posterior => 'reapresentada' (ignorada)
    again = ok_xml(rss([("Another title about outage on Solana", "https://m.example.com/1", "", rfc822(t36 - timedelta(minutes=1)))]))
    c.t.routes[MEDIA_A.url] = [again]
    c.refresh(t36 + timedelta(minutes=1))
    assert any("reapresentada" in r.flags for r in store.latest())


def test_malformed_status_feed_does_not_create_phantom_versions():
    c, store, t, eng, _ = _setup([HttpResponse(200, "<rss><channel><item><title>x</title></item></channel></rss>", {})])
    st = c.refresh(NOW)
    assert st["novos"] == 0 and st["malformados"] == 1
