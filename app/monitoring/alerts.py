"""Alertas operacionais. O núcleo não depende de nenhum canal externo; Telegram é opcional."""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.logging_config import redact_text
from app.storage.database import Database

log = logging.getLogger("alertas")


class AlertManager:
    def __init__(self, db: Database | None = None, telegram: Callable[[str], None] | None = None):
        self.db, self.telegram = db, telegram
        self.sent: list[tuple[str, str]] = []

    def send(self, level: str, message: str) -> None:
        safe = redact_text(message)
        self.sent.append((level, safe))
        getattr(log, "error" if level in ("CRITICO", "ERRO") else "warning")(safe)
        if self.db:
            try:
                self.db.save_alert(level, safe)
            except Exception:  # noqa: BLE001 - alerta nunca pode derrubar o núcleo
                log.exception("falha ao persistir alerta")
        if self.telegram:
            try:
                self.telegram(f"[{level}] {safe}")
            except Exception as exc:  # noqa: BLE001
                log.warning("falha ao enviar alerta externo: %s", redact_text(str(exc)))

    def critical(self, message: str) -> None:
        self.send("CRITICO", message)

    def warning(self, message: str) -> None:
        self.send("AVISO", message)


def make_telegram_sender(token: str, chat_id: str, post: Callable[[str, dict[str, Any]], Any]) -> Callable[[str], None] | None:
    """Cria o enviador de Telegram. `post(url, json)` é injetado (httpx em produção)."""
    if not token or not chat_id:
        return None

    def send(text: str) -> None:
        post(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": text[:3500]})

    return send
