"""Macro EUA — fuso horário/horário de verão, calendário, iCalendar do BLS, valores do BLS e CSV do Tesouro.

SIMULADO: amostras sintéticas no formato descrito pela documentação. NENHUMA destas fontes foi acessada no ambiente de desenvolvimento.
A conversão de horário é verificada contra o `zoneinfo` do sistema quando ele existe (verificação independente de um banco de fusos)."""

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

from app.config import IntelParams
from app.http import HttpResponse
from app.intel.macro import MacroStore, load_calendar, make_event
from app.intel.macro_sources import fetch_macro, parse_bls_series, parse_ics, parse_treasury_csv
from app.intel.sources import SourceConfig, SourceError
from app.intel.ustime import dst_bounds_utc, et_to_utc, to_utc, utc_to_et
from app.storage.database import Database
from _helpers import raises
from _intel import FakeFeeds, NOW, ok_xml

UTC = timezone.utc


def _zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - sem banco de fusos (ex.: Windows sem tzdata)
        return None


# ------------------------------------------------------------------------------------------------ fuso e horário de verão
def test_dst_transitions_2026_are_exact():
    start, end = dst_bounds_utc(2026)
    assert start == datetime(2026, 3, 8, 7, 0, tzinfo=UTC) and end == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 2º dom. de março / 1º dom. de novembro
    assert [dst_bounds_utc(y)[0].day for y in (2024, 2025, 2027, 2028)] == [10, 9, 14, 12] and [dst_bounds_utc(y)[1].day for y in (2024, 2025, 2027, 2028)] == [3, 2, 7, 5]


def test_release_times_around_the_spring_forward_and_fall_back_dates():
    # Sexta antes da troca (EST, UTC-5) x segunda depois (EDT, UTC-4): o MESMO horário local vira UTC diferente
    assert et_to_utc(datetime(2026, 3, 6, 8, 30)) == datetime(2026, 3, 6, 13, 30, tzinfo=UTC)
    assert et_to_utc(datetime(2026, 3, 9, 8, 30)) == datetime(2026, 3, 9, 12, 30, tzinfo=UTC)
    assert et_to_utc(datetime(2026, 10, 30, 8, 30)) == datetime(2026, 10, 30, 12, 30, tzinfo=UTC)  # EDT
    assert et_to_utc(datetime(2026, 11, 2, 8, 30)) == datetime(2026, 11, 2, 13, 30, tzinfo=UTC)  # EST
    # FOMC 28/10/2026 14:00 ET (ainda EDT) = 18:00 UTC; ata em 18/11 14:00 ET (EST) = 19:00 UTC
    assert make_event("FOMC_DECISION", "2026-10-28", "14:00", "America/New_York", source_id="t").scheduled_utc == datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    assert make_event("FOMC_MINUTES", "2026-11-18", "14:00", "America/New_York", source_id="t").scheduled_utc == datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
    # nenhum offset fixo: o mesmo "08:30 ET" difere em 1 h entre verão e inverno
    assert (et_to_utc(datetime(2026, 12, 3, 8, 30)) - et_to_utc(datetime(2026, 7, 2, 8, 30))).total_seconds() % 86400 == 3600


def test_nonexistent_and_ambiguous_local_times_are_handled_explicitly():
    with raises(ValueError, "inexistente"):
        et_to_utc(datetime(2026, 3, 8, 2, 30))  # o relógio salta de 02:00 para 03:00
    first, second = et_to_utc(datetime(2026, 11, 1, 1, 30), 0), et_to_utc(datetime(2026, 11, 1, 1, 30), 1)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC) and second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 ocorre duas vezes
    with raises(ValueError, "inexistente"):
        make_event("FOMC_DECISION", "2026-03-08", "02:30", "America/New_York", source_id="t")


def test_supported_range_and_unknown_timezones_fail_instead_of_guessing():
    with raises(ValueError):
        dst_bounds_utc(2006)
    with raises(ValueError):
        dst_bounds_utc(2100)
    with raises(ValueError, "fuso não suportado"):
        to_utc(datetime(2026, 1, 1, 8, 30), "Europe/London")
    assert to_utc(datetime(2026, 1, 1, 8, 30), "UTC") == datetime(2026, 1, 1, 8, 30, tzinfo=UTC)


def test_conversion_matches_the_system_zoneinfo_for_every_half_hour_around_all_transitions_2007_2050():
    z = _zone()
    if z is None:
        return  # sem tzdata no sistema: a verificação independente não pode ser feita aqui (os casos fixos acima continuam valendo)
    for y in range(2007, 2051):
        s, e = dst_bounds_utc(y)
        for base in (s, e):
            t = base - timedelta(hours=30)
            while t < base + timedelta(hours=30):
                assert utc_to_et(t) == t.astimezone(z).replace(tzinfo=None), t
                t += timedelta(minutes=30)
    for y in (2007, 2015, 2026, 2038, 2050):  # local -> UTC (inclui horários inexistentes e ambíguos)
        for m, d in ((3, 7), (3, 8), (3, 9), (11, 1), (11, 2), (6, 15), (12, 25)):
            for h in range(24):
                for mi in (0, 30):
                    loc = datetime(y, m, d, h, mi)
                    z0 = loc.replace(tzinfo=z, fold=0).astimezone(UTC)
                    exists = z0.astimezone(z).replace(tzinfo=None) == loc
                    try:
                        u = et_to_utc(loc, 0)
                    except ValueError:
                        assert not exists, loc
                        continue
                    assert exists and u == z0 and et_to_utc(loc, 1) == loc.replace(tzinfo=z, fold=1).astimezone(UTC), loc


# ------------------------------------------------------------------------------------------------ calendário do operador
def test_calendar_file_builds_events_with_original_zone_normalized_utc_and_confidence():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cal.json")
        json.dump({"events": [{"kind": "FOMC_DECISION", "date": "2026-10-28", "time": "14:00", "tz": "America/New_York", "confidence": "oficial", "source": "https://x.example/fomc"}],
                   "recurring": [{"kind": "JOBLESS_CLAIMS", "weekday": 3, "time": "08:30", "tz": "America/New_York", "from": "2026-10-29", "to": "2026-11-12"}]}, open(path, "w"))
        ev = load_calendar(path)
        fomc = next(e for e in ev if e.kind == "FOMC_DECISION")
        assert fomc.scheduled_utc == datetime(2026, 10, 28, 18, 0, tzinfo=UTC) and fomc.tz_original == "America/New_York" and fomc.local_time == "2026-10-28 14:00"
        assert fomc.confidence == "oficial" and fomc.importance == 3 and fomc.region == "US" and fomc.source_url == "https://x.example/fomc"
        claims = [e for e in ev if e.kind == "JOBLESS_CLAIMS"]
        # quintas 29/10 (EDT), 05/11 e 12/11 (EST): 08:30 local permanece, o UTC muda na troca de horário
        assert [e.scheduled_utc.isoformat() for e in claims] == ["2026-10-29T12:30:00+00:00", "2026-11-05T13:30:00+00:00", "2026-11-12T13:30:00+00:00"]
        assert all(e.confidence == "estimado" for e in claims)  # recorrência = estimativa (feriados deslocam o dia)


def test_calendar_recurrence_skips_nonexistent_local_times_and_honors_exclusions():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cal.json")
        json.dump({"recurring": [{"kind": "JOBLESS_CLAIMS", "weekday": 6, "time": "02:30", "from": "2026-03-01", "to": "2026-03-15"},  # domingo 02:30 não existe em 08/03
                                 {"kind": "JOBLESS_CLAIMS", "weekday": 3, "time": "08:30", "from": "2026-11-19", "to": "2026-12-03", "exclude": ["2026-11-26"]}]}, open(path, "w"))
        ev = load_calendar(path)
        sundays = [e for e in ev if e.local_time.endswith("02:30")]
        assert [e.local_time[:10] for e in sundays] == ["2026-03-01", "2026-03-15"]
        thursdays = [e.local_time[:10] for e in ev if e.local_time.endswith("08:30")]
        assert thursdays == ["2026-11-19", "2026-12-03"]


def test_invalid_calendar_entries_fail_closed():
    with tempfile.TemporaryDirectory() as d:
        for bad in ({"events": [{"kind": "NOPE", "date": "2026-10-28", "time": "14:00"}]}, {"events": [{"kind": "CPI", "date": "28/10/2026", "time": "08:30"}]},
                    {"events": [{"kind": "CPI", "date": "2026-10-28", "time": "08:30", "confidence": "talvez"}]}, {"recurring": [{"kind": "CPI", "weekday": 9, "time": "08:30", "from": "2026-01-01", "to": "2026-02-01"}]},
                    {"events": [{"kind": "CPI", "date": "2026-10-28", "time": "08:30", "tz": "Mars/Base"}]}):
            path = os.path.join(d, "cal.json")
            json.dump(bad, open(path, "w"))
            with raises(ValueError):
                load_calendar(path)
        assert load_calendar(os.path.join(d, "nao_existe.json")) == []


def test_official_schedule_replaces_estimated_event_of_the_same_kind_and_day():
    db = Database(":memory:")
    st = MacroStore(db.conn)
    est = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="calendario_manual", confidence="estimado")
    off = make_event("CPI", "2026-11-12", "08:30", "America/New_York", source_id="bls_calendar", confidence="oficial")
    assert st.upsert_event(est, NOW) and len(st.events()) == 1
    assert st.upsert_event(off, NOW) and [e.confidence for e in st.events()] == ["oficial"]
    assert not st.upsert_event(est, NOW)  # o estimado não volta a entrar depois que existe um oficial
    assert not st.upsert_event(off, NOW)  # idempotente


# ------------------------------------------------------------------------------------------------ iCalendar (BLS)
ICS_SRC = SourceConfig("bls_calendar", "BLS", "ics", "https://www.bls.gov/schedule/news_release/bls.ics", 0.95, official=True, domain="macro_us")
ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VEVENT", "SUMMARY:Employment Situation", "DTSTART;TZID=America/New_York:20261106T083000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Consumer Price Index", "DTSTART:20261112T133000Z", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Producer Price Index", "DTSTART;VALUE=DATE:20261113", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Job Openings and Labor Turnover Survey", "DTSTART;TZID=Europe/Paris:20261110T150000", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Real Earnings", "DTSTART:20261112T133000Z", "END:VEVENT",
    "BEGIN:VEVENT", "SUMMARY:Employment ", " Situation (folded line)", "DTSTART;TZID=US/Eastern:20261204T083000", "END:VEVENT",
    "END:VCALENDAR"])


def test_ics_parsing_handles_utc_tzid_all_day_unknown_zone_unknown_summary_and_folded_lines():
    events, ignored = parse_ics(ICS, ICS_SRC, NOW)
    by = {(e.kind, e.local_time[:10]): e for e in events}
    nfp = by[("EMPLOYMENT_SITUATION", "2026-11-06")]
    assert nfp.scheduled_utc == datetime(2026, 11, 6, 13, 30, tzinfo=UTC) and nfp.confidence == "oficial" and nfp.indicators == ("NFP", "UNEMPLOYMENT")  # 08:30 EST
    cpi = by[("CPI", "2026-11-12")]
    assert cpi.scheduled_utc == datetime(2026, 11, 12, 13, 30, tzinfo=UTC) and cpi.confidence == "oficial" and cpi.tz_original == "America/New_York" and cpi.local_time.endswith("08:30")
    ppi = by[("PPI", "2026-11-13")]
    assert ppi.confidence == "estimado" and ppi.local_time.endswith("08:30")  # data sem horário: 08:30 ET é só ESTIMATIVA e fica marcada
    assert ("JOLTS", "2026-11-10") not in by  # fuso desconhecido (Paris): não adivinha
    assert ("EMPLOYMENT_SITUATION", "2026-12-04") in by  # linha dobrada reconstruída; "US/Eastern" aceito
    assert ignored == 2  # JOLTS (fuso não suportado) + "Real Earnings" (tipo não acompanhado)


def test_ics_rejects_non_calendar_documents():
    with raises(SourceError, "iCalendar"):
        parse_ics("<html>captcha</html>", ICS_SRC, NOW)


# ------------------------------------------------------------------------------------------------ BLS (valores)
def _bls(points, status="REQUEST_SUCCEEDED", message=None):
    return {"status": status, "message": message or [], "Results": {"series": [{"seriesID": "X", "data": [{"year": str(y), "period": f"M{m:02d}", "value": str(v)} for (y, m), v in points]}]}}


def test_bls_levels_diffs_and_yoy_are_computed_without_inventing_forecasts():
    t = NOW
    unemp = parse_bls_series(_bls([((2026, 9), 4.4), ((2026, 8), 4.3), ((2026, 7), 4.2)]), "LNS14000000", "bls", t)
    assert (unemp.indicator, unemp.period, unemp.actual, unemp.previous, unemp.forecast, unemp.revised_previous) == ("UNEMPLOYMENT", "2026-09", 4.4, 4.3, None, None)
    assert "N/D" in unemp.note  # a ausência de expectativa é explícita
    nfp = parse_bls_series(_bls([((2026, 9), 159_300), ((2026, 8), 159_150), ((2026, 7), 159_000)]), "CES0000000001", "bls", t)
    assert (nfp.actual, nfp.previous, nfp.indicator) == (150, 150, "NFP")  # variação mensal em milhares
    pts = [((2025, m), 300.0) for m in range(1, 13)] + [((2026, 8), 309.0), ((2026, 9), 309.9)]
    cpi = parse_bls_series(_bls(pts), "CUUR0000SA0", "bls", t)
    assert cpi.indicator == "CPI" and abs(cpi.actual - 3.3) < 1e-6 and abs(cpi.previous - 3.0) < 1e-6
    assert parse_bls_series(_bls([((2026, 9), 5.0)]), "CUUR0000SA0", "bls", t) is None  # sem dado do ano anterior: não calcula a/a


def test_bls_error_responses_are_classified():
    with raises(SourceError, "REQUEST_NOT_PROCESSED") as _:
        parse_bls_series(_bls([], "REQUEST_NOT_PROCESSED", ["daily threshold exceeded"]), "LNS14000000", "bls", NOW)
    try:
        parse_bls_series(_bls([], "REQUEST_NOT_PROCESSED", ["daily threshold for total number of requests allocated"]), "LNS14000000", "bls", NOW)
    except SourceError as exc:
        assert exc.kind == "rate_limit"
    for bad in ("<html>", None, {"status": "REQUEST_SUCCEEDED"}):
        with raises(SourceError):
            parse_bls_series(bad, "LNS14000000", "bls", NOW)


def test_fetch_macro_bls_makes_one_get_per_series_and_validates_json():
    src = SourceConfig("bls_values", "BLS", "bls_api", "https://api.bls.gov/publicAPI/v1/timeseries/data/", 0.95, official=True, series=("LNS14000000", "CES0000000001"), domain="macro_us", daily_cap=20)
    routes = {src.url + "LNS14000000": HttpResponse(200, _bls([((2026, 9), 4.4), ((2026, 8), 4.3)]), {}), src.url + "CES0000000001": HttpResponse(200, _bls([((2026, 9), 100), ((2026, 8), 50), ((2026, 7), 10)]), {})}
    t = FakeFeeds(routes)
    vals = fetch_macro(t, src, IntParams(), NOW)
    assert {v.indicator for v in vals} == {"UNEMPLOYMENT", "NFP"} and [c[0] for c in t.calls] == [src.url + "LNS14000000", src.url + "CES0000000001"]
    t2 = FakeFeeds({src.url + "LNS14000000": ok_xml("<html>erro</html>")})
    with raises(SourceError, "não é JSON"):
        fetch_macro(t2, src, IntParams(), NOW)


def IntParams():  # noqa: N802
    return IntelParams()


# ------------------------------------------------------------------------------------------------ Tesouro (CSV)
CSV = "﻿Date,\"1 Mo\",\"2 Yr\",\"10 Yr\",\"30 Yr\"\n10/20/2026,4.10,3.90,4.20,4.50\n10/21/2026,4.11,3.95,4.30,4.55\n10/22/2026,4.12,N/A,4.40,4.60\n"


def test_treasury_csv_parsing_variation_in_bps_and_missing_values():
    m = {x.indicator: x for x in parse_treasury_csv(CSV, "treasury", NOW)}
    assert m["UST_10Y"].obs_date == "2026-10-22" and m["UST_10Y"].value == 4.40 and m["UST_10Y"].prev_value == 4.30 and abs(m["UST_10Y"].change_bps - 10.0) < 1e-6
    assert m["UST_2Y"].obs_date == "2026-10-21" and m["UST_2Y"].value == 3.95 and abs(m["UST_2Y"].change_bps - 5.0) < 1e-6  # N/A ignorado: usa o último dia com dado
    iso = parse_treasury_csv("Date,2 Yr,10 Yr\n2026-10-21,3.9,4.2\n2026-10-22,3.8,4.3\n", "t", NOW)
    assert {x.indicator: x.obs_date for x in iso} == {"UST_2Y": "2026-10-22", "UST_10Y": "2026-10-22"}
    for bad in ("", "Foo,Bar\n1,2\n"):
        with raises(SourceError):
            parse_treasury_csv(bad, "t", NOW)
    assert date(2026, 10, 22).isoformat() == "2026-10-22"
