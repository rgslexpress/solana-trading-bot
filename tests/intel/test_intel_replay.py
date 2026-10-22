"""Replay/backtest/shadow: SEM vazamento de futuro e sem afirmar melhora de rentabilidade."""

import json
import os
import tempfile
from datetime import timedelta

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestEngine, TokenData
from app.config import Settings
from app.intel.engine import IntelEngine
from app.intel.replay import IntelReplay, load_archive
from app.intel.shadow import AVISO, compare_backtests, live_shadow_report
from app.intel.store import MemoryNewsView
from app.storage.database import Database
from _helpers import JUP, make_candles, trend_series
from _intel import CLF, MEDIA_A, NOW, SOLANA_STATUS, params, rec

BUY_BAR = 127


def _settings():
    s = Settings()
    s.strategy.ema_trend = 20
    return s


def _data():
    entry = make_candles(trend_series(n_flat=160, n_up=60), 900)
    htf = make_candles(np.linspace(80, 140, 240), 3600)
    return {"JUP": TokenData(JUP, entry, htf)}, entry


def _close_time(entry, i):
    return (entry.index[i] + pd.Timedelta(seconds=900)).to_pydatetime()


def test_memory_view_never_returns_items_published_or_collected_after_the_instant():
    r_future_pub = rec("Solana outage", minutes_ago=-30)  # publicada no futuro
    r_late_collect = rec("Solana outage tardio", minutes_ago=60, collected_lag_min=120)  # publicada há 1 h, mas só coletada daqui a 1 h
    r_ok = rec("Solana outage ok", minutes_ago=10)
    v = MemoryNewsView([r_future_pub, r_late_collect, r_ok])
    seen = {r.title for r in v.records(NOW)}
    assert seen == {"Solana outage ok"}
    assert {r.title for r in v.records(NOW + timedelta(hours=2))} == {"Solana outage ok", "Solana outage tardio", "Solana outage"}


def test_decision_changes_exactly_when_the_news_becomes_available_not_when_it_was_published():
    pub = NOW - timedelta(minutes=30)
    r = rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, published=pub, collected_lag_min=40)  # disponível 10 min depois de NOW
    e = IntelEngine(params(), MemoryNewsView([r]))
    assert not e.assess("JUP", NOW).block  # publicada, mas ainda não coletada/disponível
    assert e.assess("JUP", NOW + timedelta(minutes=11)).block
    assert not e.assess("JUP", NOW - timedelta(hours=2)).block


def test_archive_loading_applies_latency_discards_bad_lines_and_never_precedes_publication():
    pub = NOW - timedelta(hours=1)
    lines = [json.dumps({"source_id": "media_a", "title": "Solana network outage", "url": "https://a.example.com/1", "published_at": pub.isoformat()}),
             json.dumps({"source_id": "media_a", "title": "Sem data", "url": "https://a.example.com/2"}), "{ não é json", "", json.dumps({"title": "Sem url", "published_at": pub.isoformat()}),
             json.dumps({"source_id": "media_a", "title": "Link ruim", "url": "javascript:alert(1)", "published_at": pub.isoformat()}),
             json.dumps({"source_id": "media_a", "title": "Coletada antes de publicada", "url": "https://a.example.com/3", "published_at": pub.isoformat(), "collected_at": (pub - timedelta(hours=5)).isoformat()})]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "n.jsonl")
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        p = params(assumed_latency_s=900)
        recs = load_archive(path, CLF, {"media_a": MEDIA_A}, p)
    assert len(recs) == 2
    first = next(r for r in recs if r.title == "Solana network outage")
    assert first.collected_dt == pub + timedelta(seconds=900)  # latência presumida
    fixed = next(r for r in recs if r.title.startswith("Coletada antes"))
    assert fixed.collected_dt >= fixed.published_dt  # nunca disponível antes da publicação


def test_backtest_with_gate_blocks_entry_only_when_news_was_available_at_decision_time():
    data, entry = _data()
    s = _settings()
    base = BacktestEngine(s, data, 5e6).run()
    assert base.metrics["num_trades"] >= 1 and base.trades.iloc[0]["entry_ts"].startswith(str(entry.index[BUY_BAR + 1])[:16].replace(" ", "T"))
    crit_before = rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, minutes_ago=20, now=_close_time(entry, BUY_BAR))
    gate = IntelReplay([crit_before], s.intel).gate
    blocked = BacktestEngine(s, data, 5e6, intel_gate=gate).run()
    first_buy_trade = base.trades.iloc[0]["entry_ts"]
    assert all(t != first_buy_trade for t in blocked.trades["entry_ts"])  # a entrada do BUY #127 foi vetada
    assert any(e["bloqueou"] for e in blocked.intel_events) and blocked.rejection_counts.get("notícias: bloqueio de entrada", 0) >= 1
    # a MESMA notícia publicada DEPOIS da decisão não pode ter efeito sobre ela (sem vazamento de futuro)
    crit_after = rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, published=_close_time(entry, BUY_BAR) + timedelta(minutes=30))
    after = BacktestEngine(s, data, 5e6, intel_gate=IntelReplay([crit_after], s.intel).gate).run()
    assert first_buy_trade in set(after.trades["entry_ts"])
    # determinismo
    again = BacktestEngine(s, data, 5e6, intel_gate=gate).run()
    assert again.metrics["final_equity"] == blocked.metrics["final_equity"]


def test_backtest_gate_can_only_remove_or_shrink_entries_never_add():
    data, entry = _data()
    s = _settings()
    base = BacktestEngine(s, data, 5e6).run()
    news = [rec("FOMC raises interest rates", SOLANA_STATUS.__class__("fed", "Fed", "rss", "https://fed.example.com/r", 0.95, official=True), minutes_ago=20, now=_close_time(entry, BUY_BAR)),
            rec("Spot ETF approved for Solana, record inflows", MEDIA_A, minutes_ago=15, now=_close_time(entry, BUY_BAR))]
    with_news = BacktestEngine(s, data, 5e6, intel_gate=IntelReplay(news, s.intel).gate).run()
    assert with_news.metrics["num_trades"] <= base.metrics["num_trades"]
    base_entries, news_entries = set(base.trades["entry_ts"]), set(with_news.trades["entry_ts"])
    assert news_entries <= base_entries | set()  # nenhuma entrada nova criada por notícia (positiva ou não)
    cautioned = [e for e in with_news.intel_events if not e["bloqueou"]]
    assert cautioned and all(e["escala"] <= 1.0 for e in cautioned)


def test_compare_backtests_reports_both_runs_blocked_counts_and_never_claims_improvement():
    data, entry = _data()
    s = _settings()
    crit = rec("Mainnet outage: validators offline, investigating", SOLANA_STATUS, minutes_ago=20, now=_close_time(entry, BUY_BAR))
    res = compare_backtests(s, data, 5e6, IntelReplay([crit], s.intel))
    assert set(res) >= {"sem_noticias", "com_noticias", "diferenca", "entradas_bloqueadas", "aviso"} and res["entradas_bloqueadas"] >= 1
    assert res["aviso"] == AVISO and "não é evidência" in res["aviso"].lower()
    blob = json.dumps(res, default=str, ensure_ascii=False).lower()
    assert "melhora" not in blob.replace("não é evidência de melhora", "")  # o relatório não declara melhora


def test_live_shadow_report_groups_would_change_decisions_and_their_outcomes():
    db = Database(":memory:")
    from app.domain import Position
    from app.risk.stops import initial_stop
    from app.config import RiskParams

    for pid, pnl in (("p1", -5.0), ("p2", 3.0)):
        pos = Position(pid, JUP.mint, "JUP", 1.0, 100.0, "2025-01-01T00:00:00+00:00", 0.1, initial_stop(100.0, 2.0, RiskParams()), status="CLOSED", pnl_quote=pnl)
        db.save_position(pos)
    base = dict(ts="t", symbol="JUP", mint=JUP.mint, candle_ts="c", technical="BUY", mode="shadow", data_state="ok", risk_scale=1.0, divergence=None, alerts=[], rationale=[])
    db.conn.row_factory = __import__("sqlite3").Row
    from app.intel.store import IntelStore

    st = IntelStore(db.conn)
    d1 = st.add_decision({**base, "action": "SHADOW: bloquearia", "would_change": True, "applied": False})
    d2 = st.add_decision({**base, "action": "sem efeito", "would_change": False, "applied": False})
    st.link_position(d1, "p1")
    st.link_position(d2, "p2")
    rep = live_shadow_report(db.conn)
    assert rep["mudaria"]["pnl_liquido_total"] == -5.0 and rep["sem_mudanca"]["pnl_liquido_total"] == 3.0 and rep["bloquearia"] == 1 and "aviso" in rep
