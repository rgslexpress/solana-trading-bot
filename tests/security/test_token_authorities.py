from app.domain import TokenInfo
from app.execution.token_checks import evaluate_mint_account
from app.execution.tx_validator import TOKEN, TOKEN_2022

T = TokenInfo("X", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6)


def acct(owner=TOKEN, decimals=6, mint_auth=None, freeze_auth=None, exts=None, typ="mint"):
    info = {"decimals": decimals, "mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "isInitialized": True, "supply": "1"}
    if exts is not None:
        info["extensions"] = [{"extension": e} for e in exts]
    return {"owner": owner, "data": {"program": "spl-token", "parsed": {"type": typ, "info": info}}}


def test_clean_mint_passes():
    assert evaluate_mint_account(acct(), T) == []


def test_active_authorities_rejected_unless_explicitly_allowed():
    assert any("mint" in p for p in evaluate_mint_account(acct(mint_auth="Abc"), T))
    assert any("freeze" in p for p in evaluate_mint_account(acct(freeze_auth="Abc"), T))
    allowed = TokenInfo("USDC", T.mint, 6, allow_mint_authority=True, allow_freeze_authority=True)
    assert evaluate_mint_account(acct(mint_auth="Abc", freeze_auth="Abc"), allowed) == []


def test_decimals_mismatch_rejected():  # protege contra erro de unidades (conversão errada de quantidades)
    assert any("decimais" in p for p in evaluate_mint_account(acct(decimals=9), T))


def test_token2022_risky_extensions_rejected():
    probs = evaluate_mint_account(acct(owner=TOKEN_2022, exts=["transferFeeConfig", "permanentDelegate", "metadataPointer"]), T)
    assert any("transferFeeConfig" in p and "permanentDelegate" in p for p in probs) and not any("metadataPointer" in p for p in probs)


def test_missing_unparseable_or_foreign_program_rejected():
    assert evaluate_mint_account(None, T) == ["conta do mint não encontrada"]
    assert any("decodific" in p for p in evaluate_mint_account({"owner": TOKEN, "data": ["abc", "base64"]}, T))
    assert any("decodific" in p for p in evaluate_mint_account(acct(typ="account"), T))
    assert any("reconhecido" in p for p in evaluate_mint_account(acct(owner="11111111111111111111111111111111"), T))


def test_startup_enters_safe_mode_when_a_token_fails_verification():
    import os
    import tempfile

    import numpy as np

    from app.config import Settings
    from app.execution.paper import PaperExecutor
    from app.monitoring.alerts import AlertManager
    from app.risk.kill_switch import KillSwitch
    from app.services.trading_service import TradingService
    from app.storage.database import Database
    from _helpers import JUP, USDC, ArrayProvider, make_candles, trend_series
    import pandas as pd

    class Verifying(PaperExecutor):
        def verify_token(self, token):
            return ["autoridade de freeze ativa"]

    entry = make_candles(trend_series(), 900)
    prov = ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        s = Settings(db_path=os.path.join(d, "b.sqlite3"))
        s.strategy.ema_trend, s.strategy.min_candles = 20, 100
        db = Database(s.db_path)
        svc = TradingService(s, db, prov, Verifying(s.execution, 1000.0), KillSwitch(db.kill_switch_load, db.kill_switch_save), AlertManager(db), {"JUP": JUP}, USDC,
                             clock=lambda: (entry.index[125] + pd.Timedelta(seconds=910)).to_pydatetime())
        rep = svc.startup()
        assert not rep.ok and any("verificação on-chain" in n and not ok for n, ok, _ in rep.steps)
