"""Monta o runtime conforme o modo. Em PAPER/BACKTEST a carteira e o assinador NUNCA são tocados."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import LIVE_CONFIRM_PHRASE, Settings
from app.data.providers import CandleProvider, CsvProvider, DataProviderError, GeckoTerminalProvider, SyntheticProvider
from app.domain import Mode, TokenInfo
from app.execution.base import Executor
from app.execution.paper import PaperExecutor
from app.http import Transport
from app.monitoring.alerts import AlertManager, make_telegram_sender
from app.risk.kill_switch import KillSwitch
from app.services.trading_service import TradingService
from app.storage.database import Database


class LiveBlocked(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Runtime:
    settings: Settings
    db: Database
    service: TradingService
    executor: Executor
    kill_switch: KillSwitch
    alerts: AlertManager
    provider: CandleProvider


def build_provider(s: Settings, transport_factory: Callable[[], Transport] | None = None) -> CandleProvider:
    d = s.data
    if d.source == "synthetic":
        return SyntheticProvider()
    if d.source == "csv":
        return CsvProvider(d.csv_dir, liquidity_usd=d.liquidity_usd_override or None)
    if transport_factory is None:
        from app.http import HttpxTransport

        transport_factory = HttpxTransport
    t = transport_factory()

    def http_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
        r = t.request("GET", url, params=params, timeout=s.execution.http_timeout_s, headers={"Accept": "application/json"})
        if r.status != 200 or not isinstance(r.body, dict):
            raise DataProviderError(f"provedor de candles http {r.status}")
        return r.body

    return GeckoTerminalProvider(d.geckoterminal_url, http_get, d.cache_ttl_s, liquidity_cap=d.liquidity_usd_override, include_empty_intervals=d.include_empty_intervals)


def build_runtime(
    s: Settings,
    *,
    cli_confirm: str = "",
    transport_factory: Callable[[], Transport] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider: CandleProvider | None = None,
    intel_transport: Transport | None = None,
) -> Runtime:
    db = Database(s.db_path)
    tokens, quote = s.load_tokens()
    ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
    telegram = None
    if s.telegram_bot_token and s.telegram_chat_id:
        from app.http import HttpxTransport

        tr = (transport_factory or HttpxTransport)()
        telegram = make_telegram_sender(s.telegram_bot_token, s.telegram_chat_id, lambda url, body: tr.request("POST", url, json=body, timeout=5.0))
    alerts = AlertManager(db, telegram)
    ks._alert = alerts.critical  # noqa: SLF001
    prov = provider or build_provider(s, transport_factory)
    on_state = lambda iid, st, **kw: db.set_intent_state(iid, st, **kw)  # noqa: E731
    reference = tokens.get(s.backtest.reference_symbol)

    if s.mode == Mode.PAPER:
        executor: Executor = PaperExecutor(s.execution, s.paper_capital, on_state)
    elif s.mode == Mode.LIVE:
        executor = _build_live(s, db, prov, tokens, quote, ks, cli_confirm, on_state, transport_factory, clock)
    else:
        raise ValueError("BACKTEST não usa o serviço de trading; use o comando de backtest")
    from app.intel.runtime import build_intel

    intel = build_intel(s, db.conn, tokens, intel_transport, transport_factory)
    svc = TradingService(s, db, prov, executor, ks, alerts, tokens, quote, reference, clock, intel=intel)
    return Runtime(s, db, svc, executor, ks, alerts, prov)


def _build_live(s: Settings, db: Database, prov: CandleProvider, tokens: dict[str, TokenInfo], quote: TokenInfo, ks: KillSwitch, cli_confirm: str, on_state: Callable[..., None], transport_factory: Callable[[], Transport] | None, clock: Callable[[], datetime]) -> Executor:
    # Importações do caminho LIVE ficam aqui: o modo PAPER nunca carrega assinatura/Jupiter.
    from app.execution.jupiter_client import JupiterClient
    from app.execution.jupiter_executor import JupiterExecutor
    from app.execution.live_guard import authorize_live
    from app.execution.rpc import SolanaRpc
    from app.execution.signer import Signer, SignerError
    from app.http import HttpxTransport

    # Pré-condições baratas ANTES de tocar no segredo.
    pre = [] if (s.live_confirm == LIVE_CONFIRM_PHRASE and cli_confirm == LIVE_CONFIRM_PHRASE) else ["confirmação dupla de LIVE ausente (BOT_LIVE_CONFIRM + --confirm-live)"]
    if pre:
        raise LiveBlocked(pre)
    try:
        signer = Signer.from_config(s.wallet_secret, s.wallet_secret_file, s.wallet_public_key)
    except SignerError as exc:
        raise LiveBlocked([f"assinador: {exc}"]) from exc
    t = (transport_factory or HttpxTransport)()
    x = s.execution
    rpc = SolanaRpc(s.rpc_url, t, x.http_timeout_s, x.max_retries, x.backoff_base_s, x.backoff_max_s)
    client = JupiterClient(s.jupiter_base_url, s.jupiter_api_key, t, x)
    now = clock()
    # data_ok: candles válidos para todos os tokens; health: banco + RPC
    probe = TradingService(s, db, prov, _NullExec(), ks, AlertManager(db), tokens, quote, None, clock)
    data_ok = all(probe._features(tok, now)[0] is not None for tok in tokens.values())  # noqa: SLF001
    health_ok = db.integrity_ok() and rpc.health()
    auth, why = authorize_live(
        s, cli_confirm=cli_confirm, wallet_pubkey_from_signer=signer.public_key, health_ok=health_ok, data_ok=data_ok,
        kill_switch_active=ks.active, allowlist_size=len(tokens),
    )
    if auth is None:
        raise LiveBlocked(why)
    return JupiterExecutor(settings=s, client=client, rpc=rpc, signer=signer, tokens=tokens, quote=quote, authorization=auth, on_state=on_state)


class _NullExec(Executor):
    """Executor inerte usado só para sondagem de dados antes de autorizar o LIVE."""

    mode = Mode.PAPER

    def execute(self, req):  # type: ignore[no-untyped-def]
        raise RuntimeError("sondagem não executa ordens")

    def quote_balance(self) -> float:
        return 0.0

    def fee_reserve_ok(self) -> bool:
        return True
