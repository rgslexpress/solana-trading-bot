"""Retry com backoff exponencial + jitter e circuit breaker para provedores instáveis.

Regra de ouro: operações FINANCEIRAS que enviam transações nunca passam por `retry` depois do envio."""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RecoverableError(Exception):
    """Erro transitório (rede, 429, 5xx): pode haver nova tentativa de operações idempotentes (leituras)."""


class FatalError(Exception):
    """Erro não recuperável (autenticação, requisição inválida): não repetir."""


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, failures: int = 5, reset_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.max_failures, self.reset_s, self._clock = failures, reset_s, clock
        self._fails, self._opened_at = 0, None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._clock() - self._opened_at >= self.reset_s:
            return False  # meio-aberto: permite uma tentativa
        return True

    def before(self) -> None:
        if self.is_open:
            raise CircuitOpenError("circuit breaker aberto")

    def success(self) -> None:
        self._fails, self._opened_at = 0, None

    def failure(self) -> None:
        self._fails += 1
        if self._fails >= self.max_failures:
            self._opened_at = self._clock()


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Full jitter: U(0, min(cap, base*2^attempt))."""
    r = rng or random
    return r.uniform(0, min(cap, base * (2**attempt)))


def retry_read(
    fn: Callable[[], T],
    *,
    max_retries: int,
    base: float,
    cap: float,
    sleep: Callable[[float], None] = time.sleep,
    breaker: CircuitBreaker | None = None,
) -> T:
    """Repete APENAS RecoverableError, até `max_retries`. FatalError sobe imediatamente."""
    attempt = 0
    while True:
        if breaker:
            breaker.before()
        try:
            out = fn()
            if breaker:
                breaker.success()
            return out
        except RecoverableError:
            if breaker:
                breaker.failure()
            if attempt >= max_retries:
                raise
            sleep(backoff_delay(attempt, base, cap))
            attempt += 1
