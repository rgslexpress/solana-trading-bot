"""Painel local: python -m app.dashboard [--porta 8765] [--abrir] | --parar

Escuta SOMENTE em 127.0.0.1. Não inicia o paper trading automaticamente. Não pede nem lê chaves/carteiras."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from app.config import ConfigError, load_settings
from app.dashboard.controller import BotController
from app.dashboard.server import PanelServer, validate_loopback_host
from app.instance import AlreadyRunning, InstanceLock, panel_lock_path
from app.logging_config import setup_logging


def runtime_file(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".painel.json")


def request_shutdown(db_path: str, timeout: float = 30.0) -> str:
    """Pede o encerramento organizado do painel em execução (usa o arquivo de execução local, só loopback)."""
    rf = runtime_file(db_path)
    if not rf.exists():
        return "Nenhum painel em execução (arquivo de execução não encontrado)."
    info = json.loads(rf.read_text(encoding="utf-8"))
    host_port = f"127.0.0.1:{int(info['porta'])}"
    req = urllib.request.Request(f"http://{host_port}/api/panel/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-CSRF-Token": info["token"], "Origin": f"http://{host_port}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("mensagem", "ok")
    except OSError as exc:
        return f"Não foi possível contatar o painel ({type(exc).__name__}). Se ele já fechou, apague {rf.name}."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.dashboard", description="Painel local do robô (somente PAPER, somente loopback).")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--host", default="127.0.0.1", help="somente loopback IPv4 (padrão 127.0.0.1)")
    ap.add_argument("--porta", type=int, default=None, help="padrão: BOT_PANEL_PORT ou 8765")
    ap.add_argument("--abrir", action="store_true", help="abre o navegador neste computador")
    ap.add_argument("--parar", action="store_true", help="encerra o painel em execução (de forma organizada)")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    try:
        s = load_settings(args.env_file)
        validate_loopback_host(args.host)
    except (ConfigError, ValueError) as exc:
        print(f"Configuração inválida: {exc}", file=sys.stderr)
        return 2
    if args.parar:
        print(request_shutdown(s.db_path))
        return 0
    setup_logging(s.log_level, s.log_json, s.log_file)
    port = args.porta if args.porta is not None else s.panel_port
    try:
        plock = InstanceLock(panel_lock_path(s.db_path), "instância do painel").acquire()
    except AlreadyRunning as exc:
        print(f"Recusado: {exc}.", file=sys.stderr)
        return 3
    controller = None
    rf = runtime_file(s.db_path)
    try:
        controller = BotController(s)
        server = PanelServer(args.host, port, controller)
    except OSError as exc:
        print(f"Não foi possível abrir {args.host}:{port}: {exc}. A porta pode estar em uso.", file=sys.stderr)
        plock.release()
        return 3
    rf.write_text(json.dumps({"pid": os.getpid(), "porta": server.port, "token": server.csrf_token}), encoding="utf-8")
    try:
        os.chmod(rf, 0o600)
    except OSError:
        pass
    url = f"http://127.0.0.1:{server.port}/"
    print(f"Painel local: {url}\nEscutando SOMENTE em 127.0.0.1 (loopback). O paper trading NÃO foi iniciado: use o botão 'Iniciar'.\n"
          "Para encerrar: Ctrl+C nesta janela ou `python -m app.dashboard --parar`.")
    if controller.live_requested_in_config:
        print("AVISO: a configuração pedia modo LIVE; o painel ignora isso e só executa PAPER.")
    signal.signal(signal.SIGTERM, lambda *_: server.request_shutdown())
    if args.abrir:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        print("Encerrando: parando o paper trading de forma organizada (posições simuladas preservadas)...")
        res = controller.shutdown()
        if res.get("aviso"):
            print("AVISO:", res["aviso"])
        server.server_close()
        try:
            rf.unlink()
        except OSError:
            pass
        plock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
