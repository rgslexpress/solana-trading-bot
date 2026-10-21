import os
import random
import tempfile

from app.monitoring.alerts import AlertManager, make_telegram_sender
from app.resilience import CircuitBreaker, CircuitOpenError, FatalError, RecoverableError, backoff_delay, retry_read
from app.storage.database import Database
from app.utils.base58 import b58encode
from _helpers import raises


def test_retry_only_recoverable_with_limit_and_backoff():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RecoverableError("rede")
        return "ok"

    assert retry_read(flaky, max_retries=3, base=0.5, cap=8, sleep=sleeps.append) == "ok" and len(calls) == 3 and len(sleeps) == 2
    fatal_calls = []

    def fatal():
        fatal_calls.append(1)
        raise FatalError("auth")

    with raises(FatalError):
        retry_read(fatal, max_retries=5, base=0.1, cap=1, sleep=lambda s: None)
    assert len(fatal_calls) == 1  # erro não recuperável não é repetido
    always = []

    def down():
        always.append(1)
        raise RecoverableError("x")

    with raises(RecoverableError):
        retry_read(down, max_retries=2, base=0.1, cap=1, sleep=lambda s: None)
    assert len(always) == 3  # 1 + 2 retentativas


def test_backoff_has_jitter_and_cap():
    rng = random.Random(1)
    ds = [backoff_delay(10, 0.5, 8.0, rng) for _ in range(50)]
    assert max(ds) <= 8.0 and len(set(round(d, 6) for d in ds)) > 10
    assert all(backoff_delay(0, 0.5, 8.0, rng) <= 0.5 for _ in range(20))


def test_circuit_breaker_opens_and_half_opens():
    t = [0.0]
    cb = CircuitBreaker(failures=3, reset_s=60, clock=lambda: t[0])
    for _ in range(3):
        cb.failure()
    with raises(CircuitOpenError):
        cb.before()
    t[0] = 61.0
    cb.before()  # meio-aberto permite uma tentativa
    cb.success()
    assert not cb.is_open


def test_alerts_redact_secrets_persist_and_never_raise():
    sent = []
    secret = b58encode(bytes(range(64)))
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "b.sqlite3"))
        am = AlertManager(db, telegram=lambda m: sent.append(m))
        am.critical(f"falha com chave {secret} e api_key=ABC123")
        row = db.conn.execute("SELECT message FROM alerts").fetchone()["message"]
        assert secret not in row and "ABC123" not in row and secret not in sent[0] and "ABC123" not in sent[0]

        def boom(m):
            raise RuntimeError("telegram fora do ar")

        AlertManager(db, telegram=boom).warning("segue funcionando")  # canal externo falho não derruba o núcleo
        assert make_telegram_sender("", "", lambda u, b: None) is None
        posts = []
        snd = make_telegram_sender("TOK", "42", lambda url, body: posts.append((url, body)))
        snd("olá")
        assert posts[0][0].endswith("/botTOK/sendMessage") and posts[0][1]["chat_id"] == "42"
