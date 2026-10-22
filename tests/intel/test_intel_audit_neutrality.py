"""P0.1 — A auditoria é SÓ observação: falha de gravação (disco, permissão, banco travado) não pode alterar a decisão técnica de trading.

Evidência SIMULADA (falhas injetadas no armazenamento); nenhuma fonte real é usada."""

import errno
import sqlite3
import tempfile


from test_intel_integration import _build, _critical, _positions, _run, _rows

FAILURES = [
    ("disco cheio", OSError(errno.ENOSPC, "No space left on device")),
    ("permissão negada", PermissionError(errno.EACCES, "Permission denied")),
    ("banco travado", sqlite3.OperationalError("database is locked")),
    ("erro de E/S", sqlite3.DatabaseError("disk I/O error")),
    ("bug inesperado", RuntimeError("falha qualquer")),
]


def _signature(db):
    pos = [(round(p.qty, 9), round(p.entry_price, 9), p.status, round(p.pnl_quote or 0, 6)) for p in _positions(db)]
    sig = [tuple(r) for r in db.conn.execute("SELECT candle_ts,type,score FROM signals WHERE candle_ts NOT LIKE '%#%' ORDER BY candle_ts")]
    return pos, sig


def _run_case(mode, *, news=False, add_decision_error=None, link_error=None, marker_error=False):
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, mode)
        if news:
            store.insert(_critical(entry))
        if add_decision_error is not None:
            def boom(*a, **k):
                raise add_decision_error
            svc.intel.store.add_decision = boom
        if link_error is not None:
            def boom2(*a, **k):
                raise link_error
            svc.intel.store.link_position = boom2
        if marker_error:
            real = db.save_signal

            def flaky(mint, sym, candle_ts, *a, **k):
                if "#noticias" in candle_ts:
                    raise sqlite3.OperationalError("database is locked")
                return real(mint, sym, candle_ts, *a, **k)
            db.save_signal = flaky
        acts = _run(svc, clock, entry)
        return _signature(db), acts, svc.intel, _rows(db, "SELECT COUNT(*) c FROM intel_decisions")[0]["c"]


def test_shadow_decision_is_identical_with_audit_working_and_with_audit_unavailable():
    """Com ou sem evento crítico: a decisão de trading em SHADOW é idêntica ao baseline `off`, com a auditoria funcionando ou quebrada."""
    for news in (False, True):
        base, _, _, _ = _run_case("off", news=False)
        ok_sig, _, rt_ok, n_ok = _run_case("shadow", news=news)
        assert ok_sig == base and rt_ok.audit_failures == 0 and n_ok >= 1  # auditoria funcionando: decisão idêntica e registro gravado
        for label, err in FAILURES:
            bad_sig, acts, rt, n = _run_case("shadow", news=news, add_decision_error=err)
            assert bad_sig == base, f"{label}: a falha de auditoria alterou a decisão em shadow"
            assert n == 0 and rt.audit_failures >= 1 and rt.last_audit_error  # a falha foi CONTADA e visível, não engolida em silêncio
            assert any("AUDITORIA DE NOTÍCIAS INDISPONÍVEL" in a for a in acts)  # aparece no relatório do ciclo (e, portanto, no painel)


def test_shadow_decision_is_identical_when_linking_the_position_fails():
    base, _, _, _ = _run_case("off")
    sig, _, rt, _ = _run_case("shadow", link_error=sqlite3.OperationalError("database is locked"))
    assert sig == base and rt.audit_failures >= 1 and len(base[0]) >= 1  # a entrada aconteceu mesmo assim


def test_enforce_block_still_applies_when_the_audit_or_the_marker_cannot_be_written():
    """Falha de auditoria não REMOVE uma restrição: em enforce, o bloqueio por evento crítico continua valendo."""
    base_blocked, _, _, _ = _run_case("enforce", news=True)
    assert base_blocked[0] == []  # sem entrada (bloqueada)
    for kw in (dict(add_decision_error=OSError(errno.ENOSPC, "cheio")), dict(marker_error=True), dict(add_decision_error=PermissionError("negado"), marker_error=True)):
        sig, _, rt, _ = _run_case("enforce", news=True, **kw)
        assert sig[0] == [], f"o bloqueio deixou de valer com {kw}"
        assert rt.audit_failures >= 1


def test_record_decision_never_raises_and_counts_failures():
    with tempfile.TemporaryDirectory() as d:
        svc, db, store, clock, entry = _build(d, "shadow")
        rt = svc.intel

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        rt.store.add_decision = boom
        for i in range(3):
            assert rt.record_decision({"ts": "t", "symbol": "JUP", "technical": "BUY", "mode": "shadow", "data_state": "ok", "action": "x", "would_change": False,
                                       "applied": False, "risk_scale": 1.0}) is None
        assert rt.audit_failures == 3 and "OperationalError" in (rt.last_audit_error or "")
        rt.link_position(1, "p1")  # também não lança (link_position ainda é o real: tabela existe; só não deve levantar)


def test_off_mode_never_touches_the_audit_path():
    sig_off, acts, _, n = _run_case("off")
    assert n == 0 and not any("AUDITORIA" in a for a in acts)
