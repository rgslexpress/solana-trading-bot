"""Logging estruturado (JSON) com redação automática de segredos."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sequências longas em base58 (chaves/seeds) e campos conhecidos são mascarados.
_KEY_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,100}\b")
_SECRET_NAMES = ("secret", "private", "seed", "api_key", "apikey", "x-api-key", "token", "password")
_JSON_ARRAY_RE = re.compile(r"\[(?:\s*\d{1,3}\s*,){31,}\s*\d{1,3}\s*\]")
_PLACEHOLDER = "***REDIGIDO***"


def redact_text(text: str) -> str:
    text = _KEY_RE.sub(_PLACEHOLDER, text)
    text = _JSON_ARRAY_RE.sub(_PLACEHOLDER, text)
    return re.sub(
        r"(?i)((?:secret|private[_ ]?key|seed|api[_-]?key|x-api-key|password|bot_token)\s*[=:]\s*)([^\s,;\"']+)",
        r"\1" + _PLACEHOLDER,
        text,
    )


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (_PLACEHOLDER if any(n in str(k).lower() for n in _SECRET_NAMES) else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        extra = getattr(record, "data", None)
        if extra is not None:
            record.data = redact_obj(extra)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            payload["dados"] = data
        if record.exc_info:
            payload["excecao"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", as_json: bool = True, log_file: str | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level.upper())
    fmt: logging.Formatter = (
        JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
