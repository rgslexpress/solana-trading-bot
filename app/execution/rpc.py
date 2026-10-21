"""Cliente JSON-RPC mínimo da Solana (leituras). Nunca envia transações assinadas por conta própria."""

from __future__ import annotations

from typing import Any

from app.http import HttpResponse, Transport, TransportError
from app.resilience import CircuitBreaker, FatalError, RecoverableError, retry_read


class SolanaRpc:
    def __init__(self, url: str, transport: Transport, timeout: float = 10.0, max_retries: int = 3, backoff_base: float = 0.5, backoff_max: float = 8.0, breaker: CircuitBreaker | None = None, sleep=None):  # type: ignore[no-untyped-def]
        self.url, self.t, self.timeout = url, transport, timeout
        self._retries, self._base, self._cap = max_retries, backoff_base, backoff_max
        self.breaker = breaker or CircuitBreaker()
        import time as _t

        self._sleep = sleep or _t.sleep
        self._id = 0

    def _call_once(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        try:
            r: HttpResponse = self.t.request("POST", self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, timeout=self.timeout)
        except TransportError as exc:
            raise RecoverableError(f"rpc rede: {exc}") from exc
        if r.status == 429 or r.status >= 500:
            raise RecoverableError(f"rpc http {r.status}")
        if r.status in (401, 403):
            raise FatalError("rpc: erro de autenticação")
        if r.status >= 400 or not isinstance(r.body, dict):
            raise FatalError(f"rpc: resposta inválida (http {r.status})")
        if "error" in r.body:
            code = r.body["error"].get("code") if isinstance(r.body["error"], dict) else None
            if code in (-32005, -32004):  # nó sobrecarregado / atrasado
                raise RecoverableError("rpc: nó indisponível")
            raise FatalError(f"rpc erro {code}")
        if "result" not in r.body:
            raise FatalError("rpc: resposta sem 'result'")
        return r.body["result"]

    def call(self, method: str, params: list[Any]) -> Any:
        return retry_read(lambda: self._call_once(method, params), max_retries=self._retries, base=self._base, cap=self._cap, sleep=self._sleep, breaker=self.breaker)

    # --------------------------------------------------------------- leituras
    def get_signature_status(self, signature: str) -> dict[str, Any] | None:
        res = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        vals = res.get("value") if isinstance(res, dict) else None
        return vals[0] if vals else None

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        res = self.call("getAccountInfo", [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return res.get("value") if isinstance(res, dict) else None

    def get_block_height(self) -> int:
        return int(self.call("getBlockHeight", [{"commitment": "confirmed"}]))

    def get_sol_balance(self, pubkey: str) -> int:
        res = self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(res["value"])

    def get_token_balance(self, owner: str, mint: str) -> int:
        res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total = 0
        for acc in res.get("value", []):
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return total

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])  # type: ignore[no-any-return]

    def health(self) -> bool:
        try:
            return self.call("getHealth", []) == "ok"
        except (RecoverableError, FatalError):
            return False


def owner_deltas(tx: dict[str, Any], owner: str) -> dict[str, int]:
    """Variação líquida de saldo do `owner` por mint (SOL nativo aparece sob a chave 'native')."""
    meta = tx.get("meta") or {}
    out: dict[str, int] = {}
    pre: dict[tuple[int, str], int] = {}
    for b in meta.get("preTokenBalances", []) or []:
        if b.get("owner") == owner:
            pre[(b["accountIndex"], b["mint"])] = int(b["uiTokenAmount"]["amount"])
    post: dict[tuple[int, str], int] = {}
    for b in meta.get("postTokenBalances", []) or []:
        if b.get("owner") == owner:
            post[(b["accountIndex"], b["mint"])] = int(b["uiTokenAmount"]["amount"])
    for k in set(pre) | set(post):
        out[k[1]] = out.get(k[1], 0) + post.get(k, 0) - pre.get(k, 0)
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if owner in keys:
        i = keys.index(owner)
        out["native"] = int(meta["postBalances"][i]) - int(meta["preBalances"][i])
    return out
