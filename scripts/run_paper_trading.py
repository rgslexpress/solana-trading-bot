"""Paper trading em REPLAY sobre dados sintéticos (demonstração offline) ou ao vivo com dados reais.

  python scripts/run_paper_trading.py --replay-days 20      # relógio simulado, dados SINTÉTICOS
  python -m app.main --mode paper                             # relógio real + provedor configurado

O replay NÃO usa mercado real e NÃO assina nada: usa PaperExecutor."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.domain import Mode, TokenInfo  # noqa: E402
from app.factory import build_runtime  # noqa: E402
from app.services.reporting import performance_summary, write_operational_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-days", type=int, default=20)
    ap.add_argument("--db", default="")
    ap.add_argument("--reports", default="reports")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()
    s = Settings(mode=Mode.PAPER, db_path=args.db or str(Path(tmp) / "paper.sqlite3"), reports_dir=args.reports, tokens_file=str(Path(tmp) / "tokens.json"))
    s.strategy.ema_trend = 50
    s.data.source = "synthetic"
    tokens = {"quote": {"symbol": "USDC", "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6},
              "allowlist": [{"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "decimals": 6}]}
    Path(s.tokens_file).write_text(json.dumps(tokens), encoding="utf-8")
    clock = {"now": None}
    rt = build_runtime(s, clock=lambda: clock["now"])
    jup = rt.service.tokens["JUP"]
    full = rt.provider.full_history(jup, "15m")  # type: ignore[attr-defined]
    start = full.index[0] + timedelta(days=25)
    clock["now"] = start.to_pydatetime()
    rep = rt.service.startup()
    print("startup:", "OK" if rep.ok else "FALHOU", [(n, ok) for n, ok, _ in rep.steps])
    steps = args.replay_days * 96
    for i in range(steps):
        clock["now"] = (start + timedelta(minutes=15 * i)).to_pydatetime()
        r = rt.service.run_cycle()
        for a in r.actions:
            print(clock["now"].isoformat(), a)
    print(json.dumps(performance_summary(rt.db), indent=2, ensure_ascii=False, default=str))
    print("relatório:", write_operational_report(rt.db, s.reports_dir, "paper_replay"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
