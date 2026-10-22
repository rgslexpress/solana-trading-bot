"""Fuso horário dos EUA (America/New_York) SEM depender de tzdata/zoneinfo (que não vem no Windows por padrão).

Regra vigente desde 2007 (Energy Policy Act de 2005): o horário de verão (EDT, UTC-4) começa no 2º domingo de março às 02:00 locais
e termina no 1º domingo de novembro às 02:00 locais (EST, UTC-5). Nunca use offsets fixos: datas de divulgação perto das trocas de horário
mudariam 1 h no UTC. Cobertura: anos 2007 a 2099. Fora disso, ValueError (melhor falhar do que calcular um horário errado).

Horário local inexistente (02:00–02:59 no dia em que o relógio avança) => ValueError.
Horário local ambíguo (01:00–01:59 no dia em que o relógio recua) => resolvido por `fold` (0 = primeira ocorrência, EDT; 1 = segunda, EST)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

ET_NAMES = frozenset({"America/New_York", "US/Eastern", "EST5EDT", "America/Detroit", "America/Toronto"})
MIN_YEAR, MAX_YEAR = 2007, 2099
EDT, EST = timedelta(hours=-4), timedelta(hours=-5)


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
    """(início, fim) do horário de verão em UTC. Início: 2º dom. de março 02:00 EST = 07:00 UTC. Fim: 1º dom. de novembro 02:00 EDT = 06:00 UTC."""
    if not (MIN_YEAR <= year <= MAX_YEAR):
        raise ValueError(f"ano fora do intervalo suportado ({MIN_YEAR}-{MAX_YEAR}): {year}")
    start = _nth_sunday(year, 3, 2)
    end = _nth_sunday(year, 11, 1)
    return (datetime(start.year, start.month, start.day, 7, 0, tzinfo=timezone.utc), datetime(end.year, end.month, end.day, 6, 0, tzinfo=timezone.utc))


def et_offset_at_utc(utc: datetime) -> timedelta:
    u = utc.astimezone(timezone.utc)
    start, end = dst_bounds_utc(u.year)
    return EDT if start <= u < end else EST


def utc_to_et(utc: datetime) -> datetime:
    """Instante UTC -> horário local de Nova York (datetime ingênuo)."""
    u = utc.astimezone(timezone.utc)
    return (u + et_offset_at_utc(u)).replace(tzinfo=None)


def et_to_utc(local: datetime, fold: int = 0) -> datetime:
    """Horário local de Nova York (ingênuo) -> UTC. Lança ValueError se o horário não existir (salto da primavera)."""
    if local.tzinfo is not None:
        raise ValueError("informe um datetime ingênuo (horário local de Nova York)")
    start, end = dst_bounds_utc(local.year)
    # Candidatos: interpretar como EDT e como EST; vale o que, convertido de volta, reproduz o horário local original.
    cands = []
    for off in (EDT, EST):
        u = (local - off).replace(tzinfo=timezone.utc)
        if et_offset_at_utc(u) == off:
            cands.append(u)
    if not cands:
        raise ValueError(f"horário local inexistente em America/New_York (salto do horário de verão): {local.isoformat()}")
    cands.sort()
    return cands[0] if (fold == 0 or len(cands) == 1) else cands[1]


def to_utc(local: datetime, tz: str, fold: int = 0) -> datetime:
    """Converte um horário local ingênuo no fuso `tz` (UTC ou fuso dos EUA/Leste) para UTC."""
    if tz.upper() in ("UTC", "Z", "GMT"):
        return local.replace(tzinfo=timezone.utc)
    if tz in ET_NAMES:
        return et_to_utc(local, fold)
    raise ValueError(f"fuso não suportado: {tz!r} (suportados: UTC e America/New_York)")


def is_dst_utc(utc: datetime) -> bool:
    return et_offset_at_utc(utc) == EDT
