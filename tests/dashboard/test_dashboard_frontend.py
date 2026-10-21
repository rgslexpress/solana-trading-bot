"""Fumaça do frontend: executa app.js em Node com um DOM MÍNIMO manual contra o servidor real.
NÃO é um teste em navegador real (sem Chrome/Playwright neste ambiente); verifica que o JS roda sem exceções,
exibe N/D, respeita as ações permitidas e que o clique chama o backend de verdade. Ignorado se `node` não existir."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from harness import Panel, wait_until

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent / "app" / "dashboard" / "static"


def _run(p, click=None):
    html = p.req("GET", "/")[3].decode()
    cmd = ["node", str(HERE / "dom_smoke.js"), f"http://127.0.0.1:{p.port}", str(APP / "app.js")] + ([click] if click else [])
    r = subprocess.run(cmd, input=html, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_frontend_renders_nd_and_paper_labels_and_clicks_hit_the_real_backend():
    if not shutil.which("node"):
        print("IGNORADO: node não encontrado")
        return
    with tempfile.TemporaryDirectory() as d:
        p = Panel(d)
        try:
            out = _run(p)
            assert "PARADO" in out["estado_antes"] and "N/D" in out["cards"] and "simulados" not in out.get("erro", "")
            assert "exclusivamente paper trading" in out["live"] and out["btn_parar_desabilitado"] is True and out["btn_iniciar_desabilitado"] is False
            out = _run(p, "iniciar")  # o clique faz POST real com token CSRF e origem corretos
            assert wait_until(lambda: p.ctl._alive()), out
            assert "Paper trading iniciado" in out["msg_apos"] and "RODANDO" in out["estado_apos"] and out["btn_parar_apos_desabilitado"] is False
            out = _run(p, "emergencia")
            assert p.status()["kill_switch"]["ativo"] and "Kill switch acionado" in out["msg_apos"]
        finally:
            p.close()


def test_frontend_source_has_no_dynamic_html_eval_storage_or_credentials():
    js = (APP / "app.js").read_text(encoding="utf-8")
    for bad in (".innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write", "localStorage", "sessionStorage", "document.cookie",
                "private", "secret", "seed", "wallet"):
        assert bad not in js, bad
    html = (APP / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "onclick" not in html and "style=" not in html
    assert "http://" not in html and "https://" not in html  # nenhum recurso externo (CDN, fontes, analytics)
