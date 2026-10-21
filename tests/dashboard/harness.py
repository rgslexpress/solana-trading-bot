"""Harness: servidor do painel REAL em 127.0.0.1 (porta efêmera) + controlador real + provedor/relógio determinísticos."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time

import numpy as np
import pandas as pd

from app.config import Settings
from app.dashboard.controller import BotController
from app.dashboard.server import PanelServer
from _helpers import ArrayProvider, make_candles, trend_series

SEC = 910  # fechamento do candle de 15m + carência de 5 s


def write_tokens(d: str) -> str:
    from _helpers import JUP, USDC

    path = os.path.join(d, "tokens.json")
    with open(path, "w") as f:
        json.dump({"quote": {"symbol": "USDC", "mint": USDC.mint, "decimals": 6}, "allowlist": [{"symbol": "JUP", "mint": JUP.mint, "decimals": 6}]}, f)
    return path


def make_settings(d: str, **kw) -> Settings:
    s = Settings(db_path=os.path.join(d, "bot.sqlite3"), tokens_file=write_tokens(d), log_file=os.path.join(d, "bot.log"), **kw)
    s.strategy.ema_trend, s.strategy.min_candles, s.data.history_candles = 20, 100, 400
    return s


def make_provider(n_down: int = 40):
    entry = make_candles(trend_series(n_down=n_down), 900)
    return ArrayProvider(entry, make_candles(np.linspace(80, 140, 240), 3600)), entry


def wait_until(cond, timeout: float = 10.0, step: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class Panel:
    def __init__(self, d: str, settings: Settings | None = None, provider=None, entry=None, start_idx: int = 125, poll: float = 0.02, max_errors: int = 5):
        self.d = d
        self.s = settings or make_settings(d)
        if provider is None:
            provider, entry = make_provider()
        self.provider, self.entry = provider, entry
        self.clock = {"now": (entry.index[start_idx] + pd.Timedelta(seconds=SEC)).to_pydatetime()}
        self.ctl = BotController(self.s, provider=provider, clock=lambda: self.clock["now"], poll_seconds=poll, max_errors_before_pause=max_errors, join_timeout_s=10)
        self.server = PanelServer("127.0.0.1", 0, self.ctl)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.port = self.server.port
        self.token = self.server.csrf_token

    # ---------------------------------------------------------------- HTTP
    def req(self, method: str, path: str, body=None, headers: dict | None = None, token: bool = True, origin: bool = True, raw: bytes | None = None):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            h["Content-Type"] = "application/json"
            if token:
                h["X-CSRF-Token"] = self.token
            if origin:
                h["Origin"] = f"http://127.0.0.1:{self.port}"
        h.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body if body is not None else {}).encode() if method == "POST" else None)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=payload, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        try:
            js = json.loads(data.decode("utf-8")) if hdrs.get("content-type", "").startswith("application/json") else None
        except ValueError:
            js = None
        return r.status, js, hdrs, data

    def get(self, path: str, **kw):
        s, js, h, _ = self.req("GET", path, **kw)
        return s, js

    def post(self, path: str, body=None, **kw):
        s, js, h, _ = self.req("POST", path, body=body, **kw)
        return s, js

    def status(self) -> dict:
        return self.get("/api/status")[1]

    def advance(self, idx: int, cycles: int = 2) -> None:
        """Move o relógio simulado para depois do fechamento do candle `idx` e espera ciclos completos."""
        self.clock["now"] = (self.entry.index[idx] + pd.Timedelta(seconds=SEC)).to_pydatetime()
        c0 = self.ctl.cycles
        assert wait_until(lambda: self.ctl.cycles >= c0 + cycles or not self.ctl._alive()), "o laço não avançou"

    def close(self) -> None:
        try:
            self.ctl.stop()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.ctl.close()
