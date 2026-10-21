import contextlib
import inspect
import io
import json
import os
import tempfile
from pathlib import Path

from app import factory
from app.config import LIVE_CONFIRM_PHRASE, Settings, load_settings
from app.domain import Mode, OrderState, Side
from app.execution import paper
from app.execution.base import Executor, ExecutionResult, TradeRequest
from app.execution.signer import Signer
from app.main import main
from app.monitoring.alerts import AlertManager
from app.risk.kill_switch import KillSwitch
from app.services.trading_service import TradingService
from app.storage.database import Database
from app.utils.base58 import b58encode
from _helpers import JUP, USDC, ArrayProvider, make_candles, raises, trend_series, utc

ROOT = Path(__file__).resolve().parent.parent.parent
TOKENS = {
    "quote": {"symbol": "USDC", "mint": USDC.mint, "decimals": 6},
    "allowlist": [{"symbol": "JUP", "mint": JUP.mint, "decimals": 6}],
}


def _env(tmp, extra=""):
    tokens = os.path.join(tmp, "tokens.json")
    Path(tokens).write_text(json.dumps(TOKENS))
    env = os.path.join(tmp, ".env")
    Path(env).write_text(f"BOT_TOKENS_FILE={tokens}\nBOT_DB_PATH={os.path.join(tmp, 'b.sqlite3')}\nBOT_REPORTS_DIR={tmp}\nBOT_LOG_FILE=\n{extra}")
    return env


def test_paper_executor_source_has_no_signing_or_network_capability():
    import ast

    tree = ast.parse(inspect.getsource(paper))
    idents = set()
    for node in ast.walk(tree):  # só código: imports, nomes e atributos (docstrings ficam de fora)
        if isinstance(node, ast.Import):
            idents |= {a.name.lower() for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            idents.add((node.module or "").lower())
            idents |= {a.name.lower() for a in node.names}
        elif isinstance(node, ast.Name):
            idents.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            idents.add(node.attr.lower())
        elif isinstance(node, ast.FunctionDef):
            idents.add(node.name.lower())
    for forbidden in ("signer", "jupiter", "httpx", "requests", "urllib", "socket", "sign", "submit", "sendtransaction", "wallet_secret", "rpc"):
        assert not any(forbidden in i for i in idents), (forbidden, [i for i in idents if forbidden in i])
    members = {n.lower() for n in dir(paper.PaperExecutor)}
    assert not any(k in n for n in members for k in ("sign", "send", "submit", "transmit", "wallet"))
    assert not any(k in n.lower() for n in dir(Executor) for k in ("sign", "send", "submit"))


def test_paper_runtime_never_loads_signer_even_if_secret_and_live_flags_present(monkeypatch=None):
    called = []
    orig = Signer.from_config
    Signer.from_config = classmethod(lambda cls, *a, **k: called.append(1) or orig(*a, **k))  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as d:
            s = Settings(mode=Mode.PAPER, wallet_secret="x" * 88, wallet_public_key=b58encode(bytes([4]) * 32), live_confirm=LIVE_CONFIRM_PHRASE, jupiter_api_key="k",
                         db_path=os.path.join(d, "b.sqlite3"), tokens_file=_env(d).replace(".env", "tokens.json"))
            rt = factory.build_runtime(s, cli_confirm=LIVE_CONFIRM_PHRASE, provider=ArrayProvider(make_candles([1.0] * 5, 900), make_candles([1.0] * 5, 3600)))
            assert rt.executor.mode == Mode.PAPER and isinstance(rt.executor, paper.PaperExecutor)
            assert not called, "Signer não pode ser carregado em PAPER"
    finally:
        Signer.from_config = orig  # type: ignore[method-assign]


def test_live_requires_double_confirmation_before_touching_secret():
    called = []
    orig = Signer.from_config
    Signer.from_config = classmethod(lambda cls, *a, **k: called.append(1) or orig(*a, **k))  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as d:
            base = dict(mode=Mode.LIVE, wallet_secret="x" * 88, wallet_public_key=b58encode(bytes([4]) * 32), jupiter_api_key="k", db_path=os.path.join(d, "b.sqlite3"))
            _env(d)
            for live_confirm, cli in (("", ""), (LIVE_CONFIRM_PHRASE, ""), ("", LIVE_CONFIRM_PHRASE), ("errado", "errado")):
                s = Settings(live_confirm=live_confirm, tokens_file=os.path.join(d, "tokens.json"), **base)
                with raises(factory.LiveBlocked, "confirmação"):
                    factory.build_runtime(s, cli_confirm=cli, provider=ArrayProvider(make_candles([1.0] * 5, 900), make_candles([1.0] * 5, 3600)))
            assert not called
    finally:
        Signer.from_config = orig  # type: ignore[method-assign]


def test_live_blocked_when_signer_invalid_even_with_confirmations():
    with tempfile.TemporaryDirectory() as d:
        _env(d)
        s = Settings(mode=Mode.LIVE, live_confirm=LIVE_CONFIRM_PHRASE, wallet_secret="lixo", wallet_public_key=b58encode(bytes([4]) * 32), jupiter_api_key="k",
                     db_path=os.path.join(d, "b.sqlite3"), tokens_file=os.path.join(d, "tokens.json"))
        with raises(factory.LiveBlocked, "assinador"):
            factory.build_runtime(s, cli_confirm=LIVE_CONFIRM_PHRASE, provider=ArrayProvider(make_candles([1.0] * 5, 900), make_candles([1.0] * 5, 3600)))


def test_cli_live_without_confirmation_exits_2_and_never_trades():
    with tempfile.TemporaryDirectory() as d:
        env = _env(d, "BOT_MODE=live\nBOT_WALLET_SECRET=" + "x" * 88 + "\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--env-file", env, "--once"])
        assert code == 2 and "LIVE BLOQUEADO" in err.getvalue()


def test_cli_show_config_masks_secrets_and_default_mode_is_paper():
    with tempfile.TemporaryDirectory() as d:
        env = _env(d, "BOT_JUPITER_API_KEY=SUPERSECRETKEY123\nBOT_WALLET_SECRET=" + "y" * 88 + "\nBOT_TELEGRAM_BOT_TOKEN=tg-token-999\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--env-file", env, "--show-config"])
        text = out.getvalue()
        assert code == 0 and "SUPERSECRETKEY123" not in text and "y" * 20 not in text and "tg-token-999" not in text
        assert json.loads(text)["mode"] == "paper"


def test_cli_invalid_config_blocks_startup():
    with tempfile.TemporaryDirectory() as d:
        env = _env(d, "BOT_RISK_RISK_PER_TRADE_PCT=5\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert main(["--env-file", env, "--show-config"]) == 2
        assert "inválida" in err.getvalue()


def test_env_example_and_gitignore_are_safe():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    kv = dict(line.split("=", 1) for line in env_example.splitlines() if "=" in line and not line.startswith("#"))
    assert kv["BOT_MODE"].strip() == "paper"
    assert kv.get("BOT_LIVE_CONFIRM", "").strip() == ""
    assert kv.get("BOT_WALLET_SECRET", "").strip() == "" and kv.get("BOT_JUPITER_API_KEY", "").strip() == ""
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pat in (".env", "*.sqlite3", "logs/", "*.key", "keypair*.json", "secrets/"):
        assert pat in gi, pat
    # nenhuma chave privada base58 de 64 bytes em arquivos de texto versionáveis
    import re

    pat = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{85,90}\b")
    for p in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")) + list(ROOT.rglob("*.json")) + [ROOT / ".env.example"]:
        if ".git" in p.parts or "reports" in p.parts:
            continue
        assert not pat.search(p.read_text(encoding="utf-8", errors="ignore")), f"possível chave em {p}"


class FailingExecutor(Executor):
    mode = Mode.PAPER

    def __init__(self):
        self.calls = 0

    def execute(self, req: TradeRequest) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(req.intent_id, OrderState.FAILED, req.side, error="falha simulada")

    def quote_balance(self):
        return 1000.0

    def fee_reserve_ok(self):
        return True


def test_repeated_execution_failures_trip_kill_switch():
    closes = trend_series(n_down=40)
    entry = make_candles(closes, 900)
    prov = ArrayProvider(entry, make_candles(__import__("numpy").linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        s = Settings(db_path=os.path.join(d, "b.sqlite3"))
        s.strategy.ema_trend, s.strategy.min_candles = 20, 100
        s.execution.max_consecutive_exec_failures = 1
        db = Database(s.db_path)
        ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
        clock = {"now": (entry.index[125] + __import__("pandas").Timedelta(seconds=910)).to_pydatetime()}
        ex = FailingExecutor()
        svc = TradingService(s, db, prov, ex, ks, AlertManager(db), {"JUP": JUP}, USDC, clock=lambda: clock["now"])
        assert svc.startup().ok
        for i in range(126, 140):
            clock["now"] = (entry.index[i] + __import__("pandas").Timedelta(seconds=910)).to_pydatetime()
            svc.run_cycle()
        assert ex.calls >= 1 and ks.active and "falhas de execução" in ks.reason
        # com o kill switch ativo, nenhuma nova ordem de compra é enviada
        n = ex.calls
        for i in range(140, 160):
            clock["now"] = (entry.index[i] + __import__("pandas").Timedelta(seconds=910)).to_pydatetime()
            svc.run_cycle()
        assert ex.calls == n


def test_settings_repr_never_contains_secrets():
    s = Settings(jupiter_api_key="KEY-SECRET-1", wallet_secret="WALLET-SECRET-2", telegram_bot_token="TG-SECRET-3")
    text = repr(s) + str(s)
    assert "SECRET" not in text


def test_failed_buy_is_not_retried_within_the_same_candle():
    pd = __import__("pandas")
    closes = trend_series(n_down=40)
    entry = make_candles(closes, 900)
    prov = ArrayProvider(entry, make_candles(__import__("numpy").linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        s = Settings(db_path=os.path.join(d, "b.sqlite3"))
        s.strategy.ema_trend, s.strategy.min_candles = 20, 100
        s.execution.max_consecutive_exec_failures = 99  # não deixa o kill switch mascarar o teste
        db = Database(s.db_path)
        ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
        clock = {"now": (entry.index[125] + pd.Timedelta(seconds=910)).to_pydatetime()}
        ex = FailingExecutor()
        svc = TradingService(s, db, prov, ex, ks, AlertManager(db), {"JUP": JUP}, USDC, clock=lambda: clock["now"])
        assert svc.startup().ok
        clock["now"] = (entry.index[127] + pd.Timedelta(seconds=910)).to_pydatetime()  # candle do sinal de BUY
        svc.run_cycle()
        first = ex.calls
        assert first == 1
        for _ in range(5):  # polls seguintes dentro do MESMO candle
            clock["now"] += pd.Timedelta(seconds=30)
            svc.run_cycle()
        assert ex.calls == first


def test_pending_unknown_intent_blocks_new_entries_via_health():
    with tempfile.TemporaryDirectory() as d:
        s = Settings(db_path=os.path.join(d, "b.sqlite3"))
        db = Database(s.db_path)
        db.create_intent("u1", "BUY", JUP.mint, "JUP", "live", {})
        for st in (OrderState.VALIDATED, OrderState.QUOTED, OrderState.SIGNED, OrderState.SUBMITTED, OrderState.UNKNOWN):
            db.set_intent_state("u1", st, signature="sig" if st == OrderState.SIGNED else None)
        entry = make_candles(trend_series(), 900)
        prov = ArrayProvider(entry, make_candles(__import__("numpy").linspace(80, 140, 240), 3600))
        ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
        svc = TradingService(s, db, prov, FailingExecutor(), ks, AlertManager(db), {"JUP": JUP}, USDC)
        rep = svc.health.run()
        assert not rep.ok and any(c.name == "transacoes_pendentes" for c in rep.failures)
