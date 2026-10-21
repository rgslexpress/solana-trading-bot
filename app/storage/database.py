"""Persistência SQLite (auditável, com restrições contra duplicidade). Nunca grava segredos."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.domain import ALLOWED_TRANSITIONS, InvalidTransition, OrderState, Position
from app.logging_config import redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_snapshots (id INTEGER PRIMARY KEY, ts TEXT, strategy_version TEXT, config_json TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts TEXT, mint TEXT, symbol TEXT, candle_ts TEXT, type TEXT, score REAL,
  reasons TEXT, rejections TEXT, UNIQUE(mint, candle_ts));
CREATE TABLE IF NOT EXISTS intents (
  id TEXT PRIMARY KEY, created_ts TEXT, side TEXT, mint TEXT, symbol TEXT, state TEXT NOT NULL,
  signature TEXT, mode TEXT, request_json TEXT, result_json TEXT, error TEXT, updated_ts TEXT);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state);
CREATE TABLE IF NOT EXISTS intent_events (id INTEGER PRIMARY KEY, intent_id TEXT, ts TEXT, from_state TEXT, to_state TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS positions (id TEXT PRIMARY KEY, mint TEXT, status TEXT, data_json TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS equity (id INTEGER PRIMARY KEY, ts TEXT, equity REAL, cash REAL, exposure REAL);
CREATE TABLE IF NOT EXISTS kill_switch_events (id INTEGER PRIMARY KEY, ts TEXT, active INTEGER, reason TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS balances (id INTEGER PRIMARY KEY, ts TEXT, mint TEXT, raw_amount TEXT);
CREATE TABLE IF NOT EXISTS bot_events (id INTEGER PRIMARY KEY, ts TEXT, event TEXT, detail TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, params_json TEXT, result_json TEXT);
"""

# Um único intent não-terminal por (mint, side): impede compras/vendas duplicadas por reinício/callback.
_ACTIVE_STATES = ("CREATED", "VALIDATED", "QUOTED", "SIGNED", "SUBMITTED", "UNKNOWN")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_intent ON intents(mint, side) "
            f"WHERE state IN ({','.join(repr(s) for s in _ACTIVE_STATES)})"
        )

    # ---------------------------------------------------------------- util
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def integrity_ok(self) -> bool:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---------------------------------------------------------------- auditoria
    def save_config_snapshot(self, strategy_version: str, config: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO config_snapshots(ts,strategy_version,config_json) VALUES(?,?,?)",
            (now_iso(), strategy_version, json.dumps(redact_obj(config), default=str)),
        )

    def save_signal(self, mint: str, symbol: str, candle_ts: str, type_: str, score: float, reasons: list[str], rejections: list[str]) -> bool:
        """Retorna False se o sinal desse candle já foi registrado (idempotência)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals(ts,mint,symbol,candle_ts,type,score,reasons,rejections) VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), mint, symbol, candle_ts, type_, score, json.dumps(reasons, ensure_ascii=False), json.dumps(rejections, ensure_ascii=False)),
        )
        return cur.rowcount == 1

    def last_signal_ts(self, mint: str) -> str | None:
        # ignora marcadores auxiliares ("<ts>#bloqueio"): o candle já foi decidido uma única vez
        row = self.conn.execute("SELECT MAX(candle_ts) m FROM signals WHERE mint=? AND candle_ts NOT LIKE '%#%'", (mint,)).fetchone()
        return row["m"] if row and row["m"] else None

    def save_equity(self, equity: float, cash: float, exposure: float) -> None:
        self.conn.execute("INSERT INTO equity(ts,equity,cash,exposure) VALUES(?,?,?,?)", (now_iso(), equity, cash, exposure))

    def save_alert(self, level: str, message: str) -> None:
        self.conn.execute("INSERT INTO alerts(ts,level,message) VALUES(?,?,?)", (now_iso(), level, message))

    def save_run(self, kind: str, params: dict[str, Any], result: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs(ts,kind,params_json,result_json) VALUES(?,?,?,?)",
            (now_iso(), kind, json.dumps(redact_obj(params), default=str), json.dumps(result, default=str)),
        )

    def save_balance(self, mint: str, raw_amount: int) -> None:
        self.conn.execute("INSERT INTO balances(ts,mint,raw_amount) VALUES(?,?,?)", (now_iso(), mint, str(raw_amount)))

    def save_bot_event(self, event: str, detail: str = "", source: str = "sistema") -> None:
        """Mudanças de estado do robô (iniciar/pausar/parar/emergência/erros). `detail` é redigido antes de gravar."""
        from app.logging_config import redact_text

        self.conn.execute("INSERT INTO bot_events(ts,event,detail,source) VALUES(?,?,?,?)", (now_iso(), event, redact_text(detail)[:1000], source))

    # ---------------------------------------------------------------- kill switch
    def kill_switch_load(self) -> tuple[bool, str]:
        row = self.conn.execute("SELECT active, reason FROM kill_switch_events ORDER BY id DESC LIMIT 1").fetchone()
        return (bool(row["active"]), row["reason"]) if row else (False, "")

    def kill_switch_save(self, active: bool, reason: str, source: str) -> None:
        self.conn.execute(
            "INSERT INTO kill_switch_events(ts,active,reason,source) VALUES(?,?,?,?)", (now_iso(), int(active), reason, source)
        )

    # ---------------------------------------------------------------- posições
    def save_position(self, p: Position) -> None:
        self.conn.execute(
            "INSERT INTO positions(id,mint,status,data_json,updated_ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status,data_json=excluded.data_json,updated_ts=excluded.updated_ts",
            (p.id, p.mint, p.status, json.dumps(p.to_dict()), now_iso()),
        )

    def load_positions(self, status: str | None = None) -> list[Position]:
        q, args = "SELECT data_json FROM positions", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [Position.from_dict(json.loads(r["data_json"])) for r in self.conn.execute(q + " ORDER BY updated_ts", args)]

    # ---------------------------------------------------------------- intents
    def create_intent(self, intent_id: str, side: str, mint: str, symbol: str, mode: str, request: dict[str, Any]) -> bool:
        """Persiste a intenção ANTES de qualquer envio. False => duplicada (id ou intent ativo igual)."""
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO intents(id,created_ts,side,mint,symbol,state,mode,request_json,updated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                    (intent_id, now_iso(), side, mint, symbol, OrderState.CREATED.value, mode, json.dumps(request, default=str), now_iso()),
                )
                c.execute(
                    "INSERT INTO intent_events(intent_id,ts,from_state,to_state,note) VALUES(?,?,?,?,?)",
                    (intent_id, now_iso(), None, OrderState.CREATED.value, "criada"),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_intent_state(self, intent_id: str, new: OrderState, *, signature: str | None = None, result: dict[str, Any] | None = None, error: str | None = None, note: str = "") -> None:
        with self.tx() as c:
            row = c.execute("SELECT state FROM intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise KeyError(intent_id)
            cur_state = OrderState(row["state"])
            if new not in ALLOWED_TRANSITIONS[cur_state]:
                raise InvalidTransition(f"{cur_state.value} -> {new.value} não permitido")
            c.execute(
                "UPDATE intents SET state=?, signature=COALESCE(?,signature), result_json=COALESCE(?,result_json), "
                "error=COALESCE(?,error), updated_ts=? WHERE id=?",
                (new.value, signature, json.dumps(result, default=str) if result else None, error, now_iso(), intent_id),
            )
            c.execute(
                "INSERT INTO intent_events(intent_id,ts,from_state,to_state,note) VALUES(?,?,?,?,?)",
                (intent_id, now_iso(), row["state"], new.value, note),
            )

    def get_intent(self, intent_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()

    def intents_in_states(self, states: tuple[OrderState, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(states))
        return list(self.conn.execute(f"SELECT * FROM intents WHERE state IN ({marks}) ORDER BY created_ts", tuple(s.value for s in states)))
