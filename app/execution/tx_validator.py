"""Decodificação e validação de transações Solana ANTES da assinatura (fail-closed).

Só é possível obter um `ValidatedTransaction` por meio de `validate_transaction`; o `Signer` só aceita esse tipo.
Limites desta validação (documentados em docs/LIMITACOES.md):
  * Instruções internas (CPI) das DEXs não são visíveis: confia-se nos programas da allowlist.
  * Destinos de SOL só são aceitos se forem a ATA de wSOL da carteira (derivada via PDA) ou contas criadas
    na própria transação com dono = programa de Token e inicializadas com dono = carteira.
  * Instruções do roteador Jupiter só são aceitas se os argumentos (in_amount, slippage) puderem ser lidos
    e estiverem dentro dos limites (modo estrito, padrão). Discriminadores NÃO foram confirmados contra a API real.
  * Contas vindas de Address Lookup Tables não podem ser verificadas => rejeitadas quando relevantes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from app.utils.base58 import b58encode

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
DEFAULT_ALLOWED_PROGRAMS = frozenset({SYSTEM, TOKEN, TOKEN_2022, ATA, COMPUTE_BUDGET, MEMO, JUPITER_V6})
MAX_TX_BYTES = 1232
WSOL_MINT = "So11111111111111111111111111111111111111112"
# System: somente CreateAccount(0), Transfer(2), CreateAccountWithSeed(3).
# Assign/Allocate* foram REMOVIDOS: Assign sobre a conta da carteira transferiria a posse dela a outro programa.
_SYSTEM_ALLOWED = {0: "CreateAccount", 2: "Transfer", 3: "CreateAccountWithSeed"}
_TOKEN_ALLOWED = {1, 16, 18, 17, 9}  # InitializeAccount(1/2/3 => 1,16,18), SyncNative(17), CloseAccount(9)
# Discriminadores Anchor do roteador Jupiter v6 (route / shared_accounts_route). Argumentos finais fixos:
# in_amount u64, quoted_out_amount u64, slippage_bps u16, platform_fee_bps u8 (19 bytes). NÃO confirmados contra a API V2.
_JUP_ROUTE_DISCS = {bytes([229, 23, 203, 151, 122, 227, 173, 42]), bytes([193, 32, 155, 51, 65, 214, 156, 129])}

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program: str) -> str:
    import hashlib

    from app.utils.base58 import b58decode

    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("PDA não encontrada")


def associated_token_address(wallet: str, mint: str, token_program: str = TOKEN) -> str:
    from app.utils.base58 import b58decode

    return find_program_address([b58decode(wallet), b58decode(token_program), b58decode(mint)], ATA)


class TxValidationError(Exception):
    """Transação rejeitada: NÃO assinar."""


@dataclass
class Instr:
    program_idx: int
    accounts: list[int]
    data: bytes


@dataclass
class ParsedTx:
    num_signatures: int
    message: bytes  # bytes a assinar
    version: int | str
    num_required: int
    keys: list[str]
    instructions: list[Instr]
    lookups: int
    sig_offset: int  # posição do primeiro slot de assinatura


def _shortvec(b: bytes, o: int) -> tuple[int, int]:
    val, shift = 0, 0
    while True:
        if o >= len(b):
            raise TxValidationError("shortvec truncado")
        byte = b[o]
        o += 1
        val |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return val, o
        shift += 7
        if shift > 14:
            raise TxValidationError("shortvec inválido")


def parse_transaction(raw: bytes) -> ParsedTx:
    if len(raw) > MAX_TX_BYTES:
        raise TxValidationError("transação maior que 1232 bytes")
    try:
        nsig, o = _shortvec(raw, 0)
        sig_offset = o
        o += 64 * nsig
        msg_start = o
        if o >= len(raw):
            raise TxValidationError("mensagem ausente")
        first = raw[o]
        version: int | str = "legacy"
        if first & 0x80:
            version = first & 0x7F
            if version != 0:
                raise TxValidationError(f"versão de mensagem não suportada: {version}")
            o += 1
        n_req, n_ro_s, n_ro_u = raw[o], raw[o + 1], raw[o + 2]
        o += 3
        nkeys, o = _shortvec(raw, o)
        keys = [b58encode(raw[o + 32 * i : o + 32 * (i + 1)]) for i in range(nkeys)]
        if len(raw) < o + 32 * nkeys + 32:
            raise TxValidationError("chaves truncadas")
        o += 32 * nkeys + 32  # + recent blockhash
        ninstr, o = _shortvec(raw, o)
        instrs: list[Instr] = []
        for _ in range(ninstr):
            pidx = raw[o]
            o += 1
            na, o = _shortvec(raw, o)
            accts = list(raw[o : o + na])
            o += na
            nd, o = _shortvec(raw, o)
            data = raw[o : o + nd]
            if len(data) != nd:
                raise TxValidationError("dados de instrução truncados")
            o += nd
            instrs.append(Instr(pidx, accts, data))
        lookups = 0
        if version == 0:
            lookups, o = _shortvec(raw, o)
            for _ in range(lookups):
                o += 32
                nw, o = _shortvec(raw, o)
                o += nw
                nr, o = _shortvec(raw, o)
                o += nr
        if o != len(raw):
            raise TxValidationError("bytes excedentes após a mensagem")
    except IndexError as exc:
        raise TxValidationError("transação truncada") from exc
    if n_ro_s > n_req or n_req > nkeys or nsig != n_req:
        raise TxValidationError("cabeçalho/assinaturas inconsistentes")
    return ParsedTx(nsig, raw[msg_start:], version, n_req, keys, instrs, lookups, sig_offset)


@dataclass
class ValidationContext:
    wallet: str
    allowed_programs: frozenset[str] = DEFAULT_ALLOWED_PROGRAMS
    max_lamports_out: int = 0  # teto de lamports que podem sair da carteira (transferências/criação de contas)
    max_priority_fee_lamports: int = 2_000_000
    max_signers: int = 2  # carteira + (opcional) market maker do JupiterZ
    max_instructions: int = 24
    max_in_amount: int | None = None  # in_amount máximo aceito nos argumentos do roteador Jupiter
    max_slippage_bps: int | None = None
    strict_jupiter_args: bool = True  # instrução Jupiter sem argumentos legíveis => rejeita


_SENTINEL = object()


@dataclass(frozen=True)
class ValidatedTransaction:
    raw: bytes
    parsed: ParsedTx
    signer_index: int
    summary: dict = field(default_factory=dict)
    _token: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._token is not _SENTINEL:
            raise TxValidationError("ValidatedTransaction só pode ser criada por validate_transaction")


def _u64(b: bytes, o: int) -> int:
    if o < 0 or o + 8 > len(b):
        raise TxValidationError("instrução malformada")
    return struct.unpack_from("<Q", b, o)[0]


def validate_transaction(raw: bytes, ctx: ValidationContext) -> ValidatedTransaction:
    tx = parse_transaction(raw)
    keys = tx.keys
    if not keys or keys[0] != ctx.wallet:
        raise TxValidationError("fee payer difere da carteira do robô")
    if ctx.wallet not in keys[: tx.num_required]:
        raise TxValidationError("carteira não é signatária")
    if tx.num_required > ctx.max_signers:
        raise TxValidationError("signatários demais")
    if len(tx.instructions) == 0 or len(tx.instructions) > ctx.max_instructions:
        raise TxValidationError("quantidade de instruções fora do limite")

    wsol_ata = associated_token_address(ctx.wallet, WSOL_MINT)
    own_sol_accounts: set[str] = {wsol_ata}  # onde SOL pode ser depositado/sincronizado
    lamports_out = 0
    cu_limit: int | None = None
    cu_price = 0
    programs_seen: set[str] = set()
    jup_checked = 0
    jup_unverified = 0

    for ins in tx.instructions:
        if ins.program_idx >= len(keys):
            raise TxValidationError("programa referenciado via lookup table (não verificável)")
        prog = keys[ins.program_idx]
        programs_seen.add(prog)
        if prog not in ctx.allowed_programs:
            raise TxValidationError(f"programa não permitido: {prog}")

        def acct(i: int, _ins: Instr = ins) -> str | None:
            return keys[_ins.accounts[i]] if i < len(_ins.accounts) and _ins.accounts[i] < len(keys) else None

        if prog == SYSTEM:
            if len(ins.data) < 4:
                raise TxValidationError("instrução System malformada")
            kind = struct.unpack_from("<I", ins.data, 0)[0]
            if kind not in _SYSTEM_ALLOWED:
                raise TxValidationError(f"instrução System não permitida (tipo {kind})")
            if kind == 2:  # Transfer: [from, to]
                if acct(0) != ctx.wallet:
                    raise TxValidationError("Transfer com origem diferente da carteira")
                if acct(1) not in own_sol_accounts:
                    raise TxValidationError("Transfer de SOL para destino não reconhecido (não é conta própria)")
                lamports_out += _u64(ins.data, 4)
            elif kind == 0:  # CreateAccount: [funder, new]; data: lamports, space, owner
                if acct(0) != ctx.wallet or acct(1) is None:
                    raise TxValidationError("CreateAccount financiada por outra conta")
                owner = b58encode(ins.data[20:52]) if len(ins.data) >= 52 else ""
                if owner not in (TOKEN, TOKEN_2022):
                    raise TxValidationError("CreateAccount com dono diferente do programa de Token")
                lamports_out += _u64(ins.data, 4)
                own_sol_accounts.add(acct(1))  # type: ignore[arg-type]
            elif kind == 3:  # CreateAccountWithSeed: [funder, new, base?]; data: base, seed, lamports, space, owner
                if acct(0) != ctx.wallet or acct(1) is None:
                    raise TxValidationError("CreateAccountWithSeed financiada por outra conta")
                seed_len = _u64(ins.data, 36)
                o = 44 + seed_len
                lamports_out += _u64(ins.data, o)
                owner = b58encode(ins.data[o + 16 : o + 48]) if len(ins.data) >= o + 48 else ""
                if owner not in (TOKEN, TOKEN_2022):
                    raise TxValidationError("CreateAccountWithSeed com dono diferente do programa de Token")
                own_sol_accounts.add(acct(1))  # type: ignore[arg-type]
        elif prog in (TOKEN, TOKEN_2022):
            kind = ins.data[0] if ins.data else -1
            if kind not in _TOKEN_ALLOWED:
                raise TxValidationError(f"instrução de token não permitida (tipo {kind})")
            if kind == 9 and acct(1) != ctx.wallet:  # CloseAccount: destino dos lamports = accounts[1]
                raise TxValidationError("CloseAccount com destino diferente da carteira")
            if kind == 1 and acct(2) != ctx.wallet:  # InitializeAccount: [conta, mint, dono, rent]
                raise TxValidationError("InitializeAccount com dono diferente da carteira")
            if kind in (16, 18) and b58encode(ins.data[1:33]) != ctx.wallet:
                raise TxValidationError("InitializeAccount2/3 com dono diferente da carteira")
            if kind == 17 and acct(0) not in own_sol_accounts:  # SyncNative
                raise TxValidationError("SyncNative em conta que não é da carteira")
        elif prog == ATA:
            kind = ins.data[0] if ins.data else 0  # 0=Create (dados vazios), 1=CreateIdempotent
            if kind not in (0, 1):
                raise TxValidationError("instrução ATA não permitida")
            if acct(0) != ctx.wallet or acct(2) != ctx.wallet:
                raise TxValidationError("criação de ATA para outro dono/financiador")
        elif prog == COMPUTE_BUDGET:
            kind = ins.data[0] if ins.data else -1
            if kind == 2 and len(ins.data) >= 5:
                cu_limit = struct.unpack_from("<I", ins.data, 1)[0]
            elif kind == 3 and len(ins.data) >= 9:
                cu_price = struct.unpack_from("<Q", ins.data, 1)[0]
        elif prog == JUPITER_V6 or prog not in {MEMO}:
            # Roteador/DEX: instrução opaca. Lê os argumentos finais quando o discriminador é conhecido.
            if prog == JUPITER_V6 and bytes(ins.data[:8]) in _JUP_ROUTE_DISCS and len(ins.data) >= 8 + 19:
                in_amount = _u64(ins.data, len(ins.data) - 19)
                slippage = struct.unpack_from("<H", ins.data, len(ins.data) - 3)[0]
                if ctx.max_in_amount is not None and in_amount > ctx.max_in_amount:
                    raise TxValidationError(f"in_amount da rota ({in_amount}) acima do autorizado ({ctx.max_in_amount})")
                if ctx.max_slippage_bps is not None and slippage > ctx.max_slippage_bps:
                    raise TxValidationError(f"slippage da rota ({slippage} bps) acima do limite ({ctx.max_slippage_bps})")
                jup_checked += 1
            else:
                jup_unverified += 1
                if ctx.strict_jupiter_args:
                    raise TxValidationError("instrução de roteador com argumentos não verificáveis (modo estrito)")
    if lamports_out > ctx.max_lamports_out:
        raise TxValidationError(f"saída de SOL ({lamports_out}) acima do teto ({ctx.max_lamports_out})")
    priority_fee = cu_price * (cu_limit if cu_limit is not None else 1_400_000) // 1_000_000
    if priority_fee > ctx.max_priority_fee_lamports:
        raise TxValidationError(f"taxa de prioridade ({priority_fee} lamports) acima do teto")
    if not (programs_seen - {COMPUTE_BUDGET, SYSTEM, TOKEN, TOKEN_2022, ATA, MEMO}):
        raise TxValidationError("transação sem instrução de swap reconhecida")
    return ValidatedTransaction(
        raw=raw, parsed=tx, signer_index=keys.index(ctx.wallet),
        summary={"programas": sorted(programs_seen), "lamports_out": lamports_out, "taxa_prioridade_lamports": priority_fee,
                 "versao": tx.version, "rotas_verificadas": jup_checked, "rotas_nao_verificadas": jup_unverified},
        _token=_SENTINEL,
    )
