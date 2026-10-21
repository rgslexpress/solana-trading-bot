"""Baixa candles históricos REAIS do GeckoTerminal (somente leitura) para usar em backtest/paper com BOT_DATA_SOURCE=csv.

  python scripts\\baixar_candles.py --dias 60
  python scripts\\baixar_candles.py --dias 90 --timeframes 15m 1h --preencher-vazios

Requer internet, `httpx` e `pool` preenchido em config\\tokens.json. NÃO assina nem envia nada; não usa carteira/chaves.
Não sobrescreve arquivos existentes sem --sobrescrever. O contrato da API NÃO foi validado pelo autor (sem rede no desenvolvimento):
confira o resultado com o gráfico do GeckoTerminal."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.data.downloader import DownloadError, download_history, pool_liquidity  # noqa: E402
from app.http import HttpxTransport  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=float, default=60, help="quantos dias de histórico (padrão 60)")
    ap.add_argument("--timeframes", nargs="+", default=None, help="padrão: timeframe de entrada e superior do .env (ex.: 15m 1h)")
    ap.add_argument("--simbolos", nargs="+", default=None, help="padrão: toda a allowlist")
    ap.add_argument("--saida", default=None, help="pasta de saída (padrão: BOT_DATA_CSV_DIR)")
    ap.add_argument("--preencher-vazios", action="store_true", help="pede ao provedor intervalos sem negociação (OHLC = fechamento anterior, volume 0)")
    ap.add_argument("--sobrescrever", action="store_true")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()
    s = load_settings(a.env_file if Path(a.env_file).exists() else None)
    tokens, _ = s.load_tokens()
    tfs = a.timeframes or sorted({s.strategy.entry_timeframe, s.strategy.htf_timeframe})
    syms = a.simbolos or list(tokens)
    out_dir = a.saida or s.data.csv_dir
    transport = HttpxTransport()
    now = datetime.now(timezone.utc)
    bad = 0
    print(f"Baixando {a.dias:g} dia(s) de {tfs} para {syms} em '{out_dir}' (hora UTC: {now.isoformat(timespec='seconds')})")
    for sym in syms:
        tok = tokens[sym]
        liq = pool_liquidity(transport, s.data.geckoterminal_url, tok.pool) if tok.pool else None
        print(f"\n{sym}: liquidez atual da pool (USD): {f'{liq:,.0f}' if liq else 'N/D'}  <- use como premissa em BOT_DATA_LIQUIDITY_USD_OVERRIDE")
        for tf in tfs:
            try:
                r = download_history(transport, tok, tf, a.dias, out_dir, now=now, base_url=s.data.geckoterminal_url,
                                     include_empty=a.preencher_vazios, overwrite=a.sobrescrever, pause_s=2.5, timeout=s.execution.http_timeout_s)
            except DownloadError as exc:
                print(f"  [FALHA] {sym}/{tf}: {exc}")
                bad += 1
                continue
            status = "OK" if not r.issues else "AVISO"
            print(f"  [{status}] {sym}/{tf}: {r.candles} candles, {r.first} -> {r.last}, {r.pages} página(s) -> {r.path}")
            for i in r.issues:
                print(f"        problema de dados: {i}")
            if r.issues and not a.preencher_vazios:
                print("        dica: lacunas costumam ser intervalos sem negociação; tente --preencher-vazios (dados preenchidos pelo provedor).")
    print("\nPróximo passo: BOT_DATA_SOURCE=csv e backtest: python scripts\\run_backtest.py --liquidity <USD>")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
