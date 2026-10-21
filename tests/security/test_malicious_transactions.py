"""Testes NEGATIVOS: transações maliciosas/inesperadas NUNCA podem ser assinadas, mesmo que o token esteja na allowlist."""

import struct

from app.execution import tx_validator as V
from app.utils.base58 import b58decode
from _fakes import build_tx, good_swap_tx, pk, route_data, sys_transfer
from _helpers import raises

W, ATTACKER, NEWACC = pk(1), pk(66), pk(70)
CTX = V.ValidationContext(W, max_lamports_out=1_000_000_000 + 10_000_000, max_in_amount=100_000_000, max_slippage_bps=50)
# índices: 0=W 1=NEWACC 2=ATTACKER 3=SYSTEM 4=TOKEN 5=JUPITER
KEYS = [W, NEWACC, ATTACKER, V.SYSTEM, V.TOKEN, V.JUPITER_V6]
SWAP = (5, [0, 1], route_data())


def tx(*ix):
    return build_tx(KEYS, list(ix) + [SWAP], n_ro_unsigned=3)


def reject(raw, frag):
    with raises(V.TxValidationError, frag):
        V.validate_transaction(raw, CTX)


def test_system_assign_of_wallet_account_is_rejected():
    reject(tx((3, [0], struct.pack("<I", 1) + b58decode(ATTACKER))), "System não permitida")  # Assign: roubaria a conta


def test_allocate_and_other_system_instructions_rejected():
    for kind in (8, 9, 10, 4, 5, 7, 11, 12):
        reject(tx((3, [0], struct.pack("<I", kind) + b"\x00" * 40)), "System não permitida")


def test_sol_transfer_to_attacker_rejected_even_within_cap():
    reject(tx((3, [0, 2], sys_transfer(1_000))), "destino não reconhecido")


def test_sol_transfer_to_attacker_disguised_with_syncnative_rejected():
    reject(tx((3, [0, 2], sys_transfer(1_000)), (4, [2], bytes([17]))), "destino não reconhecido")
    reject(tx((4, [2], bytes([17]))), "SyncNative")


def test_transfer_to_wallets_own_wsol_ata_is_accepted_and_capped():
    ata = V.associated_token_address(W, V.WSOL_MINT)
    keys = [W, ata, ATTACKER, V.SYSTEM, V.TOKEN, V.JUPITER_V6]
    ok = build_tx(keys, [(3, [0, 1], sys_transfer(500_000_000)), (4, [1], bytes([17])), (5, [0, 1], route_data())], n_ro_unsigned=3)
    V.validate_transaction(ok, CTX)
    over = build_tx(keys, [(3, [0, 1], sys_transfer(5_000_000_000)), (5, [0, 1], route_data())], n_ro_unsigned=3)
    with raises(V.TxValidationError, "teto"):
        V.validate_transaction(over, CTX)
    split = build_tx(keys, [(3, [0, 1], sys_transfer(900_000_000)), (3, [0, 1], sys_transfer(900_000_000)), (5, [0, 1], route_data())], n_ro_unsigned=3)
    with raises(V.TxValidationError, "teto"):  # várias transferências somam contra o teto
        V.validate_transaction(split, CTX)


def test_create_account_owner_and_token_account_owner_must_be_safe():
    def create(owner):
        return (3, [0, 1], struct.pack("<IQQ", 0, 2_039_280, 165) + b58decode(owner))

    reject(tx(create(ATTACKER)), "dono diferente")
    # conta criada com dono Token + inicializada para o atacante => rejeitada
    reject(tx(create(V.TOKEN), (4, [1], bytes([18]) + b58decode(ATTACKER))), "dono diferente da carteira")
    reject(tx(create(V.TOKEN), (4, [1, 2, 2], bytes([1]))), "dono diferente da carteira")  # InitializeAccount: dono = accounts[2]
    # fluxo legítimo: cria conta de token, deposita SOL nela, sincroniza e inicializa com dono = carteira
    good = tx(create(V.TOKEN), (4, [1], bytes([18]) + b58decode(W)), (3, [0, 1], sys_transfer(1_000)), (4, [1], bytes([17])))
    V.validate_transaction(good, CTX)


def test_router_arguments_must_be_readable_and_within_limits():
    keys = [W, NEWACC, V.JUPITER_V6]
    mk = lambda data: build_tx(keys, [(2, [0, 1], data)], n_ro_unsigned=1)  # noqa: E731
    with raises(V.TxValidationError, "in_amount"):
        V.validate_transaction(mk(route_data(in_amount=100_000_001)), CTX)  # tenta gastar mais que o autorizado
    with raises(V.TxValidationError, "slippage"):
        V.validate_transaction(mk(route_data(slippage_bps=5_000)), CTX)  # slippage de 50%
    with raises(V.TxValidationError, "não verificáveis"):
        V.validate_transaction(mk(b"\x01" * 40), CTX)  # discriminador desconhecido em modo estrito
    with raises(V.TxValidationError, "não verificáveis"):
        V.validate_transaction(mk(b""), CTX)
    loose = V.ValidationContext(W, max_lamports_out=0, strict_jupiter_args=False)
    v = V.validate_transaction(mk(b"\x01" * 40), loose)  # modo não estrito aceita, mas registra
    assert v.summary["rotas_nao_verificadas"] == 1


def test_token_authority_and_delegate_instructions_rejected_with_correct_program():
    for kind in (3, 4, 6, 7, 8, 12, 13, 14, 15):  # Transfer, Approve, SetAuthority, MintTo, Burn, TransferChecked...
        reject(tx((4, [0, 2, 2], bytes([kind]) + b"\x00" * 9)), "token não permitida")


def test_signer_count_and_wallet_position_rules():
    raw = good_swap_tx(W, nsig=2)
    ctx_one = V.ValidationContext(W, max_lamports_out=10**9, max_signers=1)
    with raises(V.TxValidationError, "signatários"):
        V.validate_transaction(raw, ctx_one)
    other_payer = build_tx([ATTACKER, W, V.JUPITER_V6], [(2, [0, 1], route_data())], nsig=2, n_ro_unsigned=1)
    with raises(V.TxValidationError, "fee payer"):
        V.validate_transaction(other_payer, CTX)


def test_allowlisted_token_does_not_make_a_malicious_transaction_acceptable():
    """O validador não conhece o token: mesmo com a cotação correta para um token da allowlist,
    a transação que contém Assign/Transfer indevido é recusada."""
    evil = tx((3, [0], struct.pack("<I", 1) + b58decode(ATTACKER)))
    with raises(V.TxValidationError):
        V.validate_transaction(evil, CTX)


def test_ata_derivation_properties():
    a = V.associated_token_address(W, V.WSOL_MINT)
    assert a == V.associated_token_address(W, V.WSOL_MINT)  # determinística
    assert a != V.associated_token_address(ATTACKER, V.WSOL_MINT)
    assert not V._on_curve(b58decode(a))  # PDA nunca está na curva
    base_y = (4 * pow(5, V._P - 2, V._P)) % V._P
    assert V._on_curve(base_y.to_bytes(32, "little"))  # ponto base do Ed25519 está na curva
