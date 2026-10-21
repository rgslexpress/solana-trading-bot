"""Servidor HTTP do painel: SOMENTE loopback, com proteções contra CSRF, cross-origin e DNS rebinding.

Sem dependências externas (biblioteca padrão). Camadas de defesa, em ordem, para TODA requisição:
 1. Bind exclusivo em 127.0.0.x (qualquer outro endereço é recusado na criação do servidor).
 2. Cabeçalho Host precisa ser 127.0.0.1:<porta> ou localhost:<porta> (bloqueia DNS rebinding).
 3. Nenhum cabeçalho CORS é emitido (navegadores impedem páginas de outras origens de LEREM as respostas) e OPTIONS é recusado.
 4. POST exige: Origin (se presente) igual à origem do painel; Content-Type application/json; token CSRF aleatório por
    execução (só obtido lendo a página do painel, o que a política de mesma origem impede para outros sites).
 5. Corpo ≤ 4 KB, JSON, esquema estrito por rota (campos desconhecidos => 400). Nenhuma rota aceita modo, chaves ou configuração.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.dashboard import readmodel
from app.dashboard.controller import BotController, CommandError

log = logging.getLogger("painel.http")
STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 4096
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
STATIC_FILES = {"/static/app.js": ("app.js", "text/javascript; charset=utf-8"), "/static/app.css": ("app.css", "text/css; charset=utf-8")}
POST_ROUTES = ("/api/bot/start", "/api/bot/pause-entries", "/api/bot/resume-entries", "/api/bot/stop", "/api/bot/emergency",
               "/api/kill-switch/resume", "/api/panel/shutdown")
GET_ROUTES = ("/", "/api/status", "/api/metrics", "/api/positions", "/api/decisions", "/api/logs", "/api/config", *STATIC_FILES)


def validate_loopback_host(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"endereço inválido: {host!r} (use 127.0.0.1)") from exc
    if ip.version != 4 or not ip.is_loopback:
        raise ValueError(f"o painel só pode escutar em loopback IPv4 (127.x.x.x); recebido {host!r}")
    return host


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # no Windows reuse permitiria DOIS processos na mesma porta

    def __init__(self, host: str, port: int, controller: BotController, csrf_token: str | None = None, static_dir: Path = STATIC_DIR):
        validate_loopback_host(host)
        if not (0 <= int(port) <= 65535):
            raise ValueError("porta inválida")
        self.controller = controller
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.static_dir = static_dir
        self.shutdown_requested = threading.Event()
        super().__init__((host, int(port)), PanelHandler)
        p = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        self.allowed_origins = {f"http://127.0.0.1:{p}", f"http://localhost:{p}"}

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: impede outro processo de compartilhar a porta
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def request_shutdown(self) -> None:
        """Encerra de forma organizada: primeiro o laço de paper trading (sem ordens, posições preservadas), depois o servidor."""
        self.shutdown_requested.set()

        def _work() -> None:
            try:
                self.controller.shutdown()
            finally:
                self.shutdown()

        threading.Thread(target=_work, name="painel-shutdown", daemon=True).start()


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer
    server_version = "PainelRobo"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ infraestrutura
    def log_message(self, fmt: str, *args: Any) -> None:  # somente método/caminho/status; nunca cabeçalhos ou corpo
        log.info("%s %s", self.command, self.path.split("?")[0])

    def _headers(self, status: int, ctype: str, length: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status: int, body: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data), extra)
        self.wfile.write(data)

    def _error(self, status: int, msg: str) -> None:
        self._json(status, {"erro": msg})

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.server.allowed_hosts

    # ------------------------------------------------------------ métodos
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "Host não permitido (o painel só atende 127.0.0.1/localhost)")
        path = self.path.split("?")[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path not in GET_ROUTES:
            return self._error(404, "não encontrado")
        if path == "/":
            html = (self.server.static_dir / "index.html").read_text(encoding="utf-8").replace("__CSRF__", self.server.csrf_token)
            return self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
        if path in STATIC_FILES:  # nomes fixos: nenhum caminho vindo do cliente toca o disco
            fname, ctype = STATIC_FILES[path]
            return self._send_bytes((self.server.static_dir / fname).read_bytes(), ctype)
        try:
            self._json(200, self._api_get(path, query))
        except Exception:  # noqa: BLE001 - não vaza detalhes internos
            log.exception("erro ao atender %s", path)
            self._error(500, "erro interno ao ler o estado (veja os logs)")

    def _send_bytes(self, data: bytes, ctype: str) -> None:
        self._headers(200, ctype, len(data))
        self.wfile.write(data)

    def _api_get(self, path: str, query: str) -> dict[str, Any]:
        c = self.server.controller
        limit = 100
        for part in query.split("&"):
            if part.startswith("limite="):
                try:
                    limit = int(part.split("=", 1)[1])
                except ValueError:
                    limit = 100
        if path == "/api/status":
            return c.status()
        if path == "/api/config":
            return readmodel.config_view(c.s)
        with readmodel.open_ro(c.s.db_path) as conn:
            if path == "/api/metrics":
                return {"metricas": readmodel.metrics(conn, c.s), "curva_patrimonio": readmodel.equity_curve(conn)}
            if path == "/api/positions":
                return {"abertas": readmodel.open_positions_view(conn), "encerradas": readmodel.closed_trades_view(conn, min(max(limit, 1), 500))}
            if path == "/api/decisions":
                return {"decisoes": readmodel.decisions_view(conn, limit)}
            if path == "/api/logs":
                return readmodel.logs_view(conn, c.s.log_file, limit)
        return {}

    def _drain_body(self) -> tuple[int, bytes]:
        """Lê SEMPRE o corpo enviado pelo cliente antes de responder. No Windows, responder/fechar a conexão com dados
        não lidos causa RST (WinError 10053) e o cliente não recebe a resposta. Retorna (tamanho_declarado, corpo<=MAX_BODY)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1, b""
        if length <= 0:
            return length, b""
        keep = self.rfile.read(min(length, MAX_BODY))
        remaining = min(length, 1_048_576) - len(keep)  # descarta (sem guardar) até 1 MB; acima disso apenas fecha
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        return length, keep

    def _reject_method(self) -> None:
        self._drain_body()
        self._json(405, {"erro": "método não permitido"}, {"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _reject_method  # noqa: N815 (sem CORS: OPTIONS recusado)

    def do_POST(self) -> None:  # noqa: N802
        length, raw = self._drain_body()  # antes de QUALQUER resposta (ver _drain_body)
        if not self._host_ok():
            return self._error(403, "Host não permitido")
        path = self.path.split("?")[0]
        if path not in POST_ROUTES:
            return self._error(404, "não encontrado")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            return self._error(403, "origem não permitida")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return self._error(403, "requisição entre sites recusada")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "Content-Type deve ser application/json")
        token = self.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(token.encode(), self.server.csrf_token.encode()):
            return self._error(403, "token CSRF inválido")
        if length < 0:
            return self._error(400, "Content-Length inválido")
        if length > MAX_BODY:
            return self._error(413, "corpo grande demais")
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "JSON inválido")
        if not isinstance(body, dict):
            return self._error(400, "o corpo deve ser um objeto JSON")
        try:
            result = self._dispatch(path, body)
        except CommandError as exc:
            return self._error(exc.status, exc.message)
        except Exception:  # noqa: BLE001
            log.exception("erro ao executar %s", path)
            return self._error(500, "erro interno ao executar o comando (veja os logs)")
        self._json(200, result)
        if path == "/api/panel/shutdown":
            self.server.request_shutdown()

    def _dispatch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        c = self.server.controller
        allowed = {"/api/bot/emergency": {"confirm"}, "/api/kill-switch/resume": {"phrase", "note"}}.get(path, set())
        extra = set(body) - allowed
        if extra:  # esquema estrito: nenhum comando aceita modo, chaves, limites ou qualquer campo extra
            raise CommandError(400, f"campos não permitidos: {sorted(extra)}")
        if path == "/api/bot/start":
            return c.start()
        if path == "/api/bot/pause-entries":
            return c.pause_entries()
        if path == "/api/bot/resume-entries":
            return c.resume_entries()
        if path == "/api/bot/stop":
            return c.stop()
        if path == "/api/bot/emergency":
            if body.get("confirm") is not True:
                raise CommandError(400, "confirmação obrigatória: envie {\"confirm\": true}")
            return c.emergency()
        if path == "/api/kill-switch/resume":
            phrase, note = body.get("phrase"), body.get("note")
            if not (isinstance(phrase, str) and isinstance(note, str)) or len(phrase) > 100 or len(note) > 300:
                raise CommandError(400, "informe 'phrase' e 'note' (texto)")
            return c.resume_kill_switch(phrase, note)
        if path == "/api/panel/shutdown":
            return {"ok": True, "mensagem": "Encerrando o painel. O paper trading será parado de forma organizada; posições simuladas ficam preservadas."}
        raise CommandError(404, "não encontrado")
