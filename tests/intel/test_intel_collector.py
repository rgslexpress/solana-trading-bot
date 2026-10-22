from datetime import timedelta

from app.http import HttpResponse
from app.intel.collector import NewsCollector
from app.intel.store import IntelStore, compute_source_states
from app.storage.database import Database
from _intel import CLF, FakeFeeds, MEDIA_A, MEDIA_B, NOW, TIMEOUT, ok_xml, params, rfc822, rss


class Mono:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _setup(routes, sources=(MEDIA_A,), **kw):
    db = Database(":memory:")
    store = IntelStore(db.conn)
    t = FakeFeeds(routes)
    mono = Mono()
    c = NewsCollector(params(**kw), list(sources), t, store, CLF, mono)
    return c, store, t, mono


def _feed(*items):
    return ok_xml(rss([(title, link, "", rfc822(pub)) for title, link, pub in items]))


def test_new_items_are_stored_with_utc_times_source_and_link_and_duplicates_are_skipped():
    pub = NOW - timedelta(minutes=10)
    c, store, t, _ = _setup({MEDIA_A.url: _feed(("Solana network outage hits validators", "https://a.example.com/1", pub))})
    s1 = c.refresh(NOW)
    assert s1["novos"] == 1 and s1["fontes_ok"] == 1
    r = store.latest()[0]
    assert r.source_id == "media_a" and r.url == "https://a.example.com/1" and r.published_dt == pub and r.collected_dt == NOW and r.first_seen == NOW.isoformat()
    assert r.published_at.endswith("+00:00") and r.collected_at.endswith("+00:00")
    s2 = c.refresh(NOW + timedelta(minutes=5))
    assert s2["novos"] == 0 and s2["duplicados"] == 1 and len(store.latest()) == 1  # mesma matéria na rodada seguinte
    assert t.calls[0][1]["User-Agent"].startswith("solana-trading-bot-paper")


def test_same_story_with_tracking_params_or_www_is_a_duplicate():
    pub = NOW - timedelta(minutes=10)
    c, store, _, _ = _setup({MEDIA_A.url: [_feed(("Solana outage", "https://a.example.com/x?utm_source=twitter", pub)), _feed(("Solana outage", "https://www.a.example.com/x/?utm_campaign=z", pub))]})
    c.refresh(NOW)
    st = c.refresh(NOW + timedelta(minutes=6))
    assert st["novos"] == 0 and len(store.latest()) == 1


def test_near_duplicate_title_in_same_source_is_flagged_and_not_actionable():
    pub = NOW - timedelta(minutes=30)
    c, store, _, _ = _setup({MEDIA_A.url: _feed(("Solana network halts after validator bug", "https://a.example.com/1", pub), ("Solana Network Halts After Validator Bug!", "https://a.example.com/2", pub + timedelta(minutes=5)))})
    c.refresh(NOW)
    recs = sorted(store.latest(), key=lambda r: r.published_at)
    assert len(recs) == 2 and recs[1].duplicate_of == recs[0].id and "duplicada" in recs[1].flags and not recs[1].actionable and recs[0].actionable


def test_old_story_with_bumped_date_on_same_url_is_flagged_as_resurfaced():
    c, store, _, _ = _setup({MEDIA_A.url: [_feed(("Solana network outage", "https://a.example.com/x", NOW - timedelta(days=1))),
                                           _feed(("Solana network outage (updated)", "https://a.example.com/x", NOW - timedelta(minutes=5)))]})
    c.refresh(NOW)
    c.refresh(NOW + timedelta(minutes=6))
    flagged = [r for r in store.latest() if "reapresentada" in r.flags]
    assert len(flagged) == 1 and not flagged[0].actionable  # nunca vira alerta como se fosse nova


def test_similar_title_in_other_source_counts_as_corroboration_unless_it_is_older_news():
    t1, t2 = NOW - timedelta(minutes=40), NOW - timedelta(minutes=30)
    c, store, _, _ = _setup({MEDIA_A.url: _feed(("Solana network halts after validator bug", "https://a.example.com/1", t1)),
                             MEDIA_B.url: _feed(("Solana network halts after validator bug", "https://b.example.com/9", t2))}, sources=(MEDIA_A, MEDIA_B))
    c.refresh(NOW)
    recs = store.latest()
    assert len(recs) == 2 and all("similar_antigo" not in r.flags and r.actionable for r in recs)
    # mesma história "nova" em outra fonte, mas bem mais velha que a original => não conta como corroboração
    old = NOW - timedelta(hours=20)
    c2, store2, _, _ = _setup({MEDIA_A.url: _feed(("Solana network halts after validator bug", "https://a.example.com/1", old)),
                               MEDIA_B.url: _feed(("Solana network halts after validator bug", "https://b.example.com/9", NOW - timedelta(minutes=5)))}, sources=(MEDIA_A, MEDIA_B), item_max_age_h=48)
    c2.refresh(NOW)
    assert any("similar_antigo" in r.flags for r in store2.latest())


def test_future_dated_and_too_old_items_are_flagged_and_malformed_counted():
    c, store, _, _ = _setup({MEDIA_A.url: _feed(("Solana outage futuro", "https://a.example.com/f", NOW + timedelta(hours=5)), ("Solana outage antigo", "https://a.example.com/o", NOW - timedelta(days=5)),
                                                ("Solana outage ok", "https://a.example.com/k", NOW - timedelta(minutes=3)))})
    st = c.refresh(NOW)
    by = {r.title: r for r in store.latest()}
    # item com data no FUTURO não é gravado (não fica "marcado para sempre"; é aceito quando a data chegar), e conta como malformado
    assert "Solana outage futuro" not in by and "antiga" in by["Solana outage antigo"].flags and by["Solana outage ok"].actionable
    assert st["malformados"] >= 1
    later = c.refresh(NOW + timedelta(hours=5, minutes=1))  # a data "do futuro" chegou: o mesmo item passa a ser aceito normalmente
    assert {r.title: r for r in store.latest()}["Solana outage futuro"].actionable and later["novos"] >= 1


def test_source_state_transitions_unavailable_ok_stale_inconsistent():
    feed = _feed(("Solana outage ok", "https://a.example.com/k", NOW - timedelta(minutes=3)))
    c, store, t, mono = _setup({MEDIA_A.url: [TIMEOUT, TIMEOUT, TIMEOUT, feed]})
    p = c.p
    assert compute_source_states([MEDIA_A], store.status_rows(), NOW, p)[0].state == "unavailable"  # nunca coletou
    c.refresh(NOW)
    st = compute_source_states([MEDIA_A], store.status_rows(), NOW, p)[0]
    assert st.state == "unavailable" and "tempo esgotado" in (st.last_error or "") and st.consecutive_failures == 1
    for i in (1, 2):
        c.refresh(NOW + timedelta(minutes=5 * i))
    assert compute_source_states([MEDIA_A], store.status_rows(), NOW + timedelta(minutes=10), p)[0].consecutive_failures == 3
    # circuito aberto após 3 falhas: a coleta pula a fonte (não martela o servidor)
    assert c.refresh(NOW + timedelta(minutes=15))["puladas"] == 1 and len(t.calls) == 3
    mono.t += 700  # circuito meio-aberto
    c.refresh(NOW + timedelta(minutes=30))
    # recuperação só é CONFIRMADA após recovery_min_successes sucessos seguidos (até lá, continua marcada como indisponível)
    partial = compute_source_states([MEDIA_A], store.status_rows(), NOW + timedelta(minutes=30), p)[0]
    assert partial.state == "unavailable" and partial.recovering == "1/2"
    c.refresh(NOW + timedelta(minutes=36))
    ok_state = compute_source_states([MEDIA_A], store.status_rows(), NOW + timedelta(minutes=36), p)[0]
    assert ok_state.state == "ok" and ok_state.consecutive_failures == 0 and ok_state.recovering is None
    stale = compute_source_states([MEDIA_A], store.status_rows(), NOW + timedelta(minutes=36, seconds=p.stale_after_s + 60), p)[0]
    assert stale.state == "stale"  # sem novas coletas com sucesso
    # fonte que responde, mas só com lixo => inconsistente
    junk = rss([("", "", "", "")] * 6 + [("Ok", "https://a.example.com/ok", "", rfc822(NOW))])
    c3, store3, _, _ = _setup({MEDIA_A.url: ok_xml(junk)})
    c3.refresh(NOW)
    assert compute_source_states([MEDIA_A], store3.status_rows(), NOW, c3.p)[0].state == "invalid"


def test_refresh_budget_and_interval_are_respected():
    feed = _feed(("Solana outage", "https://a.example.com/1", NOW - timedelta(minutes=3)))
    c, store, t, mono = _setup({MEDIA_A.url: feed, MEDIA_B.url: feed}, sources=(MEDIA_A, MEDIA_B), refresh_budget_s=5)

    class Slow(FakeFeeds):
        def request(self, *a, **k):
            mono.t += 10  # cada fonte "demora" 10 s
            return super().request(*a, **k)

    c.t = Slow({MEDIA_A.url: feed, MEDIA_B.url: feed})
    st = c.refresh(NOW)
    assert st["fontes_ok"] == 1 and st["puladas"] == 1  # orçamento de tempo estourou: a 2ª fonte fica para a próxima rodada
    assert c.refresh_if_due(NOW + timedelta(seconds=10)) is None  # ainda não venceu o intervalo
    assert c.refresh_if_due(NOW + timedelta(seconds=c.p.refresh_interval_s + 1)) is not None


def test_collector_never_raises_on_unexpected_errors_and_bad_http():
    class Boom:
        def request(self, *a, **k):
            raise ValueError("falha inesperada do transporte")

    c, store, _, _ = _setup({})
    c.t = Boom()
    st = c.refresh(NOW)  # não propaga
    assert st["fontes_falha"] == 1
    assert "erro inesperado" in (store.status_rows()["media_a"]["last_error"])
    for resp in (HttpResponse(500, "", {}), HttpResponse(200, "<html>captcha</html>", {}), HttpResponse(200, "", {})):
        c2, s2, _, _ = _setup({MEDIA_A.url: resp})
        assert c2.refresh(NOW)["fontes_falha"] == 1 and s2.latest() == []


def test_retention_purges_old_cache_only_when_configured():
    feed = _feed(("Solana outage", "https://a.example.com/1", NOW - timedelta(minutes=3)))
    c, store, _, _ = _setup({MEDIA_A.url: feed}, retention_days=1)
    c.refresh(NOW)
    assert len(store.latest()) == 1
    c.refresh(NOW + timedelta(days=3))  # fonte repete o mesmo item; cache antigo é descartado, depois recoletado como novo (já fora do TTL)
    c0, store0, _, _ = _setup({MEDIA_A.url: feed}, retention_days=0)
    c0.refresh(NOW)
    c0.refresh(NOW + timedelta(days=400))
    assert len(store0.latest()) >= 1  # retention_days=0 => nunca apaga
