"""Proteções do modo LIVE. A execução real exige TODAS as condições; qualquer ambiguidade => recusa."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import LIVE_CONFIRM_PHRASE, Settings, validate_settings
from app.domain import Mode


@dataclass(frozen=True)
class LiveAuthorization:
    """Prova de que o guard aprovou. O JupiterExecutor exige uma instância (criada só por `authorize_live`)."""

    wallet: str
    _created_by_guard: bool = False


def live_blockers(
    settings: Settings,
    *,
    cli_confirm: str,
    wallet_pubkey_from_signer: str | None,
    health_ok: bool,
    data_ok: bool,
    kill_switch_active: bool,
    allowlist_size: int,
) -> list[str]:
    """Lista de motivos que impedem o LIVE (vazia = liberado)."""
    why: list[str] = []
    if settings.mode != Mode.LIVE:
        why.append("BOT_MODE não é 'live'")
    if settings.live_confirm != LIVE_CONFIRM_PHRASE:
        why.append("BOT_LIVE_CONFIRM ausente/incorreta")
    if cli_confirm != LIVE_CONFIRM_PHRASE:
        why.append("flag --confirm-live ausente/incorreta")
    errs = validate_settings(settings)
    if errs:
        why.append("configuração inválida: " + "; ".join(errs))
    if not settings.jupiter_api_key:
        why.append("BOT_JUPITER_API_KEY ausente")
    if not settings.rpc_url.startswith("https://"):
        why.append("RPC deve usar https")
    if not settings.wallet_public_key:
        why.append("BOT_WALLET_PUBLIC_KEY ausente")
    if wallet_pubkey_from_signer is None:
        why.append("assinador não carregado/validado")
    elif wallet_pubkey_from_signer != settings.wallet_public_key:
        why.append("carteira do assinador difere da configurada")
    if allowlist_size == 0:
        why.append("allowlist de tokens vazia")
    if settings.execution.max_order_quote <= 0:
        why.append("max_order_quote inválido")
    if not health_ok:
        why.append("health check reprovado")
    if not data_ok:
        why.append("dados de mercado inválidos")
    if kill_switch_active:
        why.append("kill switch ativo")
    return why


def authorize_live(settings: Settings, **kw) -> tuple[LiveAuthorization | None, list[str]]:  # type: ignore[no-untyped-def]
    why = live_blockers(settings, **kw)
    if why:
        return None, why
    return LiveAuthorization(settings.wallet_public_key, True), []
