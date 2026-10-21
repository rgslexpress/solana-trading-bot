"""Cliente HTTP da Jupiter Swap API V2 (Meta-Aggregator): GET /order  ->  assinatura local  ->  POST /execute.

Base: https://api.jup.ag/swap/v2 ; autenticação: cabeçalho `x-api-key`.
Fluxo escolhido: /order + /execute (a Jupiter faz o envio/landing; não depende do RPC para enviar).
Alternativa /build (+ envio próprio) foi descartada na v1: exige montar/enviar a transação e perde o
roteamento gerenciado; pode ser avaliada depois (ver docs/LIMITACOES.md).

Os NOMES DE CAMPOS abaixo foram obtidos de resumos da documentação oficial e NÃO puderam ser conferidos
contra a resposta real neste ambiente (sem rede). O parser falha de forma fechada se algo essencial faltar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import ExecutionParams
from app.http import HttpResponse, Transport, TransportError
from app.resilience import CircuitBreaker, FatalError, RecoverableError, retry_read


@dataclass
class JupiterOrder:
    transaction_b64: str | None
    request_id: str
    in_amount: int
    out_amount: int
    input_mint: str | None
    output_mint: str | None
    price_impact_pct: float | None  # interpretado como PERCENTUAL (unidade não confirmada)
    raw: dict[str, Any]
    slippage_bps: int | None = None
    other_amount_threshold: int | None = None
    swap_mode: str | None = None
    last_valid_block_height: int | None = None


@dataclass
class ExecuteOutcome:
    kind: str  # "success" | "failed" | "rejected" | "unknown"
    signature: str | None = None
    in_amount: int | None = None  # totalInputAmount (valores refletidos na carteira); fallback: inputAmountResult
    out_amount: int | None = None
    error: str | None = None
    code: int | None = None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class JupiterClient:
    def __init__(self, base_url: str, api_key: str, transport: Transport, params: ExecutionParams, sleep=None, send_slippage_param: bool | None = None):  # type: ignore[no-untyped-def]
        import time as _t

        self.base, self.key, self.t, self.p = base_url.rstrip("/"), api_key, transport, params
        self.breaker = CircuitBreaker(params.breaker_failures, params.breaker_reset_s)
        self._sleep = sleep or _t.sleep
        self.send_slippage_param = params.send_slippage_param if send_slippage_param is None else send_slippage_param

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.key} if self.key else {}

    @staticmethod
    def _classify(r: HttpResponse) -> None:
        if r.status in (401, 403):
            raise FatalError("jupiter: erro de autenticação (verifique a API key)")
        if r.status == 429 or r.status >= 500:
            raise RecoverableError(f"jupiter http {r.status}")
        if r.status >= 400:
            raise FatalError(f"jupiter http {r.status}")

    # ------------------------------------------------------------------ cotação (leitura: pode repetir)
    def get_order(self, input_mint: str, output_mint: str, amount: int, taker: str | None) -> JupiterOrder:
        params: dict[str, Any] = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount)}
        if taker:
            params["taker"] = taker
        if self.send_slippage_param:
            params["slippageBps"] = int(self.p.max_slippage_bps)

        def once() -> JupiterOrder:
            try:
                r = self.t.request("GET", f"{self.base}/order", headers=self._headers(), params=params, timeout=self.p.http_timeout_s)
            except TransportError as exc:
                raise RecoverableError(f"jupiter rede: {exc}") from exc
            self._classify(r)
            b = r.body
            if not isinstance(b, dict):
                raise FatalError("jupiter: resposta de /order inválida")
            in_amt, out_amt = _int(b.get("inAmount")), _int(b.get("outAmount"))
            if in_amt is None or out_amt is None or in_amt <= 0 or out_amt <= 0:
                raise FatalError("jupiter: /order sem inAmount/outAmount válidos")
            if taker and not (b.get("transaction") and b.get("requestId")):
                # transaction == "" com taker => a Jupiter não conseguiu montar a transação (ver errorCode)
                raise FatalError(f"jupiter: /order sem transaction/requestId (errorCode={b.get('errorCode')})")
            # `priceImpact` é o campo vigente; `priceImpactPct` está deprecated (unidades NÃO confirmadas).
            impact = b.get("priceImpact", b.get("priceImpactPct"))
            try:
                impact_f = float(impact) if impact is not None else None
            except (TypeError, ValueError):
                impact_f = None
            return JupiterOrder(
                b.get("transaction"), str(b.get("requestId", "")), in_amt, out_amt, b.get("inputMint"), b.get("outputMint"), impact_f, b,
                _int(b.get("slippageBps")), _int(b.get("otherAmountThreshold")), b.get("swapMode"), _int(b.get("lastValidBlockHeight")),
            )

        return retry_read(once, max_retries=self.p.max_retries, base=self.p.backoff_base_s, cap=self.p.backoff_max_s, sleep=self._sleep, breaker=self.breaker)

    # ------------------------------------------------------------------ envio (NUNCA repetir)
    def execute(self, signed_tx_b64: str, request_id: str) -> ExecuteOutcome:
        """Uma única tentativa. Falha de rede/5xx/429 => 'unknown' (a transação PODE ter sido enviada)."""
        try:
            r = self.t.request("POST", f"{self.base}/execute", headers=self._headers(), json={"signedTransaction": signed_tx_b64, "requestId": request_id}, timeout=self.p.http_timeout_s)
        except TransportError as exc:
            return ExecuteOutcome("unknown", error=f"rede: {exc}")
        if r.status == 429 or r.status >= 500:
            return ExecuteOutcome("unknown", error=f"http {r.status}")
        if r.status >= 400:
            return ExecuteOutcome("rejected", error=f"http {r.status}")
        b = r.body
        if not isinstance(b, dict):
            return ExecuteOutcome("unknown", error="resposta ilegível")
        sig = b.get("signature")
        status, code = str(b.get("status", "")).lower(), _int(b.get("code"))
        # totalInput/OutputAmount = valores refletidos na carteira; *Result = contabilidade da rota (sem taxa)
        in_a = _int(b.get("totalInputAmount")) or _int(b.get("inputAmountResult"))
        out_a = _int(b.get("totalOutputAmount")) or _int(b.get("outputAmountResult"))
        if status == "success" and (code in (0, None)):
            return ExecuteOutcome("success", sig, in_a, out_a, code=code)
        if status == "failed" or (code is not None and code < 0):
            return ExecuteOutcome("failed", sig, in_a, out_a, error=str(b.get("error", "falha na execução")), code=code)
        return ExecuteOutcome("unknown", sig, error="status não reconhecido")
