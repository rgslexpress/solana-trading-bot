import os
import stat
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.execution import tx_validator as V
from app.execution.signer import Signer, SignerError
from app.utils.base58 import b58decode, b58encode
from _fakes import build_tx, good_swap_tx, pk, route_data, sys_transfer
from _helpers import raises


def _new_signer():
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return Signer(seed + pub, b58encode(pub)), seed + pub


W = pk(1)
CTX = V.ValidationContext(W, max_lamports_out=1_000_000_000 + 10_000_000)


def test_valid_swap_transaction_passes():
    v = V.validate_transaction(good_swap_tx(W, lamports=1_000_000_000), CTX)
    assert v.summary["lamports_out"] == 1_000_000_000 and V.JUPITER_V6 in v.summary["programas"]


def test_valid_with_market_maker_second_signer():
    v = V.validate_transaction(good_swap_tx(W, nsig=2), CTX)
    assert v.parsed.num_required == 2


def test_fee_payer_must_be_our_wallet():
    with raises(V.TxValidationError, "fee payer"):
        V.validate_transaction(good_swap_tx(pk(2)), CTX)


def test_unknown_program_rejected():
    unknown = pk(77)
    keys = [W, pk(50), V.COMPUTE_BUDGET, unknown]
    raw = build_tx(keys, [(3, [0, 1], b"\x01\x02")], n_ro_unsigned=2)
    with raises(V.TxValidationError, "programa não permitido"):
        V.validate_transaction(raw, CTX)
    # com o programa explicitamente na allowlist configurada, passa
    ctx2 = V.ValidationContext(W, V.DEFAULT_ALLOWED_PROGRAMS | {unknown}, 10**9, strict_jupiter_args=False)
    V.validate_transaction(raw, ctx2)


def test_sol_transfer_limits_and_source():
    with raises(V.TxValidationError, "teto"):
        V.validate_transaction(good_swap_tx(W, lamports=2_000_000_000), CTX)
    keys = [W, pk(50), V.SYSTEM, V.JUPITER_V6]
    stolen = build_tx(keys, [(2, [1, 0], sys_transfer(5)), (3, [0], route_data())], n_ro_unsigned=2)  # origem = conta 1, não a carteira
    with raises(V.TxValidationError, "origem"):
        V.validate_transaction(stolen, CTX)


def test_dangerous_token_instructions_rejected():
    for kind, label in ((6, "SetAuthority"), (4, "Approve"), (3, "Transfer"), (12, "TransferChecked")):
        keys = [W, pk(50), V.TOKEN, V.JUPITER_V6]
        raw = build_tx(keys, [(2, [1, 0], bytes([kind]) + b"\x00" * 9), (3, [0], route_data())], n_ro_unsigned=2)
        with raises(V.TxValidationError, "token não permitida"):
            V.validate_transaction(raw, CTX)


def test_close_account_destination_must_be_wallet():
    keys = [W, pk(50), pk(60), V.TOKEN, V.JUPITER_V6]
    ok = build_tx(keys, [(3, [1, 0, 0], bytes([9])), (4, [0], route_data())], n_ro_unsigned=2)
    V.validate_transaction(ok, CTX)
    bad = build_tx(keys, [(3, [1, 2, 0], bytes([9])), (4, [0], route_data())], n_ro_unsigned=2)
    with raises(V.TxValidationError, "CloseAccount"):
        V.validate_transaction(bad, CTX)


def test_ata_creation_only_for_our_wallet():
    keys = [W, pk(50), pk(60), pk(61), V.ATA, V.JUPITER_V6]
    bad = build_tx(keys, [(4, [0, 1, 2, 3], b"\x01"), (5, [0], route_data())], n_ro_unsigned=2)  # dono = pk(60)
    with raises(V.TxValidationError, "ATA"):
        V.validate_transaction(bad, CTX)
    ok = build_tx(keys, [(4, [0, 1, 0, 3], b"\x01"), (5, [0], route_data())], n_ro_unsigned=2)
    V.validate_transaction(ok, CTX)


def test_priority_fee_cap_and_missing_swap_and_lookup_program():
    import struct

    keys = [W, V.COMPUTE_BUDGET, V.JUPITER_V6]
    expensive = build_tx(keys, [(1, [], struct.pack("<BQ", 3, 50_000_000)), (2, [0], route_data())], n_ro_unsigned=2)
    with raises(V.TxValidationError, "prioridade"):
        V.validate_transaction(expensive, CTX)
    only_budget = build_tx([W, V.COMPUTE_BUDGET], [(1, [], struct.pack("<BI", 2, 1000))], n_ro_unsigned=1)
    with raises(V.TxValidationError, "swap"):
        V.validate_transaction(only_budget, CTX)
    via_alt = build_tx([W, V.JUPITER_V6], [(5, [0], route_data())], n_ro_unsigned=1, lookups=1)  # programa em índice de lookup table
    with raises(V.TxValidationError, "lookup"):
        V.validate_transaction(via_alt, CTX)


def test_garbage_truncated_and_oversized_rejected():
    for raw in (b"", b"\x01", b"\x01" + b"\x00" * 64, os_urandom_like(300)):
        with raises(V.TxValidationError):
            V.validate_transaction(raw, CTX)
    good = good_swap_tx(W)
    with raises(V.TxValidationError):
        V.validate_transaction(good[:-5], CTX)
    with raises(V.TxValidationError):
        V.validate_transaction(good + b"\x00", CTX)
    with raises(V.TxValidationError, "1232"):
        V.validate_transaction(b"\x01" + b"\x00" * 1300, CTX)


def os_urandom_like(n):
    return bytes((i * 37 + 11) % 256 for i in range(n))


def test_validated_transaction_cannot_be_forged():
    parsed = V.parse_transaction(good_swap_tx(W))
    with raises(V.TxValidationError):
        V.ValidatedTransaction(raw=b"x", parsed=parsed, signer_index=0)


def test_signer_signs_only_validated_and_signature_verifies():
    signer, _ = _new_signer()
    raw = good_swap_tx(signer.public_key, nsig=2)
    v = V.validate_transaction(raw, V.ValidationContext(signer.public_key, max_lamports_out=10**9))
    signed, sig_b58 = signer.sign_validated(v)
    off = v.parsed.sig_offset
    sig = signed[off : off + 64]
    assert sig == b58decode(sig_b58)
    Ed25519PublicKey.from_public_bytes(b58decode(signer.public_key)).verify(sig, v.parsed.message)  # lança se inválida
    assert signed[off + 64 : off + 128] == b"\x00" * 64  # slot do market maker intacto
    assert signed[off + 128 :] == raw[off + 128 :]
    with raises(SignerError, "validadas"):
        signer.sign_validated(raw)  # type: ignore[arg-type]


def test_signer_hides_secret_and_checks_wallet_match():
    signer, secret = _new_signer()
    text = repr(signer) + str(signer) + repr(vars(signer).keys())
    assert b58encode(secret) not in text and b58encode(secret[:32]) not in text
    with raises(SignerError, "difere"):
        Signer(secret, pk(5))
    with raises(SignerError, "obrigatória"):
        Signer(secret, "")
    import pickle

    with raises(SignerError):
        pickle.dumps(signer)


def test_signer_from_file_requires_strict_permissions():
    if os.name != "posix":  # a verificação de permissões 0600 só existe em POSIX; no Windows use ACLs/pasta do usuário
        return
    signer, secret = _new_signer()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "key.json")
        with open(p, "w") as f:
            f.write("[" + ",".join(str(b) for b in secret) + "]")
        os.chmod(p, 0o644)
        with raises(SignerError, "permissões"):
            Signer.from_config("", p, signer.public_key)
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        assert Signer.from_config("", p, signer.public_key).public_key == signer.public_key
