"""CLI: python -m app.main --mode paper|backtest|live [opções]

Padrão seguro: PAPER. LIVE exige BOT_MODE=live + BOT_LIVE_CONFIRM + --confirm-live e todas as verificações."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime, timezone

from app.config import ConfigError, Settings, load_settings, validate_settings
from app.domain import Mode
from app.logging_config import setup_logging
from app.risk.kill_switch import RESUME_PHRASE

log = logging.getLogger("main")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.main", description="Robô de trading Solana/Jupiter (padrão: PAPER).")
    p.add_argument("--mode", choices=[m.value for m in Mode], help="sobrepõe BOT_MODE")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--health-check", action="store_true", help="verifica config, banco, dados, RPC e kill switch; não opera")
    p.add_argument("--show-config", action="store_true", help="exibe a configuração com segredos mascarados")
    p.add_argument("--show-positions", action="store_true")
    p.add_argument("--reconcile", action="store_true", help="reconcilia intenções pendentes/desconhecidas e sai")
    p.add_argument("--report", action="store_true", help="gera relatório operacional (paper/live) a partir do banco")
    p.add_argument("--kill-switch", metavar="MOTIVO", help="ativa o kill switch manualmente (bloqueia novas entradas)")
    p.add_argument("--resume-kill-switch", metavar="FRASE", help=f"retoma após kill switch; use '{RESUME_PHRASE}' e --note")
    p.add_argument("--note", default="", help="justificativa para a retomada")
    p.add_argument("--confirm-live", default="", metavar="FRASE", help="segunda confirmação para o modo LIVE")
    p.add_argument("--once", action="store_true", help="executa um único ciclo")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--liquidity", type=float, default=5_000_000.0, help="liquidez (USD) ASSUMIDA no backtest")
    p.add_argument("--robustness", action="store_true", help="backtest: inclui sensibilidade e walk-forward")
    return p


def _load(args: argparse.Namespace) -> Settings:
    s = load_settings(args.env_file)
    if args.mode:
        s.mode = Mode(args.mode)
        errs = validate_settings(s)
        if errs:
            raise ConfigError("; ".join(errs))
    return s


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = _load(args)
    except ConfigError as exc:
        print(f"Configuração inválida: {exc}", file=sys.stderr)
        return 2
    setup_logging(s.log_level, s.log_json, s.log_file)

    if args.show_config:
        print(json.dumps(s.redacted_dict(), indent=2, ensure_ascii=False))
        return 0

    from app.factory import LiveBlocked, build_provider, build_runtime

    if args.mode == "backtest" or (s.mode == Mode.BACKTEST):
        from app.backtest.runner import run_backtest_command

        tokens, _quote = s.load_tokens()
        paths = run_backtest_command(s, build_provider(s), tokens, args.liquidity, args.robustness)
        print(json.dumps(paths, indent=2))
        return 0

    if args.health_check:
        return _health_check(s)

    try:
        rt = build_runtime(s, cli_confirm=args.confirm_live)
    except LiveBlocked as exc:
        print("MODO LIVE BLOQUEADO:\n - " + "\n - ".join(exc.reasons), file=sys.stderr)
        return 2

    if args.kill_switch:
        rt.kill_switch.trip(args.kill_switch, source="manual")
        print("Kill switch ativado. Novas entradas bloqueadas; posições preservadas.")
        return 0
    if args.resume_kill_switch is not None:
        ok = rt.kill_switch.resume(args.resume_kill_switch, args.note)
        print("Kill switch desativado." if ok else "Retomada recusada: frase/justificativa inválidas.")
        return 0 if ok else 2
    if args.show_positions:
        for p in rt.db.load_positions("OPEN"):
            print(json.dumps(p.to_dict(), ensure_ascii=False))
        return 0
    if args.report:
        from app.services.reporting import performance_summary, write_operational_report

        print(json.dumps(performance_summary(rt.db), indent=2, ensure_ascii=False, default=str))
        print(write_operational_report(rt.db, s.reports_dir, s.mode.value))
        return 0
    if args.reconcile:
        rt.service.positions = {p.mint: p for p in rt.db.load_positions("OPEN")}
        ok = rt.service.reconcile_pending()
        print("Reconciliação concluída." if ok else "Há intenções pendentes sem resolução; novas entradas permanecem bloqueadas.")
        return 0 if ok else 1

    from app.instance import AlreadyRunning, InstanceLock, bot_lock_path
    from app.services.scheduler import run_forever

    try:  # nunca dois laços de trading (CLI ou painel) sobre a mesma base
        loop_lock = InstanceLock(bot_lock_path(s.db_path), "instância do robô (CLI ou painel)").acquire()
    except AlreadyRunning as exc:
        print(f"Recusado: {exc}. Pare a outra instância antes.", file=sys.stderr)
        return 3
    rep = rt.service.startup()
    for name, ok, detail in rep.steps:
        print(f"[{'OK' if ok else 'FALHA'}] {name} {detail}")
    if not rep.ok:
        print("Startup reprovado: o robô permanece em MODO SEGURO (sem novas entradas).", file=sys.stderr)
    log.info("modo ativo: %s", s.mode.value.upper())
    try:
        run_forever(rt.service, s.poll_seconds, max_cycles=1 if args.once else args.max_cycles)
    finally:
        loop_lock.release()
    rt.db.close()
    return 0 if rep.ok else 1


def _health_check(s: Settings) -> int:
    from app.factory import _NullExec, build_provider
    from app.monitoring.alerts import AlertManager
    from app.risk.kill_switch import KillSwitch
    from app.services.trading_service import TradingService
    from app.storage.database import Database

    tokens, quote = s.load_tokens()
    db = Database(s.db_path)
    ks = KillSwitch(db.kill_switch_load, db.kill_switch_save)
    svc = TradingService(s, db, build_provider(s), _NullExec(), ks, AlertManager(db), tokens, quote)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("configuração", not validate_settings(s), ""))
    checks.append(("banco de dados", db.integrity_ok(), s.db_path))
    now = datetime.now(timezone.utc)
    for tok in tokens.values():
        feats, why = svc._features(tok, now)  # noqa: SLF001
        checks.append((f"dados {tok.symbol}", feats is not None, why))
    checks.append(("kill switch inativo", not ks.active, ks.reason))
    if s.mode == Mode.LIVE:
        from app.execution.rpc import SolanaRpc
        from app.http import HttpxTransport

        checks.append(("RPC", SolanaRpc(s.rpc_url, HttpxTransport(), s.execution.http_timeout_s).health(), s.rpc_url))
        checks.append(("API key Jupiter definida", bool(s.jupiter_api_key), ""))
    ok = all(c[1] for c in checks)
    for n, v, d in checks:
        print(f"[{'OK' if v else 'FALHA'}] {n} {d}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
