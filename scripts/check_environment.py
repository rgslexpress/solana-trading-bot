"""Verifica versões/dependências e a configuração SEM operar e SEM tocar em segredos.

  python scripts/check_environment.py"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REQUIRED = {"pandas": "2.3.3", "numpy": "2.2.6", "cryptography": "50.0.1", "httpx": "0.28.1"}


def main() -> int:
    ok = True
    print(f"Python {sys.version.split()[0]} (alvo: 3.12; mínimo 3.10)")
    if sys.version_info < (3, 10):
        ok = False
    for mod, pinned in REQUIRED.items():
        try:
            m = importlib.import_module(mod)
            v = getattr(m, "__version__", "?")
            flag = "OK" if v == pinned else f"DIFERENTE do fixado ({pinned})"
            print(f"[{flag}] {mod} {v}")
        except ImportError:
            print(f"[FALTA] {mod} (pip install -r requirements.txt)")
            ok = mod == "httpx" and ok  # httpx só é necessário para rede real
    from app.config import ConfigError, load_settings

    try:
        s = load_settings(".env" if Path(".env").exists() else None)
        print(f"[OK] configuração válida; modo={s.mode.value}; fonte de dados={s.data.source}")
        print("[AVISO] LIVE está " + ("HABILITADO por configuração" if s.mode.value == "live" else "desabilitado (padrão)"))
    except ConfigError as exc:
        print(f"[FALHA] configuração: {exc}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
