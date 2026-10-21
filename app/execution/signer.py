"""Assinatura LOCAL (Ed25519). A chave privada nunca sai deste processo nem entra em logs/repr.

`Signer.sign_validated` só aceita `ValidatedTransaction` (produzida por validate_transaction)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from app.execution.tx_validator import ValidatedTransaction
from app.utils.base58 import b58decode, b58encode


class SignerError(Exception):
    pass


def _parse_secret(text: str) -> bytes:
    text = text.strip()
    try:
        if text.startswith("["):
            arr = bytes(json.loads(text))
        else:
            arr = b58decode(text)
    except (ValueError, TypeError) as exc:
        raise SignerError("formato de chave inválido") from exc
    if len(arr) not in (32, 64):
        raise SignerError("chave deve ter 32 (seed) ou 64 bytes (seed+pública)")
    return arr


class Signer:
    def __init__(self, secret: bytes, expected_pubkey: str):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed = secret[:32]
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if len(secret) == 64 and secret[32:] != pub:
            raise SignerError("chave pública embutida não corresponde à privada")
        self.public_key = b58encode(pub)
        if expected_pubkey and self.public_key != expected_pubkey:
            raise SignerError("chave pública derivada difere de BOT_WALLET_PUBLIC_KEY")
        if not expected_pubkey:
            raise SignerError("BOT_WALLET_PUBLIC_KEY é obrigatória para validar a carteira")

    @classmethod
    def from_config(cls, secret: str, secret_file: str, expected_pubkey: str) -> Signer:
        if secret_file:
            p = Path(secret_file)
            if not p.exists():
                raise SignerError("arquivo de chave não encontrado")
            if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SignerError("permissões do arquivo de chave muito abertas (use chmod 600)")
            secret = p.read_text(encoding="utf-8")
        if not secret:
            raise SignerError("nenhuma chave configurada")
        return cls(_parse_secret(secret), expected_pubkey)

    def sign_validated(self, tx: ValidatedTransaction) -> tuple[bytes, str]:
        """Retorna (transação assinada, assinatura base58). Outros slots (ex.: market maker) permanecem zerados."""
        if not isinstance(tx, ValidatedTransaction):
            raise SignerError("somente transações validadas podem ser assinadas")
        sig = self._key.sign(tx.parsed.message)
        raw = bytearray(tx.raw)
        off = tx.parsed.sig_offset + 64 * tx.signer_index
        raw[off : off + 64] = sig
        return bytes(raw), b58encode(sig)

    def __repr__(self) -> str:
        return f"Signer(public_key={self.public_key})"

    __str__ = __repr__

    def __reduce__(self) -> None:  # type: ignore[override]
        raise SignerError("Signer não é serializável")
