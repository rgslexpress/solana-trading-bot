import json
import os
import tempfile

from app.config import ConfigError, Settings, load_settings, validate_settings
from app.domain import Mode
from _helpers import raises


def test_default_mode_is_paper_and_live_never_implicit():
    s = load_settings(env_file=None, environ={})
    assert s.mode == Mode.PAPER
    # presença de chave privada no ambiente NÃO muda o modo
    s2 = load_settings(env_file=None, environ={"BOT_WALLET_SECRET": "x" * 80})
    assert s2.mode == Mode.PAPER


def test_env_overrides_and_validation_errors():
    s = load_settings(env_file=None, environ={"BOT_RISK_RISK_PER_TRADE_PCT": "0.8", "BOT_STRATEGY_EMA_FAST": "5", "BOT_MODE": "backtest"})
    assert s.risk.risk_per_trade_pct == 0.8 and s.strategy.ema_fast == 5 and s.mode == Mode.BACKTEST
    with raises(ConfigError, "justificativa"):
        load_settings(env_file=None, environ={"BOT_RISK_RISK_PER_TRADE_PCT": "1.5"})
    with raises(ConfigError):
        load_settings(env_file=None, environ={"BOT_RISK_RISK_PER_TRADE_PCT": "3.0", "BOT_RISK_RISK_ABOVE_1PCT_JUSTIFICATION": "x"})
    with raises(ConfigError):
        load_settings(env_file=None, environ={"BOT_STRATEGY_EMA_FAST": "30", "BOT_STRATEGY_EMA_SLOW": "20"})
    with raises(ConfigError):
        load_settings(env_file=None, environ={"BOT_STRATEGY_ENTRY_TIMEFRAME": "7m"})
    with raises(ConfigError):
        load_settings(env_file=None, environ={"BOT_MODE": "yolo"})
    with raises(ConfigError):
        load_settings(env_file=None, environ={"BOT_RISK_ON_LIMIT_POLICY": "close_all"})


def test_redacted_dict_hides_secrets():
    s = Settings(jupiter_api_key="abc-key", wallet_secret="s3cr3t", telegram_bot_token="tok")
    text = json.dumps(s.redacted_dict())
    assert "abc-key" not in text and "s3cr3t" not in text and "tok\"" not in text
    assert "definido" in text


def test_tokens_file_validation():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tokens.json")
        good = {
            "quote": {"symbol": "USDC", "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6},
            "allowlist": [{"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6}],
        }
        with open(path, "w") as f:
            json.dump(good, f)
        tokens, quote = Settings(tokens_file=path).load_tokens()
        assert "JUP" in tokens and quote.symbol == "USDC"
        bad = json.loads(json.dumps(good))
        bad["allowlist"][0]["mint"] = "not-a-mint"
        with open(path, "w") as f:
            json.dump(bad, f)
        with raises(ConfigError, "mint inválido"):
            Settings(tokens_file=path).load_tokens()


def test_validate_returns_list():
    s = Settings()
    assert validate_settings(s) == []
    s.risk.max_token_exposure_pct = 90
    assert validate_settings(s)
