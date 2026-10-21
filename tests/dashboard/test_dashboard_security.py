"""Segurança do painel local — servidor REAL em loopback (sem mocks do HTTP)."""

import ast
import json
import os
import socket
import tempfile
from pathlib import Path

from app.config import Settings
from app.dashboard import server as srv
from app.dashboard.controller import BotController
from app.dashboard.server import PanelServer, validate_loopback_host
from app.domain import Mode
from harness import Panel, make_settings, wait_until
from _helpers import raises

ROOT = Path(__file__).resolve().parent.parent.parent


def test_server_refuses_non_loopback_hosts_and_binds_127_0_0_1():
    for bad in ("0.0.0.0", "192.168.0.10", "::", "::1", "localhost", "", "8.8.8.8", "10.0.0.1"):
        with raises(ValueError):
            validate_loopback_host(bad)
    with tempfile.TemporaryDirectory() as d:
        s = make_settings(d)
        ctl = BotController(s)
        for bad in ("0.0.0.0", "192.168.0.10"):
            with raises(ValueError):
                PanelServer(bad, 0, ctl)
        p = Panel(d)
        try:
            host, port = p.server.socket.getsockname()[:2]
            assert host == "127.0.0.1" and port == p.port
        finally:
            p.close()


def test_host_header_must_be_loopback_blocks_dns_rebinding():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            for host in ("evil.example", "evil.example:%d" % p.port, "192.168.0.5:%d" % p.port, "127.0.0.1:1", "localhost:9"):
                assert p.req("GET", "/api/status", headers={"Host": host})[0] == 403, host
                assert p.req("GET", "/", headers={"Host": host})[0] == 403
                assert p.req("POST", "/api/bot/stop", headers={"Host": host})[0] == 403
            assert p.req("GET", "/api/status", headers={"Host": f"localhost:{p.port}"})[0] == 200
        finally:
            p.close()


def test_index_has_csrf_token_csp_and_no_cors_headers_and_no_inline_script():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            st, _, h, body = p.req("GET", "/")
            html = body.decode()
            assert st == 200 and f'content="{p.token}"' in html and "__CSRF__" not in html
            assert "<script src=\"/static/app.js\"" in html and "onclick=" not in html and "<script>" not in html  # sem JS inline
            csp = h["content-security-policy"]
            assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
            assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
            for path in ("/", "/api/status", "/api/config", "/static/app.js"):
                hh = p.req("GET", path)[2]
                assert not any(k.startswith("access-control-") for k in hh), (path, hh)  # sem CORS: sites externos não leem respostas
        finally:
            p.close()


def test_path_traversal_and_unknown_routes_are_404():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            for path in ("/static/../app/config.py", "/static/%2e%2e/%2e%2e/app/config.py", "/etc/passwd", "/static/", "/static/secret.txt", "/api/unknown", "/.env", "//etc/passwd"):
                st = p.req("GET", path)[0]
                assert st == 404, (path, st)
            assert p.req("POST", "/api/bot/unknown")[0] == 404
        finally:
            p.close()


def test_post_requires_csrf_origin_content_type_size_and_valid_json():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            url = "/api/bot/pause-entries"
            assert p.req("POST", url, token=False)[0] == 403  # sem token CSRF
            assert p.req("POST", url, headers={"X-CSRF-Token": "x" * 43})[0] == 403  # token errado
            assert p.req("POST", url, headers={"Origin": "http://evil.example"})[0] == 403  # origem externa (mesmo com token)
            assert p.req("POST", url, headers={"Origin": "null"})[0] == 403
            assert p.req("POST", url, headers={"Origin": f"http://127.0.0.1:{p.port + 1}"})[0] == 403  # outra porta local = outra origem
            assert p.req("POST", url, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert p.req("POST", url, headers={"Content-Type": "text/plain"})[0] == 415  # formulário "simples" cross-site
            assert p.req("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
            assert p.req("POST", url, raw=b"{" )[0] == 400
            assert p.req("POST", url, raw=b"[1,2]")[0] == 400
            assert p.req("POST", url, raw=b"x" * 5000)[0] == 413
            assert p.status()["entradas_pausadas"] is False  # nada disso executou o comando
            assert p.req("POST", url)[0] == 200  # requisição legítima funciona
            assert p.status()["entradas_pausadas"] is True
        finally:
            p.close()


def test_cross_origin_page_cannot_control_the_bot_even_if_it_guesses_the_endpoint():
    """Simula a página maliciosa: envia a requisição de outra origem, sem conhecer o token."""
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            evil = {"Origin": "http://evil.example", "Referer": "http://evil.example/x"}
            for path in ("/api/bot/start", "/api/bot/emergency", "/api/bot/stop", "/api/kill-switch/resume", "/api/panel/shutdown"):
                s = p.req("POST", path, body={"confirm": True}, token=False, origin=False, headers=evil)[0]
                assert s == 403, path
            assert p.ctl.state == "PARADO" and not p.server.shutdown_requested.is_set()
            assert p.status()["kill_switch"]["ativo"] is False
        finally:
            p.close()


def test_methods_other_than_get_post_are_rejected_without_cors():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
                st, js, h, _ = p.req(m, "/api/bot/start")
                assert st == 405 and not any(k.startswith("access-control-") for k in h), m
        finally:
            p.close()


def test_strict_schema_no_endpoint_accepts_mode_keys_or_config():
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            evil_bodies = [{"mode": "live"}, {"modo": "live"}, {"live": True}, {"wallet_secret": "x"}, {"private_key": "x"}, {"config": {"risk_per_trade_pct": 99}},
                           {"risk": {"max_open_positions": 99}}, {"confirm_live": "EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS"}, {"BOT_LIVE_CONFIRM": "x"}]
            for path in srv.POST_ROUTES:
                for body in evil_bodies:
                    st = p.post(path, body)[0]
                    assert st == 400, (path, body, st)
            assert p.ctl.s.mode == Mode.PAPER and p.ctl.state == "PARADO"
            assert p.status()["entradas_pausadas"] is False and p.status()["kill_switch"]["ativo"] is False
            assert p.post("/api/bot/emergency", {})[0] == 400  # emergência exige confirmação explícita
            assert p.post("/api/bot/emergency", {"confirm": "true"})[0] == 400  # string não vale
        finally:
            p.close()


def test_route_table_exposes_nothing_about_live_wallets_signing_or_config_writes():
    forbidden = ("live", "sign", "send", "wallet", "key", "secret", "mode", "modo", "config", "swap", "order", "trade-real", "transaction")
    for route in (*srv.POST_ROUTES, *(r for r in srv.GET_ROUTES if r.startswith("/api"))):
        if route == "/api/config":  # leitura somente; whitelist testada abaixo
            continue
        assert not any(f in route.lower() for f in forbidden), route
    assert "/api/config" not in srv.POST_ROUTES


def test_dashboard_package_has_no_path_to_wallets_signing_jupiter_or_network():
    banned = ("signer", "jupiter_client", "jupiter_executor", "live_guard", "tx_validator", "rpc", "httpx", "requests", "socket_client")
    for f in (ROOT / "app" / "dashboard").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            for n in names:
                assert not any(b in n.lower() for b in banned), (f.name, n)
    # único uso de urllib no pacote é o `--parar` (requisição de encerramento a 127.0.0.1)
    users = [f.name for f in (ROOT / "app" / "dashboard").glob("*.py") if "urllib" in f.read_text(encoding="utf-8")]
    assert users == ["__main__.py"], users


def test_full_start_stop_cycle_never_touches_signer_or_live_builder():
    from app import factory
    from app.execution.signer import Signer

    touched = []
    orig_from, orig_live = Signer.from_config, factory._build_live
    Signer.from_config = classmethod(lambda cls, *a, **k: touched.append("signer") or orig_from(*a, **k))  # type: ignore[assignment]
    factory._build_live = lambda *a, **k: touched.append("live") or orig_live(*a, **k)  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as d:
            # mesmo com BOT_MODE=live + confirmações + segredo na configuração, o painel só roda PAPER
            s = make_settings(d, mode=Mode.LIVE, live_confirm="EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS", wallet_secret="x" * 88, jupiter_api_key="k")
            p = Panel(d, settings=s)
            try:
                assert p.ctl.live_requested_in_config and p.ctl.s.mode == Mode.PAPER
                st = p.status()
                assert st["modo"] == "PAPER" and st["live"]["bloqueado"] is True
                assert any("IGNORADO" in a for a in st["avisos"])
                assert p.post("/api/bot/start")[0] == 200
                from app.execution.paper import PaperExecutor

                assert isinstance(p.ctl.runtime.service.executor, PaperExecutor)
                p.advance(126)
                assert p.post("/api/bot/stop")[0] == 200
            finally:
                p.close()
        assert touched == []
    finally:
        Signer.from_config = orig_from  # type: ignore[method-assign]
        factory._build_live = orig_live  # type: ignore[assignment]


def test_config_endpoint_is_a_whitelist_without_secrets_wallet_or_rpc():
    with tempfile.TemporaryDirectory() as d:
        s = make_settings(d, wallet_secret="WALLET-SECRET-XYZ", jupiter_api_key="JUP-KEY-XYZ", telegram_bot_token="TG-XYZ",
                          wallet_public_key="PUBKEY-VISIVEL-NAO", rpc_url="https://rpc.example/?api-key=RPCKEY")
        p = Panel(d, settings=s)
        try:
            st, js = p.get("/api/config")
            txt = json.dumps(js)
            for leak in ("WALLET-SECRET-XYZ", "JUP-KEY-XYZ", "TG-XYZ", "PUBKEY-VISIVEL-NAO", "RPCKEY", "rpc_url", "wallet", "jupiter_api"):
                assert leak not in txt, leak
            assert st == 200 and js["risco"]["risk_per_trade_pct"] == 0.5 and js["risco"]["daily_loss_limit_pct"] == 2.0
            all_status = json.dumps(p.status())
            assert "WALLET-SECRET-XYZ" not in all_status and "JUP-KEY-XYZ" not in all_status
        finally:
            p.close()


def test_logs_endpoint_redacts_secrets():
    from app.utils.base58 import b58encode

    secret = b58encode(bytes(range(64)))
    with tempfile.TemporaryDirectory() as d:
        s = make_settings(d)
        Path(s.log_file).write_text(json.dumps({"ts": "t", "nivel": "ERROR", "logger": "x", "msg": f"falha com {secret} e api_key=SEGREDO123"}) + "\n", encoding="utf-8")
        p = Panel(d, settings=s)
        try:
            st, js, _, raw = p.req("GET", "/api/logs")
            assert st == 200 and secret not in raw.decode() and "SEGREDO123" not in raw.decode()
            assert js["erros"] and js["arquivo_existe"]
        finally:
            p.close()


def test_second_server_on_same_port_and_second_panel_instance_are_refused():
    from app.instance import AlreadyRunning, InstanceLock, panel_lock_path

    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            with raises(OSError):
                PanelServer("127.0.0.1", p.port, p.ctl)
            a = InstanceLock(panel_lock_path(p.s.db_path), "instância do painel").acquire()
            with raises(AlreadyRunning):
                InstanceLock(panel_lock_path(p.s.db_path), "instância do painel").acquire()
            a.release()
        finally:
            p.close()


def test_rejections_are_delivered_even_when_the_client_sends_a_body_that_the_server_does_not_use():
    """Regressão (Windows, WinError 10053): recusar sem ler o corpo derrubava a conexão antes do cliente ler o 403/413/405."""
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            big = b"x" * 3000
            for _ in range(5):  # repete: o defeito dependia de timing
                assert p.req("POST", "/api/bot/stop", headers={"Host": "evil.example"}, raw=big)[0] == 403
                assert p.req("POST", "/api/bot/stop", token=False, raw=big)[0] == 403
                assert p.req("POST", "/api/bot/stop", headers={"Origin": "http://evil.example"}, raw=big)[0] == 403
                assert p.req("POST", "/api/bot/unknown", raw=big)[0] == 404
                assert p.req("POST", "/api/bot/stop", raw=b"x" * 5000)[0] == 413
                assert p.req("POST", "/api/bot/stop", raw=b"x" * 200_000)[0] == 413
                assert p.req("PUT", "/api/bot/stop", raw=big)[0] == 405
            assert p.req("POST", "/api/bot/pause-entries")[0] == 200  # o servidor continua saudável
        finally:
            p.close()
