"""Runner de contingência quando o pytest não está instalado (ex.: ambiente sem rede).

Executa funções `test_*` de `tests/**/test_*.py` (sem fixtures). Para a suíte completa use `pytest`.
Uso: python scripts/run_tests_fallback.py [filtro]"""

from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: E402,F401  (aplica a tolerância de limpeza de diretórios temporários, como no pytest)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "dashboard"))


def main() -> int:
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in (ROOT / "tests").rglob("test_*.py") if flt in str(p))
    passed = failed = 0
    t0 = time.time()
    for f in files:
        name = "t_" + "_".join(f.relative_to(ROOT).with_suffix("").parts)
        spec = importlib.util.spec_from_file_location(name, f)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            failed += 1
            print(f"ERRO ao importar {f.relative_to(ROOT)}\n{traceback.format_exc()}")
            continue
        for fn_name in sorted(n for n in dir(mod) if n.startswith("test_")):
            fn = getattr(mod, fn_name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"PASSOU  {f.relative_to(ROOT)}::{fn_name}")
            except Exception:
                failed += 1
                print(f"FALHOU  {f.relative_to(ROOT)}::{fn_name}\n{traceback.format_exc()}")
    print(f"\n{passed} passaram, {failed} falharam em {time.time() - t0:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
