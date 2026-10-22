"""P1.1 — Republicações/matérias derivadas NÃO são confirmações independentes; na dúvida, a incerteza é sinalizada.

SIMULADO: matérias sintéticas. A heurística (similaridade, domínio, origem citada, links) é aproximada: ver limitações em docs/NEWS_INTEL.md."""

from datetime import timedelta

from app.intel.engine import IntelEngine, independent_origins
from app.intel.models import RawItem
from app.intel.sources import SourceConfig
from app.intel.store import MemoryNewsView
from _intel import CLF, NOW, SOLANA_STATUS, params, rec

P = params()


def outlet(name: str, domain: str) -> SourceConfig:
    return SourceConfig(name.lower().replace(" ", "_"), name, "rss", f"https://{domain}/rss", 0.7)


def item(src: SourceConfig, title: str, summary: str = "", path: str = "a", minutes_ago: float = 20, refs: tuple[str, ...] = ()):
    pub = NOW - timedelta(minutes=minutes_ago)
    raw = RawItem(src.id, title, summary, f"https://{src.url.split('/')[2]}/{path}", pub, pub + timedelta(minutes=1), None, refs)
    return CLF.classify(raw, src, pub + timedelta(minutes=1))


A, B, C = outlet("Crypto Daily", "cryptodaily.example"), outlet("Chain Wire", "chainwire.example"), outlet("Block Times", "blocktimes.example")


def assess(*recs):
    return IntelEngine(P, MemoryNewsView(list(recs))).assess("JUP", NOW, "BUY")


def test_same_story_reproduced_by_two_outlets_counts_once_and_cannot_block():
    t = "Solana mainnet outage hits validators, block production halted"
    a = assess(item(A, t), item(B, t))
    assert not a.block and a.alerts[0].corroboration == 1  # uma origem só
    n, uncertain, notes = independent_origins([item(A, t), item(B, t)], P)
    assert n == 1 and any("republicação" in x for x in notes)


def test_rewritten_headline_with_the_same_body_is_still_the_same_origin():
    body = "Validators stopped producing blocks on Solana mainnet; the outage started at 14:02 UTC and transfers are failing across wallets"
    a1 = item(A, "Solana mainnet outage: validators stop producing blocks", body)
    b1 = item(B, "Transfers fail as Solana validators halt block production", body)
    n, _, _ = independent_origins([a1, b1], P)
    assert n == 1 and not assess(a1, b1).block


def test_both_citing_an_unnamed_origin_is_flagged_as_uncertain_and_does_not_confirm():
    a1 = item(A, "Solana network outage claimed according to a tweet", "Validators offline, mainnet halted")
    b1 = item(B, "Social media post says Solana mainnet down, according to a tweet", "Block production stopped on mainnet")
    n, uncertain, notes = independent_origins([a1, b1], P)
    assert n == 1 and uncertain and any("genérica" in x for x in notes)
    a = assess(a1, b1)
    assert not a.block and a.alerts and a.alerts[0].independence == "incerta" and "INDEPENDÊNCIA" in a.alerts[0].reason


def test_named_origin_and_explicit_links_link_the_two_stories():
    a1 = item(A, "Solana mainnet outage hits validators", "Block production halted on mainnet")
    b_named = item(B, "Validators go dark as Solana mainnet stalls, according to Crypto Daily", "Users report failed transfers")
    b_ref = item(B, "Mainnet stall disrupts Solana transfers", "Wallets report failures", path="b2", refs=(a1.url,))
    for b1 in (b_named, b_ref):
        n, _, notes = independent_origins([a1, b1], P)
        assert n == 1, b1.title
        assert notes
    c1 = item(C, "Solana mainnet halts as validators fail", "Cited post", path="c", refs=("https://shared.example/post",))
    c2 = item(B, "Stall on Solana mainnet confirmed", "Same post", path="c2", refs=("https://shared.example/post",))
    assert independent_origins([c1, c2], P)[0] == 1  # ambas citam a mesma página


def test_same_publisher_family_is_one_origin_even_with_different_subdomains():
    x1 = item(outlet("Daily Main", "news.dailywire.example"), "Solana mainnet outage hits validators", "halted", path="1")
    x2 = item(outlet("Daily Markets", "markets.dailywire.example"), "Validators go offline on Solana mainnet", "stalls", path="2")
    assert independent_origins([x1, x2], P)[0] == 1


def test_genuinely_independent_reports_do_corroborate_and_can_block():
    a1 = item(A, "Solana mainnet outage hits validators", "Block production halted for hours")
    b1 = item(B, "Validators go offline as Solana mainnet stalls; engineers investigate", "Users report failed transfers")
    n, uncertain, _ = independent_origins([a1, b1], P)
    assert n == 2 and not uncertain
    a = assess(a1, b1)
    assert a.block and a.alerts[0].independence == "confirmada" and a.alerts[0].corroboration == 2
    assert "origens independentes" in a.block_reasons[0]


def test_intermediate_similarity_is_uncertain_and_not_counted_as_independent_confirmation():
    a1 = item(A, "Solana mainnet outage hits validators today", "Block production halted")
    b1 = item(B, "Solana mainnet outage hits validators, exchanges pause deposits", "Several exchanges paused deposits")
    n, uncertain, notes = independent_origins([a1, b1], P)
    assert uncertain and any("incerta" in x for x in notes), (n, notes)
    assert not assess(a1, b1).block  # claim de duas matérias parecidas: sem confirmação independente afirmada


def test_official_confirmation_blocks_regardless_of_independence_and_is_not_marked_uncertain():
    off = rec("Solana mainnet outage: block production halted", SOLANA_STATUS, minutes_ago=10)
    t = "Solana mainnet outage hits validators"
    a = assess(off, item(A, t), item(B, t))
    assert a.block and a.alerts[0].independence != "incerta"


def test_rumors_and_similar_old_items_never_count_as_independent_confirmation():
    r1 = item(A, "Rumor: Solana mainnet outage, unconfirmed", "validators offline")
    r2 = item(B, "Unconfirmed reports: Solana mainnet halted, speculation grows", "block production stopped")
    assert not assess(r1, r2).block


def test_independence_is_a_pure_function_of_visible_records():
    """Sem estado oculto: a mesma entrada produz a mesma saída (necessário para o backtest sem vazamento)."""
    t = "Solana mainnet outage hits validators"
    recs = [item(A, t), item(B, t), item(C, "Solana halts: validators fail to produce blocks", "separate report")]
    assert independent_origins(recs, P) == independent_origins(list(reversed(recs)), P)
