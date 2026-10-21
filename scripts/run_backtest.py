"""Backtest com dados SINTÉTICOS (demonstração) ou CSV (data.source=csv via .env).

  python scripts/run_backtest.py --robustness
Os dados sintéticos servem para validar a MECÂNICA do motor; não dizem nada sobre o mercado real."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.runner import run_backtest_command  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.factory import build_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--liquidity", type=float, default=5_000_000.0, help="liquidez ASSUMIDA (USD)")
    ap.add_argument("--robustness", action="store_true")
    args = ap.parse_args()
    s = load_settings(args.env_file)
    tokens, _ = s.load_tokens()
    paths = run_backtest_command(s, build_provider(s), tokens, args.liquidity, args.robustness)
    print(json.dumps(paths, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
