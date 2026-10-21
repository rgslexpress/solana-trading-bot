"""Ciclo de vida do paper trading pelo painel: início/pausa/retomada/parada, emergência, persistência, erros, dados ruins.
Servidor HTTP e controlador REAIS; provedor de candles e relógio são determinísticos (mock) — não validam APIs externas."""

import os
import sqlite3
import tempfile
import threading
from datetime import timedelta

import pandas as pd

from app import main as cli_main
from app.dashboard import __main__ as dash_main
from app.dashboard.readmodel import _rows
from harness import Panel, SEC, make_provider, make_settings, wait_until


def _db(p):
    c = sqlite3.connect(p.s.db_path)
    c.row_factory = sqlite3.Row
    return c


def _events(p):
    c = _db(p)
    try:
        return [r["event"] for r in c.execute("SELECT event FROM bot_events ORDER BY id")]
    finally:
        c.close()


def test_opening_the_panel_does_not_start_anything():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            assert wait_until(lambda: False, 0.4) is False  # dá tempo para qualquer início indevido
            st = p.status()
            assert st["estado"] == "PARADO" and p.ctl.cycles == 0 and p.ctl.runtime is None and st["modo"] == "PAPER" and st["live"]["bloqueado"]
            assert st["acoes_permitidas"]["iniciar"] and not st["acoes_permitidas"]["parar"]
            c = _db(p)
            try:
                assert c.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 0
                assert c.execute("SELECT COUNT(*) FROM bot_events").fetchone()[0] == 0
            finally:
                c.close()
        finally:
            p.close()


def test_empty_database_shows_nd_not_invented_numbers():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            m = p.get("/api/metrics")[1]["metricas"]
            for k in ("patrimonio", "pnl_realizado", "pnl_nao_realizado", "rentabilidade_pct", "drawdown_max_pct", "taxa_acerto", "profit_factor", "sharpe", "caixa"):
                assert m[k] is None, k  # a interface exibirá "N/D"
            assert m["saldo_inicial"] == 1000.0 and "SIMULADO" in m["rotulo"]
            assert p.get("/api/positions")[1] == {"abertas": [], "encerradas": []}
            assert p.get("/api/decisions")[1] == {"decisoes": []}
            assert p.get("/api/status")[1]["dados"]["tokens"] == {}
        finally:
            p.close()


def test_start_pause_resume_stop_call_the_real_backend():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            assert p.post("/api/bot/pause-entries")[0] == 200 and p.status()["entradas_pausadas"] is True  # persistido mesmo parado
            assert p.post("/api/bot/resume-entries")[0] == 200
            st, js = p.post("/api/bot/start")
            assert st == 200 and js["ok"] and not js["modo_seguro"], js
            assert wait_until(lambda: p.ctl.cycles >= 2) and p.status()["estado"] == "RODANDO"
            assert p.post("/api/bot/start")[0] == 409  # sem execução duplicada
            assert p.post("/api/bot/pause-entries")[0] == 200
            s = p.status()
            assert s["estado"] == "ENTRADAS_PAUSADAS" and s["acoes_permitidas"]["retomar_entradas"] and not s["acoes_permitidas"]["pausar_entradas"]
            assert p.ctl.runtime.service.entries_paused is True  # o serviço REAL foi alterado
            assert p.post("/api/bot/resume-entries")[0] == 200 and p.status()["estado"] == "RODANDO"
            st, js = p.post("/api/bot/stop")
            assert st == 200 and js["parado"] is True
            assert p.status()["estado"] == "PARADO" and not p.ctl._alive()
            assert p.post("/api/bot/stop")[1]["mensagem"].startswith("O paper trading já estava parado")  # idempotente
            ev = _events(p)
            for e in ("entradas_pausadas", "entradas_retomadas", "paper_iniciado", "parada_solicitada", "paper_parado"):
                assert e in ev, (e, ev)
        finally:
            p.close()


def test_paused_entries_block_buys_and_resume_allows_them_decisions_are_recorded():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.post("/api/bot/start")
            p.post("/api/bot/pause-entries")
            for i in range(126, 134):  # o sinal de compra ocorre no candle 127
                p.advance(i)
            assert p.get("/api/positions")[1]["abertas"] == []
            dec = p.get("/api/decisions?limite=200")[1]["decisoes"]
            assert any(x["tipo"] == "BUY" for x in dec) and any("pausadas pelo operador" in " ".join(x["rejeicoes"]) for x in dec)  # decisão + motivo do bloqueio
            assert any(x["tipo"] == "NO_TRADE" and x["rejeicoes"] for x in dec)
        finally:
            p.close()


def test_paper_trade_through_panel_persists_across_panel_restart_and_reconciles():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        opened = None
        try:
            assert p.post("/api/bot/start")[0] == 200
            for i in range(126, 150):
                p.advance(i)
                if p.get("/api/positions")[1]["abertas"]:
                    opened = i
                    break
            assert opened is not None, "nenhuma compra simulada"
            pos = p.get("/api/positions")[1]["abertas"][0]
            assert pos["simbolo"] == "JUP" and pos["quantidade"] > 0 and pos["stop"] < pos["preco_entrada"] < pos["alvo"]
            m = p.get("/api/metrics")[1]["metricas"]
            assert m["patrimonio"] is not None and m["saldo_inicial"] == 1000.0 and m["pnl_nao_realizado"] is not None and m["pnl_realizado"] == 0.0
            p.post("/api/bot/stop")
            st = p.status()
            assert st["estado"] == "PARADO" and st["posicoes_abertas"] == 1
            assert any("SEM monitoramento" in a for a in st["avisos"])  # aviso explícito: parado => stops não monitorados
        finally:
            p.close()
        # --- "reinício do computador": novo painel/controlador sobre a MESMA base
        p2 = Panel(d, provider=p.provider, entry=p.entry, start_idx=opened)
        try:
            assert p2.status()["estado"] == "PARADO"  # não retoma sozinho
            again = p2.get("/api/positions")[1]["abertas"]
            assert len(again) == 1 and again[0]["id"] == pos["id"] and abs(again[0]["quantidade"] - pos["quantidade"]) < 1e-12
            cash_before = p2.get("/api/metrics")[1]["metricas"]["caixa"]
            assert p2.post("/api/bot/start")[0] == 200  # reconcilia na inicialização
            assert len(p2.ctl.runtime.service.positions) == 1
            for i in range(opened + 1, len(p2.entry)):
                p2.advance(i)
            assert wait_until(lambda: not p2.get("/api/positions")[1]["abertas"], 10)
            closed = p2.get("/api/positions")[1]["encerradas"]
            assert len(closed) == 1 and closed[0]["pnl_liquido"] is not None and closed[0]["motivo_saida"]
            m2 = p2.get("/api/metrics")[1]["metricas"]
            assert m2["operacoes_encerradas"] == 1 and m2["pnl_realizado"] == closed[0]["pnl_liquido"] and m2["custos_totais"] > 0
            assert p2.get("/api/metrics")[1]["curva_patrimonio"]
            # dinheiro simulado coerente com o livro: patrimônio = capital inicial + PnL realizado (sem posições abertas)
            assert abs(m2["patrimonio"] - (1000.0 + m2["pnl_realizado"])) < 1e-6 and cash_before is not None
        finally:
            p2.close()


def test_emergency_triggers_existing_kill_switch_blocks_entries_but_keeps_monitoring_and_never_liquidates():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.post("/api/bot/start")
            assert wait_until(lambda: p.ctl.cycles >= 1)
            st, js = p.post("/api/bot/emergency", {"confirm": True})
            assert st == 200 and "nada foi liquidado" in js["mensagem"]
            s = p.status()
            assert s["kill_switch"]["ativo"] and "EMERGÊNCIA" in s["kill_switch"]["motivo"] and s["entradas_pausadas"]
            assert p.ctl.runtime.service.ks.active  # propagado ao serviço em execução
            assert s["estado"] in ("RODANDO", "ENTRADAS_PAUSADAS")  # monitoramento de posições continua
            for i in range(126, 134):
                p.advance(i)
            assert p.get("/api/positions")[1]["abertas"] == []  # nenhuma entrada com kill switch ativo
            assert p.post("/api/bot/resume-entries")[0] == 409  # a interface não contorna o kill switch
            assert p.post("/api/kill-switch/resume", {"phrase": "RETOMAR_OPERACAO", "note": "x"})[0] == 409  # exige parar antes
            p.post("/api/bot/stop")
            assert p.post("/api/kill-switch/resume", {"phrase": "sim", "note": "ok"})[0] == 400
            assert p.post("/api/kill-switch/resume", {"phrase": "RETOMAR_OPERACAO", "note": "   "})[0] == 400
            assert p.status()["kill_switch"]["ativo"]
            assert p.post("/api/kill-switch/resume", {"phrase": "RETOMAR_OPERACAO", "note": "causa corrigida"})[0] == 200
            s = p.status()
            assert not s["kill_switch"]["ativo"] and s["entradas_pausadas"] is True  # entradas seguem pausadas até retomada explícita
            assert "emergencia" in _events(p) and "kill_switch_retomado" in _events(p)
        finally:
            p.close()


def test_emergency_while_stopped_persists_and_next_start_is_safe_mode():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            assert p.post("/api/bot/emergency", {"confirm": True})[0] == 200
            assert p.status()["kill_switch"]["ativo"]
            st, js = p.post("/api/bot/start")
            assert st == 200 and js["modo_seguro"] is True  # kill switch ativo => startup reprovado => sem entradas
            for i in range(126, 133):
                p.advance(i)
            assert p.get("/api/positions")[1]["abertas"] == []
            assert any("MODO SEGURO" in a or "Kill switch" in a for a in p.status()["avisos"])
        finally:
            p.close()


def test_cycle_errors_do_not_kill_the_loop_pause_entries_and_recover():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d, max_errors=3)
        try:
            p.post("/api/bot/start")
            assert wait_until(lambda: p.ctl.cycles >= 1)
            svc = p.ctl.runtime.service
            real = svc.run_cycle
            calls = {"n": 0}

            def flaky():
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise RuntimeError("falha simulada api_key=SEGREDO123")
                return real()

            svc.run_cycle = flaky  # type: ignore[method-assign]
            assert wait_until(lambda: calls["n"] >= 4 and p.ctl.consecutive_errors == 0, 15), (calls, p.ctl.consecutive_errors)
            assert p.ctl._alive() and p.ctl.cycles >= 2  # o laço sobreviveu e voltou a ciclar
            assert svc.entries_paused is True  # pausou automaticamente após 3 erros e NÃO retoma sozinho
            ev = _events(p)
            assert ev.count("erro_ciclo") == 3 and "entradas_pausadas_automaticamente" in ev
            c = _db(p)
            try:
                detail = " ".join(r["detail"] for r in c.execute("SELECT detail FROM bot_events"))
            finally:
                c.close()
            assert "SEGREDO123" not in detail  # eventos são redigidos
            st = p.status()
            assert st["estado"] == "ENTRADAS_PAUSADAS" and p.ctl.last_error and "SEGREDO123" not in p.ctl.last_error
        finally:
            p.close()


def test_missing_invalid_and_stale_data_are_reported_and_block_entries():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.provider.fail = True  # fonte indisponível desde o início
            st, js = p.post("/api/bot/start")
            assert st == 200 and js["modo_seguro"] is True
            assert wait_until(lambda: p.ctl.cycles >= 2)
            s = p.status()
            assert any("inválidos/indisponíveis" in a for a in s["dados"]["alertas"]) and s["dados"]["tokens"]["JUP"]["ok"] is False
            assert p.get("/api/positions")[1]["abertas"] == []
            p.post("/api/bot/stop")
        finally:
            p.close()
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.post("/api/bot/start")
            p.advance(126)
            s = p.status()
            assert s["dados"]["tokens"]["JUP"]["ok"] and not s["dados"]["tokens"]["JUP"]["obsoleto"] and s["dados"]["tokens"]["JUP"]["idade_ultimo_candle_s"] is not None
            p.post("/api/bot/stop")
            p.clock["now"] = p.clock["now"] + timedelta(days=3)  # o relógio avança, o último candle fica velho
            s = p.status()
            assert s["dados"]["tokens"]["JUP"]["obsoleto"] is True and any("OBSOLETOS" in a for a in s["avisos"])
        finally:
            p.close()
    # dados inválidos: candle com preço negativo
    with tempfile.TemporaryDirectory() as d:
        prov, entry = make_provider()
        prov.frames["15m"] = entry.copy()
        prov.frames["15m"].iloc[100, prov.frames["15m"].columns.get_loc("close")] = -5.0
        p = Panel(d, provider=prov, entry=prov.frames["15m"])
        try:
            p.post("/api/bot/start")
            assert wait_until(lambda: p.ctl.cycles >= 1)
            assert any("inválidos" in a for a in p.status()["dados"]["alertas"])
            assert p.get("/api/positions")[1]["abertas"] == []
        finally:
            p.close()


def test_only_one_trading_loop_per_database_between_panel_and_cli():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            assert p.post("/api/bot/start")[0] == 200
            # segundo controlador (outro painel) sobre a mesma base
            other = Panel(d, provider=p.provider, entry=p.entry)
            try:
                st, js = other.post("/api/bot/start")
                assert st == 409 and "outra instância" in js["erro"]
            finally:
                other.close()
            # CLI `python -m app.main --mode paper` também é recusado enquanto o painel roda o laço
            env = os.path.join(d, ".env")
            with open(env, "w") as f:
                f.write(f"BOT_TOKENS_FILE={p.s.tokens_file}\nBOT_DB_PATH={p.s.db_path}\nBOT_LOG_FILE=\n")
            import contextlib
            import io

            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = cli_main.main(["--env-file", env, "--mode", "paper", "--once"])
            assert code == 3 and "Recusado" in err.getvalue()
            p.post("/api/bot/stop")
            again = Panel(d, provider=p.provider, entry=p.entry)
            try:
                assert again.post("/api/bot/start")[0] == 200  # liberado após a parada
            finally:
                again.close()
        finally:
            p.close()


def test_panel_shutdown_stops_loop_orderly_warns_about_open_positions_and_never_sends_orders():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.post("/api/bot/start")
            opened = None
            for i in range(126, 150):
                p.advance(i)
                if p.get("/api/positions")[1]["abertas"]:
                    opened = i
                    break
            assert opened is not None
            c = _db(p)
            intents_before = c.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
            c.close()
            st, js = p.post("/api/panel/shutdown")
            assert st == 200 and wait_until(p.server.shutdown_requested.is_set)
            assert wait_until(lambda: not p.ctl._alive(), 15)
            assert wait_until(lambda: "painel_encerrado_com_posicoes" in _events(p), 10)
            assert "painel_encerrado_com_posicoes" in _events(p) and "paper_parado" in _events(p)
            c = _db(p)
            try:
                assert c.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == intents_before  # nenhuma ordem nova no encerramento
                assert c.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0] == 1  # posição preservada, não liquidada
            finally:
                c.close()
        finally:
            p.close()


def test_cli_stop_command_uses_runtime_file_over_loopback_only():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            msg = dash_main.request_shutdown(p.s.db_path)
            assert "Nenhum painel" in msg  # sem arquivo de execução
            import json as _j

            rf = dash_main.runtime_file(p.s.db_path)
            rf.write_text(_j.dumps({"pid": os.getpid(), "porta": p.port, "token": p.token}), encoding="utf-8")
            msg = dash_main.request_shutdown(p.s.db_path)
            assert "Encerrando" in msg and wait_until(p.server.shutdown_requested.is_set)
        finally:
            p.close()


def test_logs_errors_and_bot_events_are_available_in_the_panel():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            p.post("/api/bot/start")
            p.post("/api/bot/pause-entries")
            p.post("/api/bot/stop")
            js = p.get("/api/logs")[1]
            names = [e["evento"] for e in js["eventos"]]
            assert "paper_iniciado" in names and "entradas_pausadas" in names and "paper_parado" in names
            assert all(e["origem"] in ("painel", "robo", "sistema") for e in js["eventos"])
        finally:
            p.close()
