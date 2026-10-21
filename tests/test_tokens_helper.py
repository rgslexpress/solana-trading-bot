import json
import os

from _helpers import JUP, USDC


def write_tokens(d: str) -> str:
    path = os.path.join(d, "tokens.json")
    with open(path, "w") as f:
        json.dump({"quote": {"symbol": "USDC", "mint": USDC.mint, "decimals": 6},
                   "allowlist": [{"symbol": "JUP", "mint": JUP.mint, "decimals": 6}]}, f)
    return path
