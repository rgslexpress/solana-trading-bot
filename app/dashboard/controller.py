"""Controlador do paper trading para o painel local.

Garantias:
* SOMENTE PAPER: o modo é forçado para PAPER e o executor é verificado (`PaperExecutor`) antes de rodar. Este módulo
  não importa carteira, assinador, Jupiter, RPC nem HTTP; nenhum comando aceita modo, chaves ou configuração.
* Iniciar o painel NÃO inicia o robô. Parar o robô/painel nunca envia ordens nem liquida posições; o estado fica
  persistido e é reconciliado no próximo início. Enquanto parado, stops simulados NÃO são monitorados (avisado).
* Uma única instância do laço de trading por base (trava do SO), compartilhada com o CLI.
* O botão de emergência usa o kill switch existente; a retomada exige a mesma frase e justificativa do CLI.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.dashboard import readmodel
from app.data.providers import CandleProvider
from app.domain import Mode
from app.execution.paper import PaperExecutor
from app.factory import build_runtime
from app.instance import AlreadyRunning, InstanceLock, bot_lock_path
from app.logging_config import redact_text
from app.risk.kill_switch import RESUME_PHRASE, KillSwitch
from app.storage.database import Database

log = logging.getLogger("painel")
LIVE_MOTIVO = "O painel executa exclusivamente paper trading. Execução real não existe nesta interface e continua bloqueada (confirmação dupla fora do painel, nunca exposta aqui)."


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class BotController:
    def __init__(
        self,
        settings: Settings,
        provider: CandleProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float | None = None,
        max_errors_before_pause: int = 5,
        join_timeout_s: float = 20.0,
    ):
        s = copy.deepcopy(settings)
        self.live_requested_in_config = s.mode != Mode.PAPER
        s.mode = Mode.PAPER  # incondicional: o painel nunca opera outro modo
        self.s, self.provider = s, provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll = poll_seconds if poll_seconds is not None else s.poll_seconds
        self.max_errors, self.join_timeout = max_errors_before_pause, join_timeout_s
        self.db = Database(s.db_path)  # conexão de controle (eventos/meta/kill switch); protegida por _db_lock
        self._db_lock = threading.Lock()
        self.svc_lock = threading.RLock()  # serializa o uso do runtime (serviço + sua conexão) entre o laço e os comandos
        self.cmd_lock = threading.Lock()
        self.runtime = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop_lock: InstanceLock | None = None
        self.cycles = 0
        self.consecutive_errors = 0
        self.last_error: str | None = None
        self.last_cycle_at: str | None = None
        self.started_at: str | None = None
        self._shutdown_done = False

    # ------------------------------------------------------------------ util
    def _event(self, name: str, detail: str = "", source: str = "painel") -> None:
        with self._db_lock:
            self.db.save_bot_event(name, detail, source)

    def _ctl_kill_switch(self) -> KillSwitch:
        return KillSwitch(self.db.kill_switch_load, self.db.kill_switch_save)

    def _alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _entries_paused(self) -> bool:
        rt = self.runtime
        if rt is not None and self._alive():
            return bool(rt.service.entries_paused)
        with self._db_lock:
            return bool(self.db.get_meta("entries_paused", False))

    @property
    def state(self) -> str:
        if self._alive():
            if self._stop.is_set():
                return "PARANDO"
            return "ENTRADAS_PAUSADAS" if self._entries_paused() else "RODANDO"
        return "PARADO"

    # ------------------------------------------------------------------ comandos
    def start(self) -> dict[str, Any]:
        with self.cmd_lock:
            if self._alive():
                raise CommandError(409, "o paper trading já está em execução")
            try:
                lock = InstanceLock(bot_lock_path(self.s.db_path), "instância do robô (CLI ou painel)").acquire()
            except AlreadyRunning as exc:
                raise CommandError(409, f"{exc}. Pare a outra instância antes de iniciar.") from exc
            try:
                rt = build_runtime(self.s, provider=self.provider, clock=self.clock)
                if not isinstance(rt.executor, PaperExecutor) or rt.executor.mode != Mode.PAPER:
                    raise CommandError(500, "executor não é PAPER — início recusado")
                with self.svc_lock:
                    rep = rt.service.startup()
            except CommandError:
                lock.release()
                raise
            except Exception as exc:  # noqa: BLE001
                lock.release()
                msg = redact_text(f"{type(exc).__name__}: {exc}")
                self._event("erro_inicio", msg)
                raise CommandError(500, f"falha ao iniciar: {msg}") from exc
            self._loop_lock, self.runtime = lock, rt
            self._stop.clear()
            self.cycles, self.consecutive_errors, self.last_error = 0, 0, None
            self.started_at = self.clock().isoformat()
            self._thread = threading.Thread(target=self._run, name="paper-loop", daemon=True)
            self._thread.start()
            falhas = [f"{n}: {d}" for n, ok, d in rep.steps if not ok]
            self._event("paper_iniciado", f"startup_ok={rep.ok}; falhas={'; '.join(falhas)[:500]}")
            return {"ok": True, "modo_seguro": not rep.ok, "falhas_startup": falhas,
                    "mensagem": "Paper trading iniciado." + (" Startup reprovado: MODO SEGURO (sem novas entradas)." if not rep.ok else "")}

    def _run(self) -> None:
        rt = self.runtime
        assert rt is not None
        try:
            while not self._stop.is_set():
                wait = self.poll
                try:
                    with self.svc_lock:
                        rt.service.run_cycle()
                    self.cycles += 1
                    self.consecutive_errors = 0
                    self.last_cycle_at = self.clock().isoformat()
                except Exception as exc:  # noqa: BLE001 - o laço sobrevive; erro registrado, backoff, pausa de entradas
                    self.consecutive_errors += 1
                    self.last_error = redact_text(f"{type(exc).__name__}: {exc}")[:300]
                    log.exception("erro no ciclo do paper trading")
                    self._event("erro_ciclo", f"{self.last_error} (consecutivos: {self.consecutive_errors})", "robo")
                    if self.consecutive_errors >= self.max_errors and not rt.service.entries_paused:
                        rt.service.entries_paused = True
                        self._event("entradas_pausadas_automaticamente", f"{self.consecutive_errors} erros consecutivos", "robo")
                    wait = min(60.0, self.poll * (2 ** min(self.consecutive_errors, 6)))
                self._stop.wait(wait)
        finally:
            with self.svc_lock:
                n_open = len(rt.service.positions)
                try:
                    with self._db_lock:
                        self.db.set_meta("entries_paused", bool(rt.service.entries_paused))
                    rt.db.close()
                except Exception:  # noqa: BLE001
                    log.exception("erro ao encerrar o runtime")
                self.runtime = None
            aviso = f" {n_open} posição(ões) simulada(s) aberta(s) SEM monitoramento de stop enquanto parado (estado preservado)." if n_open else ""
            self._event("paper_parado", f"ciclos={self.cycles}.{aviso}", "robo")
            if self._loop_lock:
                self._loop_lock.release()
                self._loop_lock = None

    def stop(self) -> dict[str, Any]:
        with self.cmd_lock:
            if not self._alive():
                return {"ok": True, "parado": True, "mensagem": "O paper trading já estava parado."}
            self._event("parada_solicitada", "")
            self._stop.set()
            t = self._thread
        t.join(self.join_timeout)  # fora do cmd_lock: não bloqueia outros comandos (ex.: emergência)
        stopped = not t.is_alive()
        n_open = len(readmodel.positions_for(self.s.db_path, "OPEN"))
        aviso = (f"Há {n_open} posição(ões) simulada(s) aberta(s): enquanto parado, stops e alvos NÃO são monitorados. "
                 "O estado foi preservado e será reconciliado ao iniciar.") if n_open else None
        return {"ok": True, "parado": stopped, "aviso": aviso,
                "mensagem": "Paper trading parado." if stopped else "Parada solicitada; aguardando o fim do ciclo em andamento."}

    def pause_entries(self) -> dict[str, Any]:
        with self.cmd_lock:
            rt = self.runtime
            if rt is not None and self._alive():
                rt.service.entries_paused = True
            with self._db_lock:
                self.db.set_meta("entries_paused", True)
            self._event("entradas_pausadas", "pelo operador")
            return {"ok": True, "mensagem": "Novas entradas pausadas. Saídas e monitoramento continuam."}

    def resume_entries(self) -> dict[str, Any]:
        with self.cmd_lock:
            with readmodel.open_ro(self.s.db_path) as c:
                ks = readmodel.kill_switch_state(c)
            if ks["ativo"]:
                raise CommandError(409, "o kill switch está ativo: as entradas só voltam após a retomada explícita do kill switch")
            rt = self.runtime
            if rt is not None and self._alive():
                rt.service.entries_paused = False
            with self._db_lock:
                self.db.set_meta("entries_paused", False)
            self._event("entradas_retomadas", "pelo operador")
            return {"ok": True, "mensagem": "Novas entradas liberadas (continuam sujeitas a risco, dados, saúde e kill switch)."}

    def emergency(self) -> dict[str, Any]:
        """Parada segura: bloqueia entradas AGORA e aciona o kill switch existente. Não liquida nem envia ordens."""
        rt = self.runtime
        if rt is not None and self._alive():
            rt.service.entries_paused = True  # efeito imediato, sem esperar o ciclo
        with self._db_lock:
            self.db.set_meta("entries_paused", True)
            self._ctl_kill_switch().trip("EMERGÊNCIA acionada pelo painel", source="painel")  # persistido (fonte de verdade)
        self._event("emergencia", "kill switch acionado pelo painel", "painel")
        if rt is not None and self.svc_lock.acquire(timeout=2.0):  # propaga já ao serviço; senão o próximo ciclo recarrega
            try:
                rt.service.ks.refresh()
            finally:
                self.svc_lock.release()
        return {"ok": True, "mensagem": "Kill switch acionado: novas entradas bloqueadas. Posições simuladas preservadas; nada foi liquidado."}

    def resume_kill_switch(self, phrase: str, note: str) -> dict[str, Any]:
        with self.cmd_lock:
            if self._alive():
                raise CommandError(409, "pare o paper trading antes de retomar o kill switch")
            with self._db_lock:
                ok = self._ctl_kill_switch().resume(phrase, note)
            if not ok:
                raise CommandError(400, f"retomada recusada: informe a frase '{RESUME_PHRASE}' e uma justificativa")
            self._event("kill_switch_retomado", note[:300])
            return {"ok": True, "mensagem": "Kill switch desativado. Entradas continuam pausadas até você retomá-las."}

    def shutdown(self) -> dict[str, Any]:
        """Encerramento do painel: para o laço de forma organizada. Sem ordens, sem liquidação, sem apagar dados."""
        if self._shutdown_done:  # idempotente (o servidor e o CLI podem chamar)
            return {"ok": True, "parado": not self._alive()}
        self._shutdown_done = True
        res = self.stop()
        n_open = len(readmodel.positions_for(self.s.db_path, "OPEN"))
        if n_open:
            self._event("painel_encerrado_com_posicoes", f"{n_open} posição(ões) simulada(s) aberta(s) sem monitoramento", "painel")
        return res

    # ------------------------------------------------------------------ leitura
    def status(self) -> dict[str, Any]:
        now = self.clock()
        with readmodel.open_ro(self.s.db_path) as c:
            ks = readmodel.kill_switch_state(c)
            last_cycle = readmodel._meta(c, "last_cycle", None) if c else None
            data = readmodel.data_status(c, self.s, now)
            n_open = len(readmodel.positions(c, "OPEN"))
            paused_db = bool(readmodel._meta(c, "entries_paused", False)) if c else False
            last_event = readmodel._rows(c, "SELECT ts, event, detail FROM bot_events ORDER BY id DESC LIMIT 1") if c else []
        state = self.state
        alive = state != "PARADO"
        avisos: list[str] = []
        if self.live_requested_in_config:
            avisos.append("A configuração pedia outro modo (ex.: BOT_MODE=live): IGNORADO. O painel só executa PAPER.")
        if not alive and n_open:
            avisos.append(f"{n_open} posição(ões) simulada(s) aberta(s) SEM monitoramento de stop (robô parado).")
        if ks["ativo"]:
            avisos.append(f"Kill switch ATIVO: {ks['motivo']}")
        avisos += data["alertas"]
        if last_cycle and last_cycle.get("pronto") is False:
            avisos.append("Startup reprovado: MODO SEGURO — " + (last_cycle.get("falha_startup") or ""))
        if self.consecutive_errors:
            avisos.append(f"{self.consecutive_errors} erro(s) consecutivo(s) no ciclo: {self.last_error}")
        paused = self._entries_paused() if alive else paused_db
        return {
            "estado": state, "modo": "PAPER", "simulado": True,
            "live": {"bloqueado": True, "motivo": LIVE_MOTIVO},
            "kill_switch": ks, "entradas_pausadas": paused,
            "ciclos": self.cycles, "iniciado_em": self.started_at, "ultimo_ciclo": last_cycle, "ultimo_ciclo_local": self.last_cycle_at,
            "erros_consecutivos": self.consecutive_errors, "ultimo_erro": self.last_error,
            "posicoes_abertas": n_open, "dados": data, "avisos": avisos, "ultimo_evento": dict(last_event[0]) if last_event else None,
            "agora": now.isoformat(), "pid": os.getpid(), "intervalo_ciclo_s": self.poll,
            "acoes_permitidas": {
                "iniciar": state == "PARADO",
                "pausar_entradas": alive and not paused,
                "retomar_entradas": paused and not ks["ativo"],
                "parar": alive and state != "PARANDO",
                "emergencia": True,
                "retomar_kill_switch": ks["ativo"] and state == "PARADO",
            },
        }
