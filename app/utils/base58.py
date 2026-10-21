"""Codificação Base58 (alfabeto Bitcoin/Solana) sem dependências externas."""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise ValueError("caractere inválido em Base58")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_pubkey(text: str) -> bool:
    """True se `text` for uma chave pública Solana (32 bytes em Base58)."""
    if not isinstance(text, str) or not (32 <= len(text) <= 44):
        return False
    try:
        return len(b58decode(text)) == 32
    except ValueError:
        return False
