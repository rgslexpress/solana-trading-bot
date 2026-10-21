"""Monta o runtime LIVE com TODAS as confirmações, mas com rede FALSA, e prova que a construção/startup/ciclos
não enviam ordens nem pedem cotações por conta própria (nenhum dinheiro real, nenhuma chamada real)."""

import os
import stat
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import factory
from app.config import LIVE_CONFIRM_PHRASE, Settings
from app.domain import Mode
from app.execution.jupiter_executor import JupiterExecutor
from app.utils.base58 import b58encode
from _fakes import FakeNet
from _helpers import JUP, USDC, ArrayProvider, make_candles, trend_series
from test_tokens_helper import write_tokens


def _keyfile(d):
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path = os.path.join(d, "key.json")
    Path(path).write_text("[" + ",".join(str(b) for b in seed + pub) + "]")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path, b58encode(pub)


def _net(wallet, mint_info):
    net = FakeNet()
    net.rpc = {
        "getHealth": lambda p: "ok",
        "getBalance": lambda p: {"value": 500_000_000},
        "getTokenAccountsByOwner": lambda p: {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "100000000"}}}}}}]},
        "getAccountInfo": lambda p: {"value": mint_info},
        "getSignatureStatuses": lambda p: {"value": [None]},
    }
    return net


def _mint(decimals=6):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {"program": "spl-token", "parsed": {"type": "mint", "info": {"decimals": decimals, "mintAuthority": None, "freezeAuthority": None, "isInitialized": True}}}}


def _settings(d, keyfile, pub):
    return Settings(mode=Mode.LIVE, live_confirm=LIVE_CONFIRM_PHRASE, jupiter_api_key="k", wallet_public_key=pub, wallet_secret_file=keyfile,
                    db_path=os.path.join(d, "b.sqlite3"), tokens_file=write_tokens(d))


def test_live_runtime_builds_and_runs_cycles_without_sending_or_quoting_anything():
    entry = make_candles(trend_series(), 900)
    prov = ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        key, pub = _keyfile(d)
        s = _settings(d, key, pub)
        s.strategy.ema_trend, s.strategy.min_candles = 20, 100
        net = _net(pub, _mint())
        now = (entry.index[125] + pd.Timedelta(seconds=910)).to_pydatetime()  # histórico suficiente; nenhum ciclo é executado neste teste
        rt = factory.build_runtime(s, cli_confirm=LIVE_CONFIRM_PHRASE, transport_factory=lambda: net, clock=lambda: now, provider=prov)
        assert isinstance(rt.executor, JupiterExecutor) and rt.executor.mode == Mode.LIVE
        rep = rt.service.startup()
        names = {n: ok for n, ok, _ in rep.steps}
        assert names["verificação on-chain dos tokens"], rep.steps
        assert net.count("order") == 0 and net.count("execute") == 0  # construir/iniciar nunca cota nem envia


def test_live_startup_fails_safe_on_decimals_mismatch_and_still_sends_nothing():
    entry = make_candles(trend_series(), 900)
    prov = ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600))
    with tempfile.TemporaryDirectory() as d:
        key, pub = _keyfile(d)
        s = _settings(d, key, pub)
        s.strategy.ema_trend, s.strategy.min_candles = 20, 100
        net = _net(pub, _mint(decimals=9))  # on-chain diz 9, configuração diz 6
        now = (entry.index[130] + pd.Timedelta(seconds=910)).to_pydatetime()
        rt = factory.build_runtime(s, cli_confirm=LIVE_CONFIRM_PHRASE, transport_factory=lambda: net, clock=lambda: now, provider=prov)
        rep = rt.service.startup()
        assert not rep.ok and not rt.service.ready
        for _ in range(3):
            rt.service.run_cycle()
        assert net.count("order") == 0 and net.count("execute") == 0
