"""Trava de instância única baseada em bloqueio do sistema operacional (liberada automaticamente se o processo morrer).

Windows: msvcrt.locking; POSIX: fcntl.flock. Usada para (1) o laço de trading — CLI ou painel, nunca os dois — e
(2) o próprio painel. Evita execução duplicada sobre a mesma base (posições/intents duplicados)."""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str | Path, label: str = "instância"):
        self.path, self.label = Path(path), label
        self._fh = None

    def acquire(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - mantido aberto enquanto o bloqueio vale
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"já existe outra {self.label} em execução ({self.path.name})") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def bot_lock_path(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".robo.lock")


def panel_lock_path(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".painel.lock")
