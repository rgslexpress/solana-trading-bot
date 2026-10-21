"""Verificações de saúde. Componentes críticos não saudáveis bloqueiam NOVAS ENTRADAS."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HealthCheck:
    name: str
    ok: bool
    critical: bool = True
    detail: str = ""


@dataclass
class HealthReport:
    checks: list[HealthCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.critical)

    @property
    def failures(self) -> list[HealthCheck]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        return "; ".join(f"{c.name}: {'OK' if c.ok else 'FALHA'} {c.detail}".strip() for c in self.checks)


class HealthMonitor:
    def __init__(self) -> None:
        self._checks: list[Callable[[], HealthCheck]] = []

    def register(self, fn: Callable[[], HealthCheck]) -> None:
        self._checks.append(fn)

    def run(self) -> HealthReport:
        out = HealthReport()
        for fn in self._checks:
            try:
                out.checks.append(fn())
            except Exception as exc:  # noqa: BLE001 - verificação que explode conta como falha crítica
                out.checks.append(HealthCheck(getattr(fn, "__name__", "check"), False, True, f"exceção: {type(exc).__name__}"))
        return out
