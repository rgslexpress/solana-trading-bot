"""DRY-RUN SOMENTE LEITURA contra a Jupiter REAL: pede cotação, decodifica e VALIDA a transação. NUNCA assina nem envia.

Serve para você conferir, com a API real, os campos da resposta, a unidade do impacto, os programas/discriminadores
usados nas rotas e se o validador aceita transações legítimas (ou onde ele precisa de ajuste).

  python scripts/dry_run_quote.py --symbol JUP --amount 5          # compra simulada: 5 USDC -> JUP
  python scripts/dry_run_quote.py --symbol JUP --amount 5 --raw    # imprime também o JSON bruto (sem a transação)

Requer BOT_JUPITER_API_KEY, BOT_WALLET_PUBLIC_KEY (apenas a CHAVE PÚBLICA) e acesso à internet. Não carrega segredos."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.execution.jupiter_client import JupiterClient  # noqa: E402
from app.execution.tx_validator import DEFAULT_ALLOWED_PROGRAMS, TxValidationError, ValidationContext, parse_transaction, validate_transaction  # noqa: E402
from app.http import HttpxTransport  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--amount", type=float, required=True, help="quantidade de moeda de referência a gastar (BUY)")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    s = load_settings(".env" if Path(".env").exists() else None)
    if not (s.jupiter_api_key and s.wallet_public_key):
        print("Defina BOT_JUPITER_API_KEY e BOT_WALLET_PUBLIC_KEY (somente a chave pública).")
        return 2
    tokens, quote = s.load_tokens()
    tok = tokens[a.symbol]
    client = JupiterClient(s.jupiter_base_url, s.jupiter_api_key, HttpxTransport(), s.execution)
    amount_raw = quote.to_raw(a.amount)
    order = client.get_order(quote.mint, tok.mint, amount_raw, s.wallet_public_key)
    safe = {k: v for k, v in order.raw.items() if k not in ("transaction",)}
    print("Campos recebidos em /order:", sorted(order.raw))
    print(f"inAmount={order.in_amount} outAmount={order.out_amount} slippageBps={order.slippage_bps} "
          f"otherAmountThreshold={order.other_amount_threshold} priceImpact(bruto)={order.price_impact_pct} swapMode={order.swap_mode} "
          f"lastValidBlockHeight={order.last_valid_block_height} requestId={'ok' if order.request_id else 'AUSENTE'}")
    if a.raw:
        print(json.dumps(safe, indent=2, default=str)[:4000])
    raw = base64.b64decode(order.transaction_b64 or "")
    try:
        p = parse_transaction(raw)
        print(f"Transação: versão={p.version} signatários={p.num_required} instruções={len(p.instructions)} lookups={p.lookups}")
        print("Programas:", sorted({p.keys[i.program_idx] for i in p.instructions if i.program_idx < len(p.keys)}))
    except TxValidationError as exc:
        print("Falha ao decodificar:", exc)
        return 1
    ctx = ValidationContext(
        s.wallet_public_key, DEFAULT_ALLOWED_PROGRAMS | s.allowed_programs(), max_lamports_out=10_000_000,
        max_in_amount=amount_raw, max_slippage_bps=int(s.execution.max_slippage_bps), strict_jupiter_args=s.execution.strict_router_args,
    )
    try:
        v = validate_transaction(raw, ctx)
        print("VALIDADOR: ACEITA (não assinada, não enviada).", v.summary)
        return 0
    except TxValidationError as exc:
        print("VALIDADOR: REJEITA ->", exc, "\n(Se for uma transação legítima, o validador precisa de ajuste ANTES de qualquer LIVE.)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
