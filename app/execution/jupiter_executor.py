"""Executor LIVE via Jupiter. Cada etapa é persistida ANTES da próxima; nenhum envio é repetido.

Fluxo: VALIDATED (limites/saldo) -> QUOTED (/order validada) -> [valida tx] -> SIGNED (assinatura persistida)
       -> SUBMITTED (persistido ANTES do POST) -> /execute (1 tentativa) -> confirmação on-chain -> CONFIRMED.
Resultado incerto => UNKNOWN + reconciliação por assinatura/estado on-chain (nunca reenvia)."""

from __future__ import annotations

import base64
import binascii
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import Settings
from app.domain import SOL_MINT, Mode, OrderState, Side, TokenInfo
from app.execution.base import ExecutionResult, Executor, StateCallback, TradeRequest
from app.execution.jupiter_client import JupiterClient, JupiterOrder
from app.execution.live_guard import LiveAuthorization
from app.execution.rpc import SolanaRpc, owner_deltas
from app.execution.token_checks import evaluate_mint_account
from app.execution.signer import Signer, SignerError
from app.execution.tx_validator import DEFAULT_ALLOWED_PROGRAMS, TxValidationError, ValidationContext, validate_transaction
from app.resilience import CircuitOpenError, FatalError, RecoverableError

log = logging.getLogger("jupiter")
RENT_ALLOWANCE_LAMPORTS = 10_000_000  # criação de contas associadas/wSOL temporárias


class JupiterExecutor(Executor):
    mode = Mode.LIVE

    def __init__(
        self, *, settings: Settings, client: JupiterClient, rpc: SolanaRpc, signer: Signer, tokens: dict[str, TokenInfo], quote: TokenInfo,
        authorization: LiveAuthorization | None, on_state: StateCallback | None = None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep, confirm_timeout_s: float = 30.0, poll_s: float = 2.0,
    ):
        if not isinstance(authorization, LiveAuthorization) or not authorization._created_by_guard:
            raise PermissionError("execução LIVE não autorizada pelo live_guard")
        if authorization.wallet != signer.public_key:
            raise PermissionError("autorização emitida para outra carteira")
        self.s, self.client, self.rpc, self.signer = settings, client, rpc, signer
        self.by_mint = {t.mint: t for t in tokens.values()}
        self.quote = quote
        self._on_state = on_state or (lambda *a, **k: None)
        self._clock, self._sleep, self._confirm_timeout, self._poll = clock, sleep, confirm_timeout_s, poll_s
        self.allowed_programs = frozenset(DEFAULT_ALLOWED_PROGRAMS | settings.allowed_programs())

    # ------------------------------------------------------------------ saldos
    def quote_balance(self) -> float:
        if self.quote.mint == SOL_MINT:
            lamports = self.rpc.get_sol_balance(self.signer.public_key)
            return max(lamports / 1e9 - self.s.risk.min_fee_reserve_sol, 0.0)
        return self.quote.from_raw(self.rpc.get_token_balance(self.signer.public_key, self.quote.mint))

    def verify_token(self, token: TokenInfo) -> list[str]:
        try:
            return evaluate_mint_account(self.rpc.get_account_info(token.mint), token)
        except (RecoverableError, FatalError, CircuitOpenError) as exc:
            return [f"não foi possível verificar o mint: {exc}"]  # fail-closed

    def token_balance(self, token: TokenInfo) -> float | None:
        try:
            return token.from_raw(self.rpc.get_token_balance(self.signer.public_key, token.mint))
        except (RecoverableError, FatalError, CircuitOpenError):
            return None

    def fee_reserve_ok(self) -> bool:
        try:
            return self.rpc.get_sol_balance(self.signer.public_key) >= int(self.s.risk.min_fee_reserve_sol * 1e9)
        except (RecoverableError, FatalError, CircuitOpenError):
            return False

    # ------------------------------------------------------------------ helpers
    def _fail(self, req: TradeRequest, msg: str, state: OrderState = OrderState.FAILED, **kw: Any) -> ExecutionResult:
        self._on_state(req.intent_id, state, error=msg)
        log.error("execução %s: %s", state.value, msg, extra={"data": {"intent": req.intent_id}})
        return ExecutionResult(req.intent_id, state, req.side, error=msg, **kw)

    def _check_order(self, req: TradeRequest, order: JupiterOrder, in_tok: TokenInfo, out_tok: TokenInfo, amount_raw: int) -> str | None:
        x = self.s.execution
        if order.input_mint and order.input_mint != in_tok.mint:
            return "mint de entrada da cotação difere do esperado"
        if order.output_mint and order.output_mint != out_tok.mint:
            return "mint de saída da cotação difere do esperado"
        if order.swap_mode and str(order.swap_mode).lower() != "exactin":
            return f"swapMode inesperado: {order.swap_mode}"
        if order.slippage_bps is not None and order.slippage_bps > x.max_slippage_bps:
            return f"slippageBps da cotação ({order.slippage_bps}) acima do limite ({x.max_slippage_bps:.0f})"
        if order.in_amount > amount_raw:
            return f"cotação pede mais entrada ({order.in_amount}) que o autorizado ({amount_raw})"
        in_h, out_h = in_tok.from_raw(order.in_amount), out_tok.from_raw(order.out_amount)
        # preço efetivo em moeda de referência por token
        eff = (in_h / out_h) if req.side == Side.BUY else (out_h / in_h)
        dev_bps = (eff / req.ref_price - 1) * 1e4 if req.side == Side.BUY else (1 - eff / req.ref_price) * 1e4
        if dev_bps > x.max_price_deviation_bps:
            return f"preço efetivo {dev_bps:.0f} bps pior que a referência (limite {x.max_price_deviation_bps:.0f})"
        if order.other_amount_threshold:  # pior caso aceito pela própria rota (mínimo de saída)
            worst_out = out_tok.from_raw(order.other_amount_threshold)
            worst = (in_h / worst_out) if req.side == Side.BUY else (worst_out / in_h)
            worst_bps = (worst / req.ref_price - 1) * 1e4 if req.side == Side.BUY else (1 - worst / req.ref_price) * 1e4
            if worst_bps > x.max_price_deviation_bps + x.max_slippage_bps:
                return f"pior caso da rota ({worst_bps:.0f} bps) acima do limite"
        if dev_bps > x.max_slippage_bps + x.max_price_impact_bps:
            return "slippage+impacto estimados acima do limite"
        if order.price_impact_pct is not None and abs(order.price_impact_pct) * 100 > x.max_price_impact_bps:
            return f"impacto de preço informado ({order.price_impact_pct}%) acima do limite"
        return None

    # ------------------------------------------------------------------ execução
    def execute(self, req: TradeRequest) -> ExecutionResult:
        x = self.s.execution
        in_tok, out_tok = (self.quote, req.token) if req.side == Side.BUY else (req.token, self.quote)
        # 1) validações locais
        if req.token.mint not in self.by_mint or req.token.mint == self.quote.mint:
            return self._fail(req, "token fora da allowlist")
        if req.amount <= 0 or req.ref_price <= 0 or not req.liquidity_usd or req.liquidity_usd < x.min_liquidity_usd:
            return self._fail(req, "requisição inválida ou liquidez insuficiente")
        notional = req.amount if req.side == Side.BUY else req.amount * req.ref_price
        if notional > x.max_order_quote + 1e-9:
            return self._fail(req, f"ordem acima do teto por ordem ({x.max_order_quote})")
        amount_raw = in_tok.to_raw(req.amount)
        if amount_raw <= 0:
            return self._fail(req, "quantidade zero após conversão de unidades")
        try:
            bal = self.rpc.get_sol_balance(self.signer.public_key) if in_tok.mint == SOL_MINT else self.rpc.get_token_balance(self.signer.public_key, in_tok.mint)
            if in_tok.mint == SOL_MINT:
                bal -= int(self.s.risk.min_fee_reserve_sol * 1e9)
            if bal < amount_raw:
                return self._fail(req, "saldo insuficiente (considerando reserva de taxas)")
            if not self.fee_reserve_ok():
                return self._fail(req, "saldo de SOL abaixo da reserva para taxas")
        except (RecoverableError, FatalError, CircuitOpenError) as exc:
            return self._fail(req, f"não foi possível consultar saldos: {exc}")
        self._on_state(req.intent_id, OrderState.VALIDATED)

        # 2) cotação + validação
        t0 = self._clock()
        try:
            order = self.client.get_order(in_tok.mint, out_tok.mint, amount_raw, self.signer.public_key)
        except (RecoverableError, FatalError, CircuitOpenError) as exc:
            return self._fail(req, f"cotação indisponível: {exc}")
        problem = self._check_order(req, order, in_tok, out_tok, amount_raw)
        if problem:
            return self._fail(req, "cotação rejeitada: " + problem)
        self._on_state(req.intent_id, OrderState.QUOTED)

        # 3) validação da transação e assinatura local
        try:
            raw = base64.b64decode(order.transaction_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            return self._fail(req, "transação da cotação não é base64 válido")
        sol_in = amount_raw if in_tok.mint == SOL_MINT else 0
        ctx = ValidationContext(
            self.signer.public_key, self.allowed_programs, max_lamports_out=sol_in + RENT_ALLOWANCE_LAMPORTS,
            max_in_amount=amount_raw, max_slippage_bps=int(x.max_slippage_bps), strict_jupiter_args=x.strict_router_args,
        )
        try:
            validated = validate_transaction(raw, ctx)
        except TxValidationError as exc:
            return self._fail(req, f"transação reprovada na validação (NÃO assinada): {exc}")
        if self._clock() - t0 > x.quote_max_age_s:
            return self._fail(req, "cotação expirou antes da assinatura", OrderState.EXPIRED)
        try:
            signed_raw, signature = self.signer.sign_validated(validated)
        except SignerError as exc:
            return self._fail(req, f"falha de assinatura: {exc}")
        self._on_state(req.intent_id, OrderState.SIGNED, signature=signature, result={"last_valid_block_height": order.last_valid_block_height})

        # 4) persiste SUBMITTED antes do envio; uma única tentativa
        self._on_state(req.intent_id, OrderState.SUBMITTED, signature=signature)
        outcome = self.client.execute(base64.b64encode(signed_raw).decode(), order.request_id)
        if outcome.signature and outcome.signature != signature:
            log.warning("assinatura retornada difere da local; usando a local para reconciliação")
        if outcome.kind == "rejected":
            return self._fail(req, f"/execute rejeitado ({outcome.error}); transação não foi aceita", signature=signature)
        if outcome.kind == "failed":
            st = self._chain_status(signature)
            if st == "confirmed":  # contradição: resolver pelo estado on-chain
                return self._finalize(req, in_tok, out_tok, signature, outcome.in_amount, outcome.out_amount)
            return self._fail(req, f"execução falhou: {outcome.error}", signature=signature)
        if outcome.kind == "unknown":
            self._on_state(req.intent_id, OrderState.UNKNOWN, note=outcome.error or "")
            return ExecutionResult(req.intent_id, OrderState.UNKNOWN, req.side, signature=signature, error=outcome.error)
        # success => ainda confirma on-chain
        deadline = self._clock() + self._confirm_timeout
        while True:
            st = self._chain_status(signature)
            if st == "confirmed":
                return self._finalize(req, in_tok, out_tok, signature, outcome.in_amount, outcome.out_amount)
            if st == "failed":
                return self._fail(req, "transação falhou on-chain", signature=signature)
            if self._clock() >= deadline:
                self._on_state(req.intent_id, OrderState.UNKNOWN, note="sem confirmação no prazo")
                return ExecutionResult(req.intent_id, OrderState.UNKNOWN, req.side, signature=signature, error="sem confirmação no prazo")
            self._sleep(self._poll)

    def _chain_status(self, signature: str) -> str:
        """'confirmed' | 'failed' | 'pending'. Erros de RPC contam como 'pending' (nunca assume sucesso)."""
        try:
            st = self.rpc.get_signature_status(signature)
        except (RecoverableError, FatalError, CircuitOpenError):
            return "pending"
        if not st:
            return "pending"
        if st.get("err"):
            return "failed"
        return "confirmed" if st.get("confirmationStatus") in ("confirmed", "finalized") else "pending"

    def _finalize(self, req: TradeRequest, in_tok: TokenInfo, out_tok: TokenInfo, signature: str, in_raw: int | None, out_raw: int | None) -> ExecutionResult:
        fee_lamports: int | None = None
        reconciled = False
        try:
            tx = self.rpc.get_transaction(signature)
            if tx:
                d = owner_deltas(tx, self.signer.public_key)
                fee_lamports = int((tx.get("meta") or {}).get("fee", 0))
                key_in = "native" if in_tok.mint == SOL_MINT else in_tok.mint
                key_out = "native" if out_tok.mint == SOL_MINT else out_tok.mint
                if key_in in d and key_out in d:
                    chain_in = -d[key_in] - (fee_lamports if key_in == "native" else 0)
                    chain_out = d[key_out] + (fee_lamports if key_out == "native" else 0)
                    if chain_in > 0 and chain_out > 0:
                        if in_raw and abs(chain_in - in_raw) / in_raw > 0.02:
                            log.warning("divergência entre resposta da API e saldos on-chain", extra={"data": {"api": in_raw, "chain": chain_in}})
                        in_raw, out_raw, reconciled = chain_in, chain_out, True
        except (RecoverableError, FatalError, CircuitOpenError):
            pass
        if not in_raw or not out_raw:
            # confirmado on-chain mas sem quantidades confiáveis => não fingimos saber: reconciliação posterior
            return self._unknown_amounts(req, signature)
        base_raw, quote_raw = (out_raw, in_raw) if req.side == Side.BUY else (in_raw, out_raw)
        base_qty, quote_amt = req.token.from_raw(base_raw), self.quote.from_raw(quote_raw)
        price = quote_amt / base_qty
        net_fee_quote = self.s.execution.network_fee_quote  # estimativa; o valor real (lamports) vai em `details`
        res = ExecutionResult(
            req.intent_id, OrderState.CONFIRMED, req.side, base_qty, quote_amt, price, net_fee_quote,
            ((price / req.ref_price - 1) if req.side == Side.BUY else (1 - price / req.ref_price)) * 1e4, signature, None, False,
            {"fee_lamports": fee_lamports, "amounts_from_chain": reconciled},
        )
        self._on_state(req.intent_id, OrderState.CONFIRMED, signature=signature, result={"price": price, "base_qty": base_qty, "quote": quote_amt, "fee_lamports": fee_lamports})
        if reconciled:
            self._on_state(req.intent_id, OrderState.RECONCILED, note="quantidades conferidas via getTransaction")
        return res

    def _unknown_amounts(self, req: TradeRequest, signature: str) -> ExecutionResult:
        # A transação está confirmada mas as quantidades não foram obtidas: marcamos UNKNOWN para o reconciliador
        # tentar novamente via getTransaction. Não há risco de reenvio (a assinatura já está confirmada).
        self._on_state(req.intent_id, OrderState.UNKNOWN, note="confirmada on-chain; quantidades pendentes")
        return ExecutionResult(req.intent_id, OrderState.UNKNOWN, req.side, signature=signature, error="quantidades pendentes")

    # ------------------------------------------------------------------ reconciliação
    def reconcile(self, intent: dict[str, Any]) -> ExecutionResult | None:
        """Resolve uma intenção SIGNED/SUBMITTED/UNKNOWN pela assinatura e pelo estado on-chain. Nunca reenvia."""
        import json

        iid, state, sig = intent["id"], OrderState(intent["state"]), intent.get("signature")
        side = Side(intent["side"])
        if state == OrderState.SIGNED:
            self._on_state(iid, OrderState.FAILED, error="assinada mas nunca enviada (reinício)", note="reconciliação")
            return ExecutionResult(iid, OrderState.FAILED, side, error="não enviada")
        if not sig:
            return None
        try:
            st = self.rpc.get_signature_status(sig)
        except (RecoverableError, FatalError, CircuitOpenError):
            return None
        if state == OrderState.SUBMITTED:
            self._on_state(iid, OrderState.UNKNOWN, note="reconciliação iniciada")
        if st and st.get("err"):
            self._on_state(iid, OrderState.FAILED, error="falhou on-chain", note="reconciliação")
            return ExecutionResult(iid, OrderState.FAILED, side, signature=sig, error="falhou on-chain")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            token = self.by_mint.get(intent["mint"])
            if token is None:
                return None
            in_tok, out_tok = (self.quote, token) if side == Side.BUY else (token, self.quote)
            try:
                tx = self.rpc.get_transaction(sig)
            except (RecoverableError, FatalError, CircuitOpenError):
                return None
            if not tx:
                return None
            d, fee = owner_deltas(tx, self.signer.public_key), int((tx.get("meta") or {}).get("fee", 0))
            ki = "native" if in_tok.mint == SOL_MINT else in_tok.mint
            ko = "native" if out_tok.mint == SOL_MINT else out_tok.mint
            if ki not in d or ko not in d:
                return None
            in_raw = -d[ki] - (fee if ki == "native" else 0)
            out_raw = d[ko] + (fee if ko == "native" else 0)
            if in_raw <= 0 or out_raw <= 0:
                return None
            base_raw, quote_raw = (out_raw, in_raw) if side == Side.BUY else (in_raw, out_raw)
            base_qty, quote_amt = token.from_raw(base_raw), self.quote.from_raw(quote_raw)
            req_json = json.loads(intent["request_json"] or "{}")
            self._on_state(iid, OrderState.CONFIRMED, result={"base_qty": base_qty, "quote": quote_amt, "fee_lamports": fee, "via": "reconciliacao"})
            self._on_state(iid, OrderState.RECONCILED, note="quantidades conferidas on-chain")
            return ExecutionResult(iid, OrderState.CONFIRMED, side, base_qty, quote_amt, quote_amt / base_qty, self.s.execution.network_fee_quote, 0.0, sig, None, False, {"request": req_json})
        # não encontrada: só declara EXPIRED quando a transação NÃO PODE mais ser incluída
        lvbh = None
        try:
            lvbh = (json.loads(intent.get("result_json") or "{}") or {}).get("last_valid_block_height")
        except (ValueError, TypeError):
            pass
        if lvbh:  # critério preciso: altura atual do bloco > lastValidBlockHeight informado pela Jupiter
            try:
                if self.rpc.get_block_height() > int(lvbh):
                    self._on_state(iid, OrderState.EXPIRED, error="blockhash expirado (altura > lastValidBlockHeight)", note="reconciliação")
                    return ExecutionResult(iid, OrderState.EXPIRED, side, signature=sig, error="expirada")
            except (RecoverableError, FatalError, CircuitOpenError):
                pass
            return None
        created = datetime.fromisoformat(intent["created_ts"])
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age >= self.s.execution.unknown_expire_after_s:
            self._on_state(iid, OrderState.EXPIRED, error="não encontrada on-chain após a validade do blockhash", note="reconciliação")
            return ExecutionResult(iid, OrderState.EXPIRED, side, signature=sig, error="expirada")
        return None
