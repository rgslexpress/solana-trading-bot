"""Transporte HTTP injetável (permite testar toda a integração sem rede)."""

from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass
from typing import Any, Protocol


class TransportError(Exception):
    """Falha de rede/timeout: o resultado da requisição é DESCONHECIDO. `kind` ajuda a diagnosticar (timeout, total_timeout, too_large, network)."""

    def __init__(self, message: str = "", kind: str = "network"):
        super().__init__(message)
        self.kind = kind


@dataclass
class HttpResponse:
    status: int
    body: Any  # JSON decodificado, ou texto se não for JSON
    headers: dict[str, str]


class Transport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
        json: Any = None, timeout: float = 10.0,
    ) -> HttpResponse: ...


class HttpxTransport:
    """Implementação padrão com httpx (importado sob demanda).

    Além dos timeouts de conexão/leitura/escrita do httpx (que valem POR FASE), aceita `total_timeout` (prazo TOTAL da requisição,
    verificado a cada pedaço recebido) e `max_bytes` (a resposta é cortada ao passar do limite, sem baixar tudo).
    Uma fonte que "pinga" bytes devagar não consegue mais ultrapassar o prazo total por muito: o excesso é de no máximo um timeout de leitura."""

    supports_limits = True

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(follow_redirects=False)

    def request(self, method: str, url: str, *, headers=None, params=None, json=None, timeout: float = 10.0,  # type: ignore[no-untyped-def]
                total_timeout: float | None = None, max_bytes: int | None = None) -> HttpResponse:
        deadline = time.monotonic() + total_timeout if total_timeout else None
        try:
            with self._client.stream(method, url, headers=headers, params=params, json=json, timeout=self._httpx.Timeout(timeout)) as r:
                chunks: list[bytes] = []
                size = 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise TransportError(f"resposta maior que {max_bytes} bytes", "too_large")
                    chunks.append(chunk)
                    if deadline is not None and time.monotonic() > deadline:
                        raise TransportError(f"prazo total de {total_timeout:g}s excedido", "total_timeout")
                status, hdrs, enc = r.status_code, dict(r.headers), r.charset_encoding or "utf-8"
        except self._httpx.TimeoutException as exc:
            raise TransportError(type(exc).__name__, "timeout") from exc
        except self._httpx.HTTPError as exc:  # inclui erros de conexão/TLS
            raise TransportError(type(exc).__name__, "network") from exc
        text = b"".join(chunks).decode(enc, errors="replace")
        try:
            body: Any = _json.loads(text)
        except ValueError:
            body = text
        return HttpResponse(status, body, hdrs)

    def close(self) -> None:
        self._client.close()
