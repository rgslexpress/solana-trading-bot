import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import LIVE_CONFIRM_PHRASE, Settings
from app.domain import Mode, OrderState, Side
from app.execution.base import TradeRequest
from app.execution.jupiter_client import JupiterClient
from app.execution.jupiter_executor import JupiterExecutor
from app.execution.live_guard import authorize_live, live_blockers
from app.execution.rpc import SolanaRpc
from app.execution.signer import Signer
from app.http import TransportError
from app.storage.database import Database
from app.utils.base58 import b58encode
from _fakes import Clock, FakeNet, good_swap_tx, ok, order_body
from _helpers import JUP, USDC, raises, utc

SIG = b58encode(bytes(range(64)))


class Env:
    """Ambiente completo com rede falsa, banco temporário e assinador real."""

    def __init__(self, balance_usdc_raw=500_000_000, sol_lamports=100_000_000, wallet_sol=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "b.sqlite3"))
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.signer = Signer(seed + pub, b58encode(pub))
        self.wallet = self.signer.public_key
        self.s = Settings(mode=Mode.LIVE, wallet_public_key=self.wallet, jupiter_api_key="k", live_confirm=LIVE_CONFIRM_PHRASE)
        self.net, self.clock = FakeNet(), Clock()
        self.sign_calls = 0
        orig = self.signer.sign_validated

        def counted(tx):
            self.sign_calls += 1
            return orig(tx)

        self.signer.sign_validated = counted  # type: ignore[method-assign]
        self.statuses: dict = {}
        self.net.rpc = {
            "getBalance": lambda p: {"value": sol_lamports},
            "getTokenAccountsByOwner": lambda p: {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(balance_usdc_raw)}}}}}}]},
            "getSignatureStatuses": lambda p: {"value": [self.statuses.get(p[0][0])]},
            "getTransaction": lambda p: self.tx_json,
            "getHealth": lambda p: "ok",
        }
        # JUP: in 100 USDC -> 50 JUP (preço 2.0). Deltas on-chain correspondentes (USDC 6 dec, JUP 6 dec)
        self.tx_json = {
            "meta": {
                "fee": 5000, "preBalances": [10**9], "postBalances": [10**9 - 5000],
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": USDC.mint, "owner": self.wallet, "uiTokenAmount": {"amount": "500000000"}},
                    {"accountIndex": 2, "mint": JUP.mint, "owner": self.wallet, "uiTokenAmount": {"amount": "0"}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": USDC.mint, "owner": self.wallet, "uiTokenAmount": {"amount": "400000000"}},
                    {"accountIndex": 2, "mint": JUP.mint, "owner": self.wallet, "uiTokenAmount": {"amount": "50000000"}},
                ],
            },
            "transaction": {"message": {"accountKeys": [{"pubkey": self.wallet}]}},
        }
        t = self.net
        from app.config import ExecutionParams

        xp = self.s.execution
        xp.backoff_base_s = 0.0
        self.client = JupiterClient("https://api.jup.ag/swap/v2", "k", t, xp, sleep=lambda s: None)
        self.rpc = SolanaRpc("https://rpc.example", t, 5.0, 2, 0.0, 0.0, sleep=lambda s: None)
        self.auth, why = authorize_live(
            self.s, cli_confirm=LIVE_CONFIRM_PHRASE, wallet_pubkey_from_signer=self.wallet, health_ok=True, data_ok=True, kill_switch_active=False, allowlist_size=1
        )
        assert self.auth, why
        self.states: list[str] = []

        def on_state(iid, st, **kw):
            self.db.set_intent_state(iid, st, **kw)
            self.states.append(st.value)

        self.ex = JupiterExecutor(
            settings=self.s, client=self.client, rpc=self.rpc, signer=self.signer, tokens={"JUP": JUP}, quote=USDC, authorization=self.auth,
            on_state=on_state, clock=self.clock, sleep=self.clock.sleep, confirm_timeout_s=10, poll_s=2,
        )

    def req(self, side=Side.BUY, amount=100.0, ref=2.0, liq=5e6, iid="i1"):
        self.db.create_intent(iid, side.value, JUP.mint, "JUP", "live", {"atr": 0.05})
        return TradeRequest(iid, side, JUP, USDC, amount, ref, liq, utc(2025, 1, 1))

    def good_order(self, in_amount=100_000_000, out_amount=50_000_000, impact=0.01):
        tx = good_swap_tx(self.wallet, nsig=1)
        return ok(order_body(tx, in_amount, out_amount, USDC.mint, JUP.mint, impact))


def test_happy_path_buy_confirmed_and_reconciled_from_chain():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG, "inputAmountResult": "100000000", "outputAmountResult": "50000000"})]
    e.statuses = {}
    # assinatura local é calculada pelo executor; o status on-chain é consultado por ela
    class Auto(dict):
        def get(self, k, d=None):
            return {"confirmationStatus": "confirmed", "err": None}
    e.statuses = Auto()
    r = e.ex.execute(e.req())
    assert r.state == OrderState.CONFIRMED and abs(r.base_qty - 50.0) < 1e-9 and abs(r.avg_price - 2.0) < 1e-9
    assert e.states == ["VALIDATED", "QUOTED", "SIGNED", "SUBMITTED", "CONFIRMED", "RECONCILED"]
    assert e.net.count("execute") == 1 and e.sign_calls == 1
    assert r.details["amounts_from_chain"] is True
    # a assinatura persistida é a local (verificável on-chain)
    assert e.db.get_intent("i1")["signature"] == r.signature


def test_quote_with_wrong_mint_or_excess_input_or_bad_price_is_rejected_without_signing():
    cases = {
        "mint": ok(order_body(good_swap_tx("x"), 100_000_000, 50_000_000, USDC.mint, USDC.mint)),
        "entrada maior": None,
        "preço": None,
    }
    e = Env()
    e.net.order_queue = [cases["mint"]]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "mint" in r.error and e.sign_calls == 0 and e.net.count("execute") == 0

    e = Env()
    e.net.order_queue = [e.good_order(in_amount=150_000_000)]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "mais entrada" in r.error and e.sign_calls == 0

    e = Env()  # recebe 40 JUP por 100 USDC => preço 2.5 (25% pior que a referência 2.0)
    e.net.order_queue = [e.good_order(out_amount=40_000_000)]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "bps pior" in r.error and e.sign_calls == 0

    e = Env()
    e.net.order_queue = [e.good_order(impact=5.0)]  # 5% de impacto informado
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "impacto" in r.error


def test_unsafe_transaction_is_never_signed():
    e = Env()
    bad = good_swap_tx("11111111111111111111111111111112")  # fee payer errado
    e.net.order_queue = [ok(order_body(bad, 100_000_000, 50_000_000, USDC.mint, JUP.mint))]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "NÃO assinada" in r.error and e.sign_calls == 0 and e.net.count("execute") == 0


def test_token_outside_allowlist_amount_limits_and_balance():
    from app.domain import TokenInfo

    e = Env()
    other = TokenInfo("RUG", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 5)
    e.db.create_intent("r", "BUY", other.mint, "RUG", "live", {})
    r = e.ex.execute(TradeRequest("r", Side.BUY, other, USDC, 10.0, 1.0, 5e6, utc(2025, 1, 1)))
    assert r.state == OrderState.FAILED and "allowlist" in r.error and e.net.count("order") == 0
    e = Env()
    r = e.ex.execute(e.req(amount=e.s.execution.max_order_quote + 1))
    assert r.state == OrderState.FAILED and "teto" in r.error
    e = Env(balance_usdc_raw=10_000_000)  # 10 USDC < 100
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "saldo" in r.error and e.net.count("order") == 0
    e = Env(sol_lamports=1_000)  # sem SOL para taxas
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "SOL" in r.error
    e = Env()
    r = e.ex.execute(e.req(liq=1_000.0))
    assert r.state == OrderState.FAILED and "liquidez" in r.error


def test_auth_error_is_not_retried_but_5xx_is():
    e = Env()
    e.net.order_queue = [ok({"error": "x"}, 401)]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "autenticação" in r.error and e.net.count("order") == 1
    e = Env()
    e.net.order_queue = [ok({}, 503), TransportError("Timeout"), e.good_order()]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG, "inputAmountResult": "100000000", "outputAmountResult": "50000000"})]
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "finalized", "err": None}})()
    r = e.ex.execute(e.req())
    assert r.state == OrderState.CONFIRMED and e.net.count("order") == 3
    e = Env()
    e.net.order_queue = [ok({}, 503)]
    r = e.ex.execute(e.req())  # esgota tentativas (max_retries=3 => 4 chamadas) e falha sem assinar
    assert r.state == OrderState.FAILED and e.sign_calls == 0 and e.net.count("order") == e.s.execution.max_retries + 1


def test_expired_quote_before_signing():
    e = Env()
    orig = e.ex.client.get_order

    def slow(*a, **k):
        res = orig(*a, **k)
        e.clock.t += 60  # cotação "envelhece" 60s antes da assinatura
        return res

    e.ex.client.get_order = slow  # type: ignore[method-assign]
    e.net.order_queue = [e.good_order()]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.EXPIRED and e.sign_calls == 0


def test_network_failure_on_execute_yields_unknown_and_never_resubmits():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [TransportError("ReadTimeout")]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.UNKNOWN and r.signature
    assert e.net.count("execute") == 1  # NÃO repetiu
    assert e.db.get_intent("i1")["state"] == "UNKNOWN"
    # a nova tentativa do mesmo token/lado é bloqueada pelo banco (intent ativo)
    assert not e.db.create_intent("i2", "BUY", JUP.mint, "JUP", "live", {})
    # reconciliação: ainda não encontrada e jovem => segue pendente
    e.statuses = {}
    row = dict(e.db.get_intent("i1"))
    assert e.ex.reconcile(row) is None and e.net.count("execute") == 1
    # transação aparece confirmada on-chain => CONFIRMED com quantidades do estado on-chain
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "confirmed", "err": None}})()
    res = e.ex.reconcile(row)
    assert res.state == OrderState.CONFIRMED and abs(res.base_qty - 50.0) < 1e-9 and abs(res.quote_amount - 100.0) < 1e-9
    assert e.db.get_intent("i1")["state"] == "RECONCILED"
    assert e.net.count("execute") == 1


def test_reconcile_failed_expired_and_signed_never_sent():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [TransportError("x")]
    e.ex.execute(e.req())
    row = dict(e.db.get_intent("i1"))
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "confirmed", "err": {"InstructionError": [0, "Custom"]}}})()
    assert e.ex.reconcile(row).state == OrderState.FAILED
    # não encontrada e antiga (blockhash expirado) => EXPIRED
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [TransportError("x")]
    e.ex.execute(e.req())
    old = dict(e.db.get_intent("i1"))
    old["created_ts"] = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    e.statuses = {}
    assert e.ex.reconcile(old).state == OrderState.EXPIRED
    # SIGNED (nunca chegou a SUBMITTED) => FAILED seguro
    e2 = Env()
    e2.db.create_intent("s1", "BUY", JUP.mint, "JUP", "live", {})
    for st in (OrderState.VALIDATED, OrderState.QUOTED, OrderState.SIGNED):
        e2.db.set_intent_state("s1", st, signature=SIG if st == OrderState.SIGNED else None)
    assert e2.ex.reconcile(dict(e2.db.get_intent("s1"))).state == OrderState.FAILED


def test_execute_rejected_or_failed_status():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"error": "bad"}, 400)]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and e.net.count("execute") == 1
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"status": "Failed", "code": -1002, "signature": SIG, "error": "slippage"})]
    e.statuses = {}
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and e.net.count("execute") == 1


def test_http_200_success_without_onchain_confirmation_becomes_unknown_not_confirmed():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG, "inputAmountResult": "100000000", "outputAmountResult": "50000000"})]
    e.statuses = {}  # RPC não conhece a assinatura
    r = e.ex.execute(e.req())
    assert r.state == OrderState.UNKNOWN and e.net.count("execute") == 1


def test_sell_flow_amounts_and_price():
    e = Env()
    # vende 50 JUP (ref 2.0) -> recebe 100 USDC
    e.tx_json["meta"]["preTokenBalances"] = [
        {"accountIndex": 1, "mint": USDC.mint, "owner": e.wallet, "uiTokenAmount": {"amount": "0"}},
        {"accountIndex": 2, "mint": JUP.mint, "owner": e.wallet, "uiTokenAmount": {"amount": "50000000"}},
    ]
    e.tx_json["meta"]["postTokenBalances"] = [
        {"accountIndex": 1, "mint": USDC.mint, "owner": e.wallet, "uiTokenAmount": {"amount": "100000000"}},
        {"accountIndex": 2, "mint": JUP.mint, "owner": e.wallet, "uiTokenAmount": {"amount": "0"}},
    ]
    tx = good_swap_tx(e.wallet, in_amount=50_000_000)
    e.net.order_queue = [ok(order_body(tx, 50_000_000, 100_000_000, JUP.mint, USDC.mint))]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG})]
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "confirmed", "err": None}})()
    e.net.rpc["getTokenAccountsByOwner"] = lambda p: {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "50000000"}}}}}}]}
    r = e.ex.execute(e.req(Side.SELL, amount=50.0))
    assert r.state == OrderState.CONFIRMED and abs(r.base_qty - 50.0) < 1e-9 and abs(r.quote_amount - 100.0) < 1e-9


def test_executor_requires_guard_authorization():
    e = Env()
    with raises(PermissionError):
        JupiterExecutor(settings=e.s, client=e.client, rpc=e.rpc, signer=e.signer, tokens={"JUP": JUP}, quote=USDC, authorization=None)
    from app.execution.live_guard import LiveAuthorization

    with raises(PermissionError):  # tentativa de forjar sem passar pelo guard
        JupiterExecutor(settings=e.s, client=e.client, rpc=e.rpc, signer=e.signer, tokens={"JUP": JUP}, quote=USDC, authorization=LiveAuthorization(e.wallet))
    with raises(PermissionError):  # autorização de outra carteira
        JupiterExecutor(settings=e.s, client=e.client, rpc=e.rpc, signer=e.signer, tokens={"JUP": JUP}, quote=USDC, authorization=LiveAuthorization("outra", True))


def test_live_guard_lists_every_blocker_and_private_key_alone_never_enables():
    s = Settings()  # padrão: PAPER, sem credenciais
    why = live_blockers(s, cli_confirm="", wallet_pubkey_from_signer=None, health_ok=False, data_ok=False, kill_switch_active=True, allowlist_size=0)
    joined = " | ".join(why)
    for frag in ("BOT_MODE", "BOT_LIVE_CONFIRM", "--confirm-live", "API_KEY", "PUBLIC_KEY", "assinador", "allowlist", "health", "dados", "kill switch"):
        assert frag in joined, frag
    s2 = Settings(wallet_secret="x" * 88)  # chave presente no ambiente, nada mais
    assert s2.mode == Mode.PAPER
    assert live_blockers(s2, cli_confirm="", wallet_pubkey_from_signer=None, health_ok=True, data_ok=True, kill_switch_active=False, allowlist_size=1)
    # só uma das duas confirmações não basta
    s3 = Settings(mode=Mode.LIVE, live_confirm=LIVE_CONFIRM_PHRASE, jupiter_api_key="k", wallet_public_key=b58encode(bytes([3]) * 32))
    assert any("--confirm-live" in w for w in live_blockers(s3, cli_confirm="", wallet_pubkey_from_signer=s3.wallet_public_key, health_ok=True, data_ok=True, kill_switch_active=False, allowlist_size=1))
    ok_, why2 = authorize_live(s3, cli_confirm=LIVE_CONFIRM_PHRASE, wallet_pubkey_from_signer=s3.wallet_public_key, health_ok=True, data_ok=True, kill_switch_active=False, allowlist_size=1)
    assert ok_ is not None and not why2


# ----------------------------------------------------------------------------------------------
# Auditoria: conformidade com o contrato documentado da Swap API V2 (campos vindos de documentação
# consultada por busca; a CONFORMIDADE REAL com a API não é provada por estes testes).
# ----------------------------------------------------------------------------------------------
def test_request_shape_matches_documented_v2_contract():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG, "totalInputAmount": "100000000", "totalOutputAmount": "50000000"})]
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "confirmed", "err": None}})()
    e.ex.execute(e.req())
    o, x = e.net.last["order"], e.net.last["execute"]
    assert o["method"] == "GET" and o["headers"] == {"x-api-key": "k"}
    assert set(o["params"]) == {"inputMint", "outputMint", "amount", "taker", "slippageBps"}
    assert o["params"]["inputMint"] == USDC.mint and o["params"]["outputMint"] == JUP.mint and o["params"]["amount"] == "100000000"
    assert o["params"]["taker"] == e.wallet and o["params"]["slippageBps"] == 50
    assert x["method"] == "POST" and x["headers"] == {"x-api-key": "k"} and set(x["json"]) == {"signedTransaction", "requestId"}
    assert x["json"]["requestId"] == "req-1"


def test_execute_prefers_total_amounts_and_falls_back_to_result_amounts():
    e = Env()
    e.net.rpc["getTransaction"] = lambda p: None  # sem dados on-chain: usa a resposta da API
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [ok({"status": "Success", "code": 0, "signature": SIG, "totalInputAmount": "100000000", "totalOutputAmount": "49500000",
                               "inputAmountResult": "99900000", "outputAmountResult": "49900000"})]
    e.statuses = type("A", (dict,), {"get": lambda self, k, d=None: {"confirmationStatus": "confirmed", "err": None}})()
    r = e.ex.execute(e.req())
    assert r.state == OrderState.CONFIRMED and abs(r.base_qty - 49.5) < 1e-9  # total* (carteira), não *Result (rota)


def test_quote_fields_slippage_swapmode_threshold_and_builderror_are_enforced():
    base = lambda e: order_body(good_swap_tx(e.wallet), 100_000_000, 50_000_000, USDC.mint, JUP.mint)  # noqa: E731
    cases = {
        "slippageBps": ({"slippageBps": 300}, "slippageBps"),
        "swapMode": ({"swapMode": "ExactOut"}, "swapMode"),
        "piso": ({"otherAmountThreshold": "30000000"}, "pior caso"),  # mínimo aceito 40% abaixo do esperado
    }
    for name, (patch, frag) in cases.items():
        e = Env()
        b = base(e)
        b.update(patch)
        e.net.order_queue = [ok(b)]
        r = e.ex.execute(e.req())
        assert r.state == OrderState.FAILED and frag in r.error and e.sign_calls == 0, (name, r.error)
    e = Env()
    e.net.order_queue = [ok({"inAmount": "100000000", "outAmount": "50000000", "transaction": "", "errorCode": 1, "requestId": "r"})]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "errorCode=1" in r.error and e.sign_calls == 0


def test_malicious_transaction_from_api_is_not_signed_nor_submitted():
    import struct
    from app.utils.base58 import b58decode
    from _fakes import build_tx, pk, route_data
    from app.execution import tx_validator as V

    e = Env()
    keys = [e.wallet, pk(2), V.SYSTEM, V.JUPITER_V6]
    evil = build_tx(keys, [(2, [0], struct.pack("<I", 1) + b58decode(pk(66))), (3, [0, 1], route_data())], n_ro_unsigned=2)  # System::Assign
    e.net.order_queue = [ok(order_body(evil, 100_000_000, 50_000_000, USDC.mint, JUP.mint))]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "NÃO assinada" in r.error and e.sign_calls == 0 and e.net.count("execute") == 0
    # rota que tenta gastar mais do que o autorizado (in_amount nos argumentos da instrução ≠ cotação)
    e = Env()
    greedy = good_swap_tx(e.wallet, in_amount=100_000_000_000)
    e.net.order_queue = [ok(order_body(greedy, 100_000_000, 50_000_000, USDC.mint, JUP.mint))]
    r = e.ex.execute(e.req())
    assert r.state == OrderState.FAILED and "in_amount" in r.error and e.sign_calls == 0


def test_reconcile_uses_last_valid_block_height_not_wall_clock():
    e = Env()
    e.net.order_queue = [e.good_order()]
    e.net.execute_queue = [TransportError("x")]
    e.statuses = {}
    heights = {"h": 1_000}
    e.net.rpc["getBlockHeight"] = lambda p: heights["h"]
    # injeta lastValidBlockHeight na cotação
    body = order_body(good_swap_tx(e.wallet), 100_000_000, 50_000_000, USDC.mint, JUP.mint)
    body["lastValidBlockHeight"] = 1_100
    e.net.order_queue = [ok(body)]
    e.ex.execute(e.req())
    row = dict(e.db.get_intent("i1"))
    assert json.loads(row["result_json"])["last_valid_block_height"] == 1_100
    row["created_ts"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()  # idade irrelevante
    assert e.ex.reconcile(row) is None  # altura 1000 <= 1100: a transação ainda poderia ser incluída
    heights["h"] = 1_101
    res = e.ex.reconcile(row)
    assert res.state == OrderState.EXPIRED and "blockhash" in e.db.get_intent("i1")["error"]
