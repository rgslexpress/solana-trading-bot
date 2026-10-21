"""Transporte HTTP injetável (permite testar toda a integração sem rede)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class TransportError(Exception):
    """Falha de rede/timeout: o resultado da requisição é DESCONHECIDO."""


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
    """Implementação padrão com httpx (importado sob demanda)."""

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(follow_redirects=False)

    def request(self, method: str, url: str, *, headers=None, params=None, json=None, timeout: float = 10.0) -> HttpResponse:  # type: ignore[no-untyped-def]
        try:
            r = self._client.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
        except self._httpx.HTTPError as exc:  # inclui timeouts e erros de conexão
            raise TransportError(type(exc).__name__) from exc
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text
        return HttpResponse(r.status_code, body, dict(r.headers))

    def close(self) -> None:
        self._client.close()
