"""Laço principal com encerramento organizado (SIGINT/SIGTERM)."""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Callable

from app.services.trading_service import TradingService

log = logging.getLogger("agendador")


def run_forever(service: TradingService, poll_seconds: float, stop: threading.Event | None = None, max_cycles: int | None = None, sleep: Callable[[float], None] | None = None) -> int:
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    n = 0
    while not stop.is_set():
        try:
            rep = service.run_cycle()
            for a in rep.actions:
                log.info(a)
        except Exception:  # noqa: BLE001 - um ciclo com erro não derruba o processo; o erro é registrado
            log.exception("erro no ciclo de trading")
            service.alerts.critical("erro inesperado no ciclo de trading (ver logs)")
        n += 1
        if max_cycles is not None and n >= max_cycles:
            break
        (sleep or stop.wait)(poll_seconds)  # type: ignore[operator]
    log.info("encerrado após %d ciclo(s)", n)
    return n
