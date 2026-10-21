"""Verifica ambiente, dependências e configuração SEM operar, SEM rede e SEM tocar em segredos.

  python scripts/check_environment.py
Saída: [OK] / [AVISO] / [FALHA]. Código de saída 1 se houver FALHA."""

from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PINNED = {"pandas": "2.3.3", "numpy": "2.2.6", "httpx": "0.28.1", "cryptography": "50.0.1"}


def main() -> int:
    fails = 0

    def out(tag: str, msg: str) -> None:
        nonlocal fails
        fails += tag == "FALHA"
        print(f"[{tag}] {msg}")

    v = sys.version_info
    out("OK" if v >= (3, 10) else "FALHA", f"Python {v.major}.{v.minor}.{v.micro} (mínimo 3.10; recomendado 3.12)")
    if v >= (3, 14):
        out("AVISO", "Python 3.14+ não foi considerado nas versões fixadas; prefira 3.12")
    out("OK" if os.name == "nt" else "AVISO", f"sistema: {os.name} ({sys.platform})" + ("" if os.name == "nt" else " — scripts .bat e travas do Windows não se aplicam aqui"))
    if "VIRTUAL_ENV" not in os.environ and sys.prefix == sys.base_prefix:
        out("AVISO", "ambiente virtual NÃO está ativo (.venv\\Scripts\\activate)")
    else:
        out("OK", "ambiente virtual ativo")

    for mod, why, required in (("pandas", "núcleo", True), ("numpy", "núcleo", True), ("httpx", "dados reais/Telegram (rede)", False), ("cryptography", "somente LIVE bloqueado e testes do assinador", False)):
        try:
            ver = getattr(importlib.import_module(mod), "__version__", "?")
            out("OK" if ver == PINNED[mod] else "AVISO", f"{mod} {ver}" + ("" if ver == PINNED[mod] else f" (fixada: {PINNED[mod]})"))
        except ImportError:
            out("FALHA" if required else "AVISO", f"{mod} ausente — {why}" + ("" if required else " (opcional)"))

    from app.config import ConfigError, load_settings

    try:
        s = load_settings(".env" if Path(".env").exists() else None)
    except ConfigError as exc:
        out("FALHA", f"configuração inválida: {exc}")
        return 1
    out("OK" if Path(".env").exists() else "AVISO", f".env {'encontrado' if Path('.env').exists() else 'AUSENTE (usando padrões; copie .env.example para .env)'}")
    out("OK", f"modo configurado: {s.mode.value}; fonte de dados: {s.data.source}")
    out("OK", "LIVE BLOQUEADO: o painel só executa PAPER" + ("" if s.mode.value == "paper" else f" (a configuração pede '{s.mode.value}', que será IGNORADO pelo painel)"))
    if s.wallet_secret or s.wallet_secret_file:
        out("AVISO", "há credencial de carteira configurada: NÃO é necessária para paper/painel — remova-a do .env")

    try:
        tokens, quote = s.load_tokens()
        out("OK", f"allowlist: {', '.join(tokens)} (referência: {quote.symbol})")
        if s.data.source == "geckoterminal":
            missing = [t.symbol for t in tokens.values() if not t.pool]
            out("FALHA" if missing else "OK", "pool de referência no tokens.json" + (f" ausente para: {', '.join(missing)}" if missing else " presente"))
        if s.data.source == "csv" and not s.data.liquidity_usd_override:
            out("AVISO", "BOT_DATA_LIQUIDITY_USD_OVERRIDE=0: liquidez desconhecida => o robô NÃO opera (fail-closed)")
    except (ConfigError, OSError, ValueError) as exc:
        out("FALHA", f"config/tokens.json: {exc} (copie config/tokens.example.json)")

    for label, p in (("banco", Path(s.db_path).parent), ("relatórios", Path(s.reports_dir)), ("logs", Path(s.log_file).parent if s.log_file else Path("logs"))):
        try:
            p.mkdir(parents=True, exist_ok=True)
            out("OK" if os.access(p, os.W_OK) else "FALHA", f"diretório de {label} gravável: {p}")
        except OSError as exc:
            out("FALHA", f"diretório de {label}: {exc}")
    if "onedrive" in str(ROOT).lower():
        out("AVISO", "projeto dentro do OneDrive: mova para um caminho como C:\\dev (sincronização pode corromper o SQLite)")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", s.panel_port))
        out("OK", f"porta {s.panel_port} livre em 127.0.0.1 (painel: http://127.0.0.1:{s.panel_port}/)")
    except OSError:
        out("AVISO", f"porta {s.panel_port} ocupada (painel já em execução ou outro programa; mude BOT_PANEL_PORT)")
    finally:
        probe.close()
    print("\nNenhuma rede foi usada. APIs externas (Jupiter, GeckoTerminal, RPC) NÃO são validadas por este script.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
