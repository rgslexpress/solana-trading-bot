"""Regressões derivadas de RESPOSTAS REAIS (relatório de diagnóstico de 2026-10-22, executado pelo usuário no Windows).

Só estão aqui fatos observados nesse relatório (títulos públicos, formato do iCalendar, ordem/colunas do CSV). Não são amostras completas das fontes."""

from datetime import datetime, timezone

from app.intel.classifier import Classifier
from app.intel.macro_sources import parse_ics, parse_treasury_csv
from app.intel.models import RawItem
from app.intel.sources import SourceConfig, SourceError
from _helpers import raises
from _intel import FED, NOW, SOLANA_STATUS, params, rec

UTC = timezone.utc
SPEECHES = SourceConfig("fed_speeches", "Fed discursos", "rss", "https://www.federalreserve.gov/feeds/speeches.xml", 0.95, official=True, domain="macro_us", default_indicator="FED_SPEECH")
BLS = SourceConfig("bls_calendar", "BLS", "ics", "https://www.bls.gov/schedule/news_release/bls.ics", 0.95, official=True, domain="macro_us")
CLF = Classifier([("SOL", ())], params())


def _speech(title):
    raw = RawItem(SPEECHES.id, title, "", "https://www.federalreserve.gov/newsevents/speech/x.htm", NOW, NOW)
    return CLF.classify(raw, SPEECHES, NOW)


# --- iCalendar REAL do BLS: 313 eventos, todos com DTSTART;TZID=US-Eastern (o parser inicial reconhecia 0)
REAL_SHAPE = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VTIMEZONE", "TZID:US-Eastern", "END:VTIMEZONE",
    "BEGIN:VEVENT", "SUMMARY:Employment Situation", "DTSTART;TZID=US-Eastern:20261106T083000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Job Openings and Labor Turnover Survey", "DTSTART;TZID=US-Eastern:20261208T100000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Consumer Price Index", "DTSTART;TZID=US-Eastern:20261112T083000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Producer Price Index", "DTSTART;TZID=US-Eastern:20261113T083000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Metropolitan Area Employment and Unemployment (Monthly)", "DTSTART;TZID=US-Eastern:20261118T100000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:State Job Openings and Labor Turnover Survey", "DTSTART;TZID=US-Eastern:20261218T100000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:The Employment Situation of Veterans", "DTSTART;TZID=US-Eastern:20261120T100000", "END:VEVENT",
    "END:VCALENDAR"])


def test_real_bls_calendar_shape_with_tzid_us_eastern_is_recognized_with_dst_aware_utc():
    events, ignored = parse_ics(REAL_SHAPE, BLS, NOW)
    by = {e.kind: e for e in events}
    assert set(by) == {"EMPLOYMENT_SITUATION", "JOLTS", "CPI", "PPI"} and ignored == 3  # só os 4 principais; "Metropolitan", "State..." e "...of Veterans" ficam de fora
    assert by["EMPLOYMENT_SITUATION"].scheduled_utc == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)  # 08:30 EST (UTC-5)
    assert by["JOLTS"].scheduled_utc == datetime(2026, 12, 8, 15, 0, tzinfo=UTC) and by["CPI"].confidence == "oficial" and by["CPI"].tz_original == "America/New_York"
    assert by["EMPLOYMENT_SITUATION"].indicators == ("NFP", "UNEMPLOYMENT")


def test_tzid_variants_are_accepted_and_unknown_zones_are_not():
    for tz in ("US-Eastern", "US/Eastern", "America/New_York", "EST5EDT"):
        text = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Consumer Price Index\nDTSTART;TZID={tz}:20260612T083000\nEND:VEVENT\nEND:VCALENDAR"
        ev, _ = parse_ics(text, BLS, NOW)
        assert ev and ev[0].scheduled_utc == datetime(2026, 6, 12, 12, 30, tzinfo=UTC), tz  # junho = EDT (UTC-4)
    ev, ign = parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Consumer Price Index\nDTSTART;TZID=Asia/Tokyo:20260612T083000\nEND:VEVENT\nEND:VCALENDAR", BLS, NOW)
    assert ev == [] and ign == 1


# --- CSV REAL do Tesouro: MAIS RECENTE PRIMEIRO, datas MM/DD/AAAA, colunas "1.5 Month", "2 Yr", "10 Yr"
REAL_CSV = ("Date,1 Mo,1.5 Month,2 Mo,3 Mo,4 Mo,6 Mo,1 Yr,2 Yr,3 Yr,5 Yr,7 Yr,10 Yr,20 Yr,30 Yr\n"
            "10/21/2026,4.30,4.31,4.32,4.33,4.34,4.35,4.60,4.81,4.90,5.00,5.08,5.13,5.50,5.40\n"
            "10/20/2026,4.30,4.31,4.32,4.33,4.34,4.35,4.58,4.79,4.88,4.99,5.07,5.12,5.49,5.39\n"
            "01/02/2026,4.0,4.0,4.0,4.0,4.0,4.0,4.0,3.5,3.6,3.7,3.8,4.2,4.8,4.7\n")


def test_real_treasury_csv_newest_first_gives_the_latest_day_and_the_change_in_bps():
    m = {x.indicator: x for x in parse_treasury_csv(REAL_CSV, "treasury", NOW)}
    assert m["UST_2Y"].obs_date == "2026-10-21" and m["UST_2Y"].value == 4.81 and abs(m["UST_2Y"].change_bps - 2.0) < 1e-9
    assert m["UST_10Y"].obs_date == "2026-10-21" and m["UST_10Y"].value == 5.13 and abs(m["UST_10Y"].change_bps - 1.0) < 1e-9  # NÃO usa a linha mais antiga (01/02/2026)
    with raises(SourceError):
        parse_treasury_csv("<html>erro</html>", "treasury", NOW)


# --- Títulos REAIS do Federal Reserve e do Solana Status
def test_real_fed_titles_are_classified_by_importance():
    cases = {"Federal Reserve issues FOMC statement": ("FOMC_DECISION", 3), "Minutes of the Federal Open Market Committee, September 15-16, 2026": ("FOMC_MINUTES", 2),
             "Minutes of the Board's discount rate meetings on August 17, September 9, and September 16,": ("FOMC_MINUTES", 2)}  # ata de desconto NÃO é decisão de juros
    for title, (ind, imp) in cases.items():
        r = rec(title, FED, minutes_ago=5)
        assert (r.category, r.indicator, r.impact) == ("macro", ind, imp), (title, r.indicator, r.impact)


def test_real_fed_speech_titles_have_no_speech_word_so_the_catalog_sets_the_type():
    for title in ("Bowman, Supervisory Modernization After Silicon Valley Bank", "Waller, The Signaling Value of the Summary of Economic Projections",
                  "Bowman, Modernizing the Regulatory and Supervisory Landscape"):
        r = _speech(title)
        assert (r.category, r.indicator, r.impact) == ("macro", "FED_SPEECH", 2), title  # impacto médio (informativo): discurso SOBRE o SEP não é decisão de juros


def test_real_solana_status_titles_environment_and_severity():
    r = rec("Public Endpoints and Explorer offline", SOLANA_STATUS, status_state="investigating", summary="Investigating - offline")
    assert r.category == "incidente_rede" and r.environment == "rpc" and r.impact == 3  # endpoints públicos fora: cautela (rpc), não "só explorer"
    r2 = rec("Cluster Instability", SOLANA_STATUS, status_state="investigating", summary="Investigating - cluster")
    assert r2.category == "incidente_rede" and r2.impact == 3


# --- Descrições REAIS do Solana Status (trechos do relatório de 2026-10-22 18:03 UTC): formato "<data> UTC <Estágio> - texto", MAIS RECENTE PRIMEIRO,
#     e o pubDate do item = instante da ÚLTIMA atualização (ex.: pubDate 15:09:24 = "Resolved" às 15:09 UTC).
import html as _html
from datetime import timedelta

from app.intel.engine import IntelEngine
from app.intel.sources import parse_feed
from app.intel.store import MemoryNewsView

REAL_INCIDENTS = [
    ("mb-020624", "Tue, 06 Feb 2024 15:09:24 +0000", "Feb 6 , 15:09 UTC Resolved - Block production on Solana mainnet beta resumed at 14:57 UTC, following a successful upgrade to v1.17.20 and a restart of the cluster by validator operators. "
     "Feb 6 , 10:22 UTC Investigating - Engineers from across the ecosystem are investigating an outage on mainnet-beta", 4, "mainnet"),
    ("Cluster Instability", "Sun, 26 Feb 2023 02:09:04 +0000", "Feb 26 , 02:09 UTC Resolved - This incident has been resolved. Feb 26 , 01:28 UTC Update - The Solana community successfully restarted the cluster - engineers will "
     "continue to monitor network performance Feb 25 , 21:45 UTC Investigating - the network attempts to recover. Core contributors of the protocol are working to identify the root cause of this issue.", 4, None),  # reinício do cluster = parada: crítico
    ("Public Endpoints and Explorer offline", "Sun, 08 Jan 2023 07:01:17 +0000", "Jan 8 , 07:01 UTC Resolved - The Public RPC and Explorer endpoints are online. Jan 8 , 04:53 UTC Identified - Mainnet beta Public RPC endpoints and "
     "Explorer are currently offline as RPC node software is upgraded. Block production has not been impacted and the Solana network remains online.", 3, "rpc"),
    ("Degraded Performance", "Sat, 01 Oct 2022 07:06:06 +0000", "Oct 1 , 07:06 UTC Resolved - Validator operators successfully completed a cluster restart of Mainnet Beta at 7 AM UTC. Oct 1 , 03:00 UTC Investigating - "
     "across the ecosystem are working on diagnosing the issue and more information will be provided as it becomes available.", 4, "mainnet"),  # título "Degraded", mas o texto descreve reinício do cluster (parada): crítico
    ("Mainnet Beta Clock Drift", "Mon, 06 Jun 2022 16:32:07 +0000", "Jun 6 , 16:32 UTC Resolved - This incident has been resolved. May 26 , 03:34 UTC Monitoring - On-chain time continues to run behind that of wall clocks, "
     "due to longer-than-normal block times. While this has no impact on performance or network operations, the time reported by block explorers and dapps may not reflect wall-clock time.", 2, "mainnet"),
    ("Mainnet Beta Outage", "Wed, 01 Jun 2022 21:06:03 +0000", "Jun 1 , 21:06 UTC Resolved - Validator operators successfully completed a cluster restart of Mainnet Beta at 9:00 PM UTC, following a roughly 4 and a half hour outage "
     "after the network failed to reach consensus. Jun 1 , 16:30 UTC Investigating - Mainnet Beta has halted. Validator operators should prepare for a restart in mb-validators on Discord.", 4, "mainnet"),
]


def _statuspage_feed():
    items = "".join(f"<item><title>{t}</title><link>https://status.solana.com/incidents/{i}</link><description>{_html.escape('<p>' + d + '</p>')}</description><pubDate>{pub}</pubDate></item>"
                    for i, (t, pub, d, _imp, _env) in enumerate(REAL_INCIDENTS))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Solana Status - Incident History</title>{items}</channel></rss>'


def test_real_statuspage_descriptions_are_all_detected_as_resolved_by_the_structured_state():
    """O relatório mostrou 7/25 'não resolvidos' porque o SCRIPT de diagnóstico não passava status_format; com o catálogo real (statuspage) todos têm o marcador 'Resolved'."""
    items, bad = parse_feed(_statuspage_feed(), SOLANA_STATUS, NOW, 100)
    assert bad == 0 and len(items) == 6 and all(i.status_state == "resolved" for i in items)
    for raw, (title, _pub, _d, impact, env) in zip(items, REAL_INCIDENTS):
        r = CLF.classify(raw, SOLANA_STATUS, NOW)
        assert r.resolution and r.closed_episode and r.polarity == "positive", title
        assert r.impact == impact, (title, r.impact)
        if env:
            assert r.environment == env, (title, r.environment)


def test_real_clock_drift_with_no_impact_is_informational_not_a_caution():
    r = rec("Mainnet Beta Clock Drift", SOLANA_STATUS, status_state="monitoring",
            summary="Monitoring - On-chain time continues to run behind wall clocks. While this has no impact on performance or network operations, the time reported by explorers may not reflect wall-clock time.")
    assert r.impact == 2 and not r.resolution
    a = IntelEngine(params(), MemoryNewsView([r])).assess("JUP", NOW, "BUY")
    assert not a.caution and not a.block  # a própria fonte diz "sem impacto": nenhum risco reduzido
    outage = rec("Mainnet Beta Outage", SOLANA_STATUS, status_state="investigating", summary="Investigating - Mainnet Beta has halted. Validator operators should prepare for a restart.")
    assert outage.impact == 4 and IntelEngine(params(), MemoryNewsView([outage])).assess("JUP", NOW, "BUY").block  # parada real continua bloqueando


def test_real_pubdate_is_the_latest_update_time_so_an_active_incident_keeps_its_start_in_the_text_not_in_the_date():
    """pubDate real = instante da última atualização. Um incidente ATIVO (sem 'Resolved') bloqueia a partir do pubDate e dura até resolver/TTL."""
    active_desc = "Feb 6 , 10:22 UTC Investigating - Engineers from across the ecosystem are investigating an outage on mainnet-beta"
    items = f'<item><title>mb-020624</title><link>https://status.solana.com/incidents/x</link><description>{_html.escape("<p>" + active_desc + "</p>")}</description><pubDate>Tue, 06 Feb 2024 10:22:00 +0000</pubDate></item>'
    parsed, _ = parse_feed(f'<rss version="2.0"><channel>{items}</channel></rss>', SOLANA_STATUS, NOW, 10)
    assert parsed[0].status_state == "investigating"
    now = parsed[0].published_at + timedelta(minutes=30)
    raw_rec = CLF.classify(parsed[0], SOLANA_STATUS, now)
    raw_rec.collected_at = (parsed[0].published_at + timedelta(minutes=1)).isoformat()
    a = IntelEngine(params(), MemoryNewsView([raw_rec])).assess("JUP", now, "BUY")
    assert a.block and a.alerts[0].impact == 4
