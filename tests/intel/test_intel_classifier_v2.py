"""Classificador v2 (P0.3 resolução, P1.2 anos, P1.4 ambiente/criticidade, P1.5 falsos positivos, P1.6 negação) — matrizes de teste.

SIMULADO: textos sintéticos. Não prova que as fontes reais usam essas frases; as limitações restantes estão em docs/NEWS_INTEL.md."""

from app.intel.classifier import detect_environment, explicit_resolution, strip_negations
from app.intel.engine import IntelEngine
from app.intel.store import MemoryNewsView
from _intel import FED, MEDIA_A, NOW, SOLANA_STATUS, CLF, params, rec


def _a(records, **kw):
    return IntelEngine(params(**kw), MemoryNewsView(records)).assess("JUP", NOW, "BUY")


# ------------------------------------------------------------------------------------------------ P0.3 — resolução EXPLÍCITA
RESOLVED_POSITIVE = [
    "Network operational again: incident resolved",
    "The incident has been resolved and block production resumed",
    "Service restored for all users",
    "Issue resolved",
    "Solana is back online",
    "The outage was resolved at 14:03 UTC",
    "Normal operations have resumed on mainnet",
    "Mainnet is fully operational",
    "Incidente resolvido: rede restabelecida",
]
RESOLVED_NEGATIVE = [  # NÃO são resolução (futuro, condição, ainda em andamento)
    "Engineers are working until the issue is resolved",
    "The issue will be resolved soon",
    "We expect the incident to be resolved within the hour",
    "Investigating the outage",
    "Identified the cause of the outage, fix in progress",
    "Issue not yet resolved",
    "The problem has not been resolved",
    "Waiting for the network to be restored",
    "Once the issue is resolved we will publish a postmortem",
    "Outage remains unresolved",
    "If the incident is resolved, trading may resume",
    "Still working to restore service",
]


def test_explicit_resolution_positive_and_negative_phrase_matrix():
    for t in RESOLVED_POSITIVE:
        assert explicit_resolution(t), t
    for t in RESOLVED_NEGATIVE:
        assert not explicit_resolution(t), t


def test_resolution_in_records_uses_explicit_state_and_never_future_or_conditional_phrases():
    for t in ("Solana mainnet outage: engineers working until the issue is resolved", "Solana mainnet outage will be resolved soon, investigating",
              "Solana mainnet outage: investigating", "Solana mainnet outage identified, fix in progress"):
        r = rec(t, MEDIA_A)
        assert not r.resolution and r.polarity == "negative", t
        assert _a([r]).caution  # continua ativo (cautela), não foi "fechado"
    for t in ("Solana mainnet outage resolved, network operational again", "Solana issue resolved"):
        assert rec(t, MEDIA_A).resolution, t


def test_structured_status_state_decides_resolution_for_official_status_pages():
    investigating = rec("Solana Mainnet Beta Degraded Performance", status_state="investigating", summary="Investigating - We are investigating reports of delays")
    identified = rec("Solana Mainnet Beta Degraded Performance", status_state="identified")
    monitoring = rec("Solana Mainnet Beta Degraded Performance", status_state="monitoring")
    resolved = rec("Solana Mainnet Beta Degraded Performance", status_state="resolved", summary="Resolved - fixed. Investigating - earlier update")
    assert [r.resolution for r in (investigating, identified, monitoring, resolved)] == [False, False, False, True]
    assert resolved.polarity == "positive" and resolved.closed_episode and not investigating.closed_episode
    # texto com "will be resolved" NÃO sobrepõe o estado estruturado
    still = rec("Solana Mainnet outage", status_state="identified", summary="Identified - the issue will be resolved soon")
    assert not still.resolution


# ------------------------------------------------------------------------------------------------ P1.6 — negação, correção, desmentido
NOT_EVENTS = [  # o evento é NEGADO/desmentido: nada de alerta negativo
    "Solana not hacked, team says",
    "There was NO outage on Solana, rumors are false",
    "Solana denies hack reports",
    "Solana denies hack reports, says no funds were drained",
    "False report: Solana mainnet outage never happened",
    "Correction: earlier report of a Solana hack was wrong",
    "Solana network was not compromised",
    "No evidence of an exploit on Solana, investigators say",
    "Protocol refutes claims of a Solana exploit",
    "Solana outage claims dismissed as unconfirmed rumor",
    "Solana não foi hackeada, diz equipe",
    "Falso alarme: não houve queda na rede Solana",
]
STILL_EVENTS = [  # a negação é PARCIAL: o evento continua valendo
    "Exploit confirmed on Solana lending protocol; no funds were drained",
    "Solana mainnet outage hits validators; denies it was an attack",
    "Correction: Solana exploit losses were $5M, not $50M, drained funds confirmed",
    "Solana mainnet outage continues, no ETA",
]


def test_negation_correction_and_denial_matrix_does_not_create_negative_events():
    for t in NOT_EVENTS:
        r = rec(t, MEDIA_A)
        assert r.polarity != "negative" and r.impact == 0 and r.retraction, (t, r.polarity, r.impact, r.retraction, r.reasons)
        a = _a([r])
        assert not a.block and not a.caution and not a.alerts, t


def test_partial_negation_keeps_the_event():
    for t in STILL_EVENTS:
        r = rec(t, MEDIA_A)
        assert r.polarity == "negative" and not r.retraction and r.impact >= 3, (t, r.polarity, r.retraction, r.impact)


def test_confirmed_retraction_closes_earlier_claims_but_a_media_denial_does_not():
    claim = rec("Solana hacked, funds drained from major protocol", MEDIA_A, minutes_ago=60)
    assert _a([claim]).caution
    official_denial = rec("Solana team confirms: no hack occurred, earlier reports were false", SOLANA_STATUS, minutes_ago=10)
    assert official_denial.retraction and official_denial.certainty == "confirmed"
    a = _a([claim, official_denial])
    assert not a.block and a.alerts and "resolvido" in a.alerts[0].reason  # fechado por desmentido CONFIRMADO: sobra a janela de cautela
    media_denial = rec("Solana denies hack reports", MEDIA_A, minutes_ago=10)
    assert media_denial.certainty == "claim"
    assert _a([claim, media_denial]).caution  # desmentido não confirmado NÃO apaga a alegação anterior


def test_strip_negations_helper_removes_only_the_negated_span():
    text, removed = strip_negations("Exploit confirmed; no funds were drained")
    assert "Exploit confirmed" in text and "drained" not in text and removed


def test_positive_statements_are_not_treated_as_negations():
    for t in ("Solana upgrade launches, record inflows", "Exchange will list JUP", "Hackathon winners announced on Solana"):
        r = rec(t, MEDIA_A)
        assert not r.retraction and r.polarity in ("positive", "neutral"), t


# ------------------------------------------------------------------------------------------------ P1.5 — termos genéricos
def test_generic_words_do_not_create_incidents():
    for t in ("Researchers identified new use case for Solana in payments", "Solana Foundation identified as grant partner",
              "Regulators investigating the market structure of stablecoins", "Investigating the best Solana wallets for beginners"):
        r = rec(t, MEDIA_A)
        assert r.category != "incidente_rede", (t, r.category)
        a = _a([r])
        assert not a.block and not a.caution, t


def test_generic_attack_without_crypto_context_is_not_a_crypto_event():
    r = rec("Heart attack risk falls with new diet", MEDIA_A)
    assert r.category == "outros"
    assert rec("Hackers drain $40M from Solana lending protocol", MEDIA_A).category == "exploit_hack"


# ------------------------------------------------------------------------------------------------ P1.4 — ambiente e criticidade
def test_environment_detection_matrix():
    cases = {
        "Solana devnet degraded performance": "devnet",
        "Testnet cluster restarted": "testnet",
        "Solana Explorer degraded performance": "explorer",
        "Documentation site unavailable": "site",
        "Public RPC endpoints returning errors": "rpc",
        "Solana Mainnet Beta Degraded Performance": "mainnet",
        "Mainnet and Devnet unavailable": "mainnet",  # mainnet junto de outro ambiente prevalece (conservador)
        "Network outage": "desconhecido",
    }
    for title, env in cases.items():
        assert detect_environment(title, "") == env, title


def test_environment_and_severity_set_the_impact_and_only_critical_mainnet_blocks():
    mk = lambda t: rec(t, status_state="investigating", summary="Investigating - " + t)  # noqa: E731
    devnet, testnet, explorer = mk("Solana devnet outage"), mk("Solana testnet outage"), mk("Solana Explorer outage")
    assert devnet.impact == testnet.impact == explorer.impact == 1
    for r in (devnet, testnet, explorer):
        a = _a([r])
        assert not a.block and not a.caution, r.title  # impacto baixo: informativo, sem cautela nem bloqueio
    degraded = mk("Solana Mainnet Beta Degraded Performance")
    assert degraded.environment == "mainnet" and degraded.impact == 3 and degraded.severity == 3
    a = _a([degraded])
    assert a.caution and not a.block and a.risk_scale == 0.5  # degradação da mainnet: cautela (política padrão)
    assert _a([degraded], mainnet_degraded_policy="block").block  # política explícita: degradação = crítico
    halt = mk("Solana mainnet outage: block production halted")
    assert halt.impact == 4 and _a([halt]).block  # parada da mainnet em fonte oficial confirmada: bloqueia
    rpc = mk("Solana public RPC outage")
    assert rpc.environment == "rpc" and rpc.impact == 3 and not _a([rpc]).block
    maintenance = mk("Scheduled maintenance for Solana explorer")
    assert maintenance.impact == 1


# ------------------------------------------------------------------------------------------------ P1.2 — anos e datas
def test_years_mentioned_in_text_do_not_discard_a_fresh_news_item():
    for t in ("Largest Solana exploit since 2022 drains protocol, confirmed by team", "Solana outage echoes the 2021 and 2023 halts, validators offline",
              "Solana hacked: biggest loss since 2020, funds drained"):
        r = rec(t, SOLANA_STATUS)
        assert "antiga" not in r.flags and r.actionable, t
    assert _a([rec("Largest Solana exploit since 2022 drains protocol, confirmed by team", SOLANA_STATUS)]).block


def test_only_publication_date_can_make_an_item_old_or_unusable():
    from app.intel.models import RawItem

    # sem data confiável => sem_data (nunca usado); data no passado distante => o motor expira por TTL (não por ano no texto)
    raw = RawItem(MEDIA_A.id, "Solana mainnet outage", "", "https://m.example.com/x", None, NOW)
    assert "sem_data" in CLF.classify(raw, MEDIA_A, NOW).flags
    old = rec("Solana mainnet outage hits validators", SOLANA_STATUS, minutes_ago=60 * 24 * 5)
    assert not _a([old]).alerts  # expirou pela data de publicação


# ------------------------------------------------------------------------------------------------ P1.3 — Federal Reserve (nomes por extenso e abreviações)
FED_CASES = {
    "Federal Reserve issues FOMC statement": "FOMC_DECISION",
    "Federal Open Market Committee decides to maintain the target range": "FOMC_DECISION",
    "FOMC minutes released": "FOMC_MINUTES",
    "Federal Open Market Committee minutes released": "FOMC_MINUTES",
    "Minutes of the Federal Open Market Committee, September 15-16": "FOMC_MINUTES",
    "Fed holds interest rates steady": "FOMC_DECISION",
    "Interest rate decision: Federal Reserve raises rates by 25 basis points": "FOMC_DECISION",
    "Fed rate cut of 50 bps surprises markets": "FOMC_DECISION",
    "Monetary policy statement from the Federal Reserve": "FOMC_DECISION",
    "Federal funds rate target range unchanged": "FOMC_DECISION",
    "Fed Chair press conference after the Federal Reserve meeting": "FED_PRESSER",
    "Fed Chair speech on the economic outlook": "FED_SPEECH",
    "Speech by Governor on inflation, Federal Reserve Board": "FED_SPEECH",
}


def test_fed_events_are_recognized_by_abbreviation_and_full_name():
    for title, ind in FED_CASES.items():
        r = rec(title, FED, minutes_ago=5)
        assert r.category == "macro" and r.indicator == ind, (title, r.category, r.indicator)
    assert rec("Federal Reserve Board announces approval of application by bank", FED).category == "outros"  # comunicado do Fed que NÃO é política monetária


def test_us_indicators_are_recognized():
    cases = {"US CPI rises 0.3% in September": "CPI", "Core CPI cools to 3.1%": "CORE_CPI", "Core PCE price index rises 2.8%": "CORE_PCE", "PCE inflation steady": "PCE",
             "Producer Price Index unexpectedly falls": "PPI", "Nonfarm payrolls add 150,000 jobs": "NFP", "US unemployment rate rises to 4.4%": "UNEMPLOYMENT",
             "Initial jobless claims fall to 199,000": "JOBLESS_CLAIMS", "JOLTS job openings drop to 7.2 million": "JOLTS", "Employment Situation Summary": "EMPLOYMENT_SITUATION"}
    for t, ind in cases.items():
        assert rec(t, FED).indicator == ind, t


def test_macro_distinguishes_announced_facts_from_expectations_opinions_and_speculation():
    facts = ["Federal Reserve issues FOMC statement", "Fed raises rates by 25 basis points", "US CPI comes in hot at 3.4%", "Initial jobless claims fall to 199,000",
             "Nonfarm payrolls beat forecasts as unemployment rate falls"]
    not_facts = {"expectativa": ["Fed expected to cut rates next week", "What to expect from Wednesday's FOMC decision", "Traders bet on a Fed rate hike", "CPI preview: economists expect 0.3%",
                                 "Fed will announce its rate decision tomorrow"],
                 "opiniao": ["Analysts say the Fed should cut rates", "Strategists argue inflation outlook is worse"], "especulacao": ["Rumor: Fed may cut rates, unconfirmed"]}
    for t in facts:
        assert rec(t, MEDIA_A).nature == "fato", t
    for nature, titles in not_facts.items():
        for t in titles:
            r = rec(t, MEDIA_A)
            assert r.nature == nature, (t, r.nature)
    # expectativa/opinião/especulação sobre macro NÃO geram alerta; fato anunciado gera cautela (impacto alto)
    assert not _a([rec("Fed expected to cut rates next week", MEDIA_A, minutes_ago=5)]).alerts
    assert _a([rec("Federal Reserve issues FOMC statement", FED, minutes_ago=5)]).caution
