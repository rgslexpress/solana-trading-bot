"""Consulta SOMENTE LEITURA dos saldos da carteira configurada (usa apenas a chave PÚBLICA).

  python scripts/inspect_wallet.py
Não carrega chave privada, não assina e não envia nada."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.execution.rpc import SolanaRpc  # noqa: E402
from app.http import HttpxTransport  # noqa: E402


def main() -> int:
    s = load_settings(".env" if Path(".env").exists() else None)
    if not s.wallet_public_key:
        print("BOT_WALLET_PUBLIC_KEY não configurada.")
        return 1
    tokens, quote = s.load_tokens()
    rpc = SolanaRpc(s.rpc_url, HttpxTransport(), s.execution.http_timeout_s)
    print(f"Carteira: {s.wallet_public_key}")
    print(f"SOL: {rpc.get_sol_balance(s.wallet_public_key) / 1e9:.6f} (reserva mínima p/ taxas: {s.risk.min_fee_reserve_sol})")
    for t in [quote, *tokens.values()]:
        raw = rpc.get_token_balance(s.wallet_public_key, t.mint)
        print(f"{t.symbol}: {t.from_raw(raw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
