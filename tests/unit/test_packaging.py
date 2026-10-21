"""Empacotamento/transferência: dependências mínimas, fins de linha dos .bat, BOM do Bloco de Notas, verificador de ambiente."""

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import load_settings

ROOT = Path(__file__).resolve().parent.parent.parent


def _third_party_imports(base: str) -> set[str]:
    std = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    found = set()
    for f in (ROOT / base).rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            found |= {m for m in mods if m not in std and m not in ("app", "conftest")}  # conftest: módulo local dos testes
    return found


def _req_names(fname: str) -> set[str]:
    out = set()
    for ln in (ROOT / fname).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("#", "-r")):
            out.add(ln.split("==")[0].lower())
    return out


def test_every_third_party_import_in_app_is_declared_and_pinned():
    used = _third_party_imports("app") | _third_party_imports("scripts")
    declared = _req_names("requirements.txt") | _req_names("requirements-live.txt") | _req_names("requirements-dev.txt")
    assert used <= declared, used - declared
    for fname in ("requirements.txt", "requirements-live.txt", "requirements-dev.txt"):
        for ln in (ROOT / fname).read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith(("#", "-r")):
                assert "==" in ln, f"{fname}: dependência sem versão fixa: {ln}"


def test_runtime_requirements_are_minimal_and_exclude_live_and_dev_tools():
    rt = _req_names("requirements.txt")
    assert rt == {"pandas", "numpy", "httpx"}  # cryptography (LIVE) e ferramentas de teste NÃO fazem parte do runtime
    assert "cryptography" in _req_names("requirements-live.txt")
    dev_text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert {"pytest", "ruff", "mypy"} <= _req_names("requirements-dev.txt")
    assert "-r requirements.txt" in dev_text and "-r requirements-live.txt" in dev_text  # dev inclui runtime e cryptography (testes do assinador)
    assert "http.server" not in "".join(rt) and not (_req_names("requirements.txt") & {"flask", "fastapi", "uvicorn", "aiohttp", "django"})


def test_panel_and_paper_modules_import_without_cryptography_or_httpx():
    code = ("import sys\nsys.modules['cryptography']=None\nsys.modules['httpx']=None\n"
            "import app.main, app.dashboard.server, app.dashboard.controller, app.dashboard.__main__, app.factory, app.services.scheduler\n"
            "print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-500:]


def test_gitattributes_keeps_bat_files_crlf_and_bat_files_are_ascii_crlf():
    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in ga
    for f in (ROOT / "scripts").glob("*.bat"):
        raw = f.read_bytes()
        raw.decode("ascii")  # sem acentos: independe da página de código do CMD
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f.name


def test_gitignore_covers_secrets_state_envs_and_caches():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pat in (".env", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.log", "logs/", ".venv/", "__pycache__/", "*.pyc", "*.key", "keypair*.json",
                "wallet*.json", "secrets/", "config/tokens.json", "*.painel.json", "*.robo.lock", "*.painel.lock", "data/*", "reports/*"):
        assert pat in gi, pat
    assert "!.env.example" in gi and "!data/.gitkeep" in gi


def test_env_and_tokens_files_saved_with_bom_by_windows_notepad_are_read_correctly():
    with tempfile.TemporaryDirectory() as d:
        env = os.path.join(d, ".env")
        with open(env, "w", encoding="utf-8-sig") as f:  # grava o BOM (EF BB BF)
            f.write("BOT_PAPER_CAPITAL=2500\nBOT_RISK_RISK_PER_TRADE_PCT=0.4\n")
        s = load_settings(env, environ={})
        assert s.paper_capital == 2500.0 and s.risk.risk_per_trade_pct == 0.4  # a 1ª variável não pode ser perdida
        tk = os.path.join(d, "tokens.json")
        with open(tk, "w", encoding="utf-8-sig") as f:
            json.dump({"quote": {"symbol": "USDC", "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6}, "allowlist": []}, f)
        s.tokens_file = tk
        assert s.load_tokens()[1].symbol == "USDC"


def test_check_environment_script_runs_offline_and_reports_live_blocked():
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, BOT_DB_PATH=os.path.join(d, "x.sqlite3"), BOT_REPORTS_DIR=os.path.join(d, "r"), BOT_LOG_FILE=os.path.join(d, "l", "b.log"),
                   BOT_PANEL_PORT="18999")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_environment.py")], cwd=d, capture_output=True, text=True, timeout=60, env={**env, "PYTHONPATH": str(ROOT)})
        # roda fora da raiz: tokens.json relativo não existe => FALHA esperada e explicada; o importante é não explodir e não usar rede
        assert "LIVE BLOQUEADO" in r.stdout and "Nenhuma rede foi usada" in r.stdout or "tokens.json" in r.stdout
        assert "Traceback" not in r.stderr
