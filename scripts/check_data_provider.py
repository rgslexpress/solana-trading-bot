"""Valida o provedor de candles REAL (somente leitura): formato, alinhamento, lacunas, defasagem, volume e liquidez.

  python scripts/check_data_provider.py            # usa BOT_DATA_SOURCE e a allowlist
Não assina nem envia nada. Requer internet para `geckoterminal`."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import TIMEFRAME_SECONDS, load_settings  # noqa: E402
from app.data.validation import closed_only, validate_candles  # noqa: E402
from app.factory import build_provider  # noqa: E402


def main() -> int:
    s = load_settings(".env" if Path(".env").exists() else None)
    prov = build_provider(s)
    tokens, _ = s.load_tokens()
    now = datetime.now(timezone.utc)
    bad = 0
    print(f"Fonte: {prov.name} (agora = {now.isoformat()})")
    for tok in tokens.values():
        for tf in (s.strategy.entry_timeframe, s.strategy.htf_timeframe):
            try:
                df = prov.get_candles(tok, tf, s.data.history_candles, now)
            except Exception as exc:  # noqa: BLE001
                print(f"[FALHA] {tok.symbol}/{tf}: {type(exc).__name__}: {exc}")
                bad += 1
                continue
            raw_n = len(df)
            df = closed_only(df, TIMEFRAME_SECONDS[tf], now, s.data.close_grace_s)
            v = validate_candles(df, TIMEFRAME_SECONDS[tf], now, s.data.max_data_staleness_candles, 50, s.data.max_gap_candles, 50)
            zero_vol = int((df["volume"] == 0).sum()) if len(df) else 0
            print(f"[{'OK' if v.ok else 'INVÁLIDO'}] {tok.symbol}/{tf}: {raw_n} candles, {len(df)} fechados, último={df.index[-1] if len(df) else None}, "
                  f"volume zero em {zero_vol}, problemas={v.issues}")
            bad += 0 if v.ok else 1
        print(f"        liquidez {tok.symbol}: {prov.liquidity_usd(tok, now)}")
        print(f"        último preço 1m {tok.symbol}: {prov.latest_price(tok, now)}")
    print("Compare alguns candles manualmente com o gráfico do GeckoTerminal (timestamp = abertura? volume em USD da pool?).")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
