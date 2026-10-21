"""Kill switch persistente: bloqueia novas entradas e exige retomada explícita.

Ativá-lo NÃO cancela transações já enviadas à rede nem liquida posições: apenas impede novas
entradas/assinaturas de compra, preserva o estado e permite reconciliação."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)
RESUME_PHRASE = "RETOMAR_OPERACAO"


class KillSwitch:
    def __init__(
        self,
        load: Callable[[], tuple[bool, str]],
        save: Callable[[bool, str, str], None],
        alert: Callable[[str], None] | None = None,
    ):
        self._load, self._save, self._alert = load, save, alert
        self._active, self._reason = load()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reason(self) -> str:
        return self._reason

    def refresh(self) -> None:
        """Recarrega o estado persistido (outro componente, ex.: painel, pode ter acionado o kill switch)."""
        self._active, self._reason = self._load()

    def trip(self, reason: str, source: str = "auto") -> None:
        """Ativa (idempotente). Persistido antes de qualquer outra ação."""
        if self._active:
            return
        self._active, self._reason = True, reason
        self._save(True, reason, f"{source}@{datetime.now(timezone.utc).isoformat()}")
        log.critical("KILL SWITCH ACIONADO: %s", reason, extra={"data": {"fonte": source}})
        if self._alert:
            self._alert(f"KILL SWITCH ACIONADO: {reason}")

    def resume(self, confirmation: str, operator_note: str) -> bool:
        """Retomada exige frase explícita e uma justificativa."""
        if confirmation != RESUME_PHRASE or not operator_note.strip():
            return False
        self._active, self._reason = False, ""
        self._save(False, "", f"retomada: {operator_note.strip()}@{datetime.now(timezone.utc).isoformat()}")
        log.warning("kill switch desativado manualmente", extra={"data": {"nota": operator_note}})
        return True
