"""Verificação on-chain da conta do mint (autoridades, extensões Token-2022, decimais).

Formato esperado: resposta `getAccountInfo` com `encoding=jsonParsed` (NÃO verificado contra o RPC real neste ambiente).
Política padrão (fail-closed): rejeita mint com autoridade de mint ou de freeze ativa, extensões Token-2022 arriscadas,
decimais diferentes do configurado e conta ausente/ilegível. Exceções são explícitas por token em tokens.json
(`allow_mint_authority`, `allow_freeze_authority`) — ex.: stablecoins centralizadas como a moeda de referência."""

from __future__ import annotations

from typing import Any

from app.domain import TokenInfo
from app.execution.tx_validator import TOKEN, TOKEN_2022

RISKY_EXTENSIONS = {
    "transferFeeConfig", "transferHook", "permanentDelegate", "confidentialTransferMint", "defaultAccountState",
    "nonTransferable", "pausableConfig", "mintCloseAuthority", "scaledUiAmountConfig",
}


def evaluate_mint_account(value: dict[str, Any] | None, token: TokenInfo) -> list[str]:
    """Retorna a lista de problemas (vazia = aprovado)."""
    if not value:
        return ["conta do mint não encontrada"]
    problems: list[str] = []
    if value.get("owner") not in (TOKEN, TOKEN_2022):
        problems.append("mint não pertence a um programa de Token reconhecido")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or parsed.get("type") != "mint":
        return problems + ["mint não decodificável (resposta inesperada)"]
    if info.get("isInitialized") is False:
        problems.append("mint não inicializado")
    if info.get("decimals") != token.decimals:
        problems.append(f"decimais on-chain ({info.get('decimals')}) diferem do configurado ({token.decimals})")
    if info.get("mintAuthority") and not token.allow_mint_authority:
        problems.append("autoridade de mint ativa (suprimento pode ser inflado)")
    if info.get("freezeAuthority") and not token.allow_freeze_authority:
        problems.append("autoridade de freeze ativa (contas podem ser congeladas)")
    exts = info.get("extensions") or []
    names = {e.get("extension") for e in exts if isinstance(e, dict)}
    risky = sorted(n for n in names if n in RISKY_EXTENSIONS)
    if risky:
        problems.append("extensões Token-2022 arriscadas: " + ", ".join(risky))
    if value.get("owner") == TOKEN_2022 and not isinstance(info.get("extensions", []), list):
        problems.append("extensões Token-2022 ilegíveis")
    return problems
