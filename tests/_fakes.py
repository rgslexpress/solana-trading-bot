"""Fakes determinísticos: rede (Jupiter + RPC), construtor de transações Solana e relógio."""

from __future__ import annotations

import base64
import struct
from typing import Any, Callable

from app.http import HttpResponse, TransportError
from app.utils.base58 import b58decode, b58encode
from app.execution import tx_validator as V


def pk(i: int) -> str:
    return b58encode(bytes([i]) * 32)


def _sv(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def build_tx(keys: list[str], instrs: list[tuple[int, list[int], bytes]], nsig: int = 1, versioned: bool = True, lookups: int = 0, n_ro_unsigned: int = 1) -> bytes:
    msg = bytearray()
    if versioned:
        msg.append(0x80)
    msg += bytes([nsig, 0, n_ro_unsigned])
    msg += _sv(len(keys)) + b"".join(b58decode(k) for k in keys)
    msg += b"\x07" * 32  # recent blockhash fictício
    msg += _sv(len(instrs))
    for pidx, accts, data in instrs:
        msg += bytes([pidx]) + _sv(len(accts)) + bytes(accts) + _sv(len(data)) + data
    if versioned:
        msg += _sv(lookups)
        for _ in range(lookups):
            msg += b"\x09" * 32 + _sv(1) + b"\x01" + _sv(0)
    return _sv(nsig) + b"\x00" * 64 * nsig + bytes(msg)


def sys_transfer(lamports: int) -> bytes:
    return struct.pack("<IQ", 2, lamports)


def route_data(in_amount: int = 100_000_000, out_amount: int = 50_000_000, slippage_bps: int = 50) -> bytes:
    """Argumentos Anchor do roteador (discriminador `route`, plano vazio, in/out, slippage, taxa de plataforma)."""
    return bytes([229, 23, 203, 151, 122, 227, 173, 42]) + struct.pack("<I", 0) + struct.pack("<QQHB", in_amount, out_amount, slippage_bps, 0)


def good_swap_tx(wallet: str, lamports: int = 0, nsig: int = 1, extra_ix: list | None = None, keys_extra: list[str] | None = None,
                 in_amount: int = 100_000_000, slippage_bps: int = 50) -> bytes:
    """Transação típica: ComputeBudget(limite+preço) + [Transfer SOL p/ ATA wSOL da carteira] + swap Jupiter."""
    keys = [wallet, V.associated_token_address(wallet, V.WSOL_MINT) if lamports else pk(50), pk(51), V.SYSTEM, V.COMPUTE_BUDGET, V.JUPITER_V6, V.TOKEN] + (keys_extra or [])
    if nsig == 2:
        keys.insert(1, pk(99))  # market maker
    off = 1 if nsig == 2 else 0
    ix = [
        (4 + off, [], struct.pack("<BI", 2, 200_000)),
        (4 + off, [], struct.pack("<BQ", 3, 1000)),
    ]
    if lamports:
        ix.append((3 + off, [0, 1 + off], sys_transfer(lamports)))
    ix.append((5 + off, [0, 1 + off, 2 + off], route_data(in_amount, 50_000_000, slippage_bps)))
    return build_tx(keys, ix + (extra_ix or []), nsig=nsig, n_ro_unsigned=3)


class FakeNet:
    """Transporte falso: roteia /order, /execute e chamadas JSON-RPC."""

    def __init__(self) -> None:
        self.order_queue: list[Any] = []
        self.execute_queue: list[Any] = []
        self.calls: list[tuple[str, str]] = []
        self.rpc: dict[str, Callable[[list], Any]] = {}
        self.last: dict[str, dict] = {}  # última requisição por rota: {"params","json","headers"}

    def request(self, method, url, *, headers=None, params=None, json=None, timeout=10.0):  # type: ignore[no-untyped-def]
        self.last[url.rsplit("/", 1)[-1]] = {"params": params, "json": json, "headers": headers, "method": method}
        if url.endswith("/order"):
            self.calls.append(("order", url))
            item = self.order_queue.pop(0) if len(self.order_queue) > 1 else self.order_queue[0]
        elif url.endswith("/execute"):
            self.calls.append(("execute", url))
            item = self.execute_queue.pop(0) if len(self.execute_queue) > 1 else self.execute_queue[0]
        else:
            m = json["method"]
            self.calls.append(("rpc:" + m, url))
            result = self.rpc[m](json["params"])
            item = HttpResponse(200, {"jsonrpc": "2.0", "id": json["id"], "result": result}, {})
        if isinstance(item, Exception):
            raise item
        return item

    def count(self, name: str) -> int:
        return sum(1 for c, _ in self.calls if c == name)


def ok(body: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body, {})


def order_body(tx: bytes | None, in_amount: int, out_amount: int, in_mint: str, out_mint: str, impact: float | None = 0.01) -> dict:
    b: dict[str, Any] = {"inAmount": str(in_amount), "outAmount": str(out_amount), "inputMint": in_mint, "outputMint": out_mint, "requestId": "req-1"}
    if tx is not None:
        b["transaction"] = base64.b64encode(tx).decode()
    if impact is not None:
        b["priceImpactPct"] = str(impact)
    return b


class Clock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


__all__ = ["FakeNet", "Clock", "build_tx", "good_swap_tx", "ok", "order_body", "pk", "sys_transfer", "TransportError"]
