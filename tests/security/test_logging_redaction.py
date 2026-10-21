import io
import json
import logging

from app.logging_config import JsonFormatter, RedactingFilter, redact_text
from app.utils.base58 import b58encode

FAKE_SECRET = b58encode(bytes(range(64)))  # 64 bytes em base58 (formato de chave privada Solana)


def _capture(msg, **extra):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    h.addFilter(RedactingFilter())
    lg = logging.getLogger("t.redact")
    lg.handlers = [h]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.info(msg, **extra)
    return buf.getvalue()


def test_private_key_base58_is_redacted_in_message():
    out = _capture(f"carteira carregada {FAKE_SECRET}")
    assert FAKE_SECRET not in out and "REDIGIDO" in out


def test_secret_named_fields_in_data_are_redacted():
    out = _capture("evento", extra={"data": {"api_key": "KEY123", "wallet_secret": "abc", "ok": 1, "x-api-key": "K"}})
    obj = json.loads(out)
    assert "KEY123" not in out and obj["dados"]["ok"] == 1


def test_json_array_secret_and_kv_pairs_redacted():
    arr = "[" + ",".join(str(i) for i in range(64)) + "]"
    assert "REDIGIDO" in redact_text(f"key={arr}")
    assert "hunter2" not in redact_text("password=hunter2 e api_key: abc123")


def test_exception_traces_are_redacted():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    h.addFilter(RedactingFilter())
    lg = logging.getLogger("t.redact2")
    lg.handlers, lg.propagate = [h], False
    try:
        raise ValueError(f"falha com {FAKE_SECRET}")
    except ValueError:
        lg.exception("erro")
    assert FAKE_SECRET not in buf.getvalue()
