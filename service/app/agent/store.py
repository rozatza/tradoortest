"""Thread-safe SQLite store for agent sessions, events, and proposals.

One connection, one ``RLock``, and ``BEGIN IMMEDIATE`` for every write.
Every row has ``user_id``. There is no users table. Market bars, backtest
results, and pool snapshots are not columns in this file; callers run
``sanitize`` before they insert tool payloads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.config import MAX_STORED_CHARS, TIERS
from app.agent.errors import StoreUnavailable
from app.agent.sanitize import dumps_stored, redact_secrets
from app.strategies.store import utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    tier TEXT NOT NULL CHECK (tier IN ('fast', 'balanced', 'max')),
    strategy_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    turn_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_name TEXT,
    tool_calls_json TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);
CREATE TABLE IF NOT EXISTS agent_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    turn_id TEXT,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN (
        'user_message', 'tool_start', 'tool_complete', 'proposal_created',
        'proposal_decided', 'assistant_message', 'error', 'turn_complete'
    )),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);
CREATE TABLE IF NOT EXISTS agent_proposals (
    proposal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    from_ir_hash TEXT NOT NULL,
    to_ir_hash TEXT NOT NULL,
    dsl_diff TEXT NOT NULL,
    ir_diff_json TEXT NOT NULL,
    candidate_dsl TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    candidate_ir_strategy_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'applying', 'accepted', 'rejected', 'conflict')
    ),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    applied_version INTEGER,
    applied_ir_hash TEXT,
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);
CREATE TABLE IF NOT EXISTS agent_validated_hashes (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ir_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id, ir_hash),
    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS agent_sessions_user
    ON agent_sessions(user_id, updated_at, session_id);
CREATE INDEX IF NOT EXISTS agent_proposals_user
    ON agent_proposals(user_id, created_at, proposal_id);
"""

_PROPOSAL_STATUSES = frozenset({"pending", "applying", "accepted", "rejected", "conflict"})
_EVENT_TYPES = frozenset(
    {
        "user_message",
        "tool_start",
        "tool_complete",
        "proposal_created",
        "proposal_decided",
        "assistant_message",
        "error",
        "turn_complete",
    }
)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    user_id: str
    title: str | None
    tier: str
    strategy_id: str | None
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "tier": self.tier,
            "strategy_id": self.strategy_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: str
    session_id: str
    user_id: str
    turn_id: str | None
    role: str
    content: str
    tool_call_id: str | None
    tool_name: str | None
    tool_calls_json: str | None
    seq: int
    created_at: str

    def public(self) -> dict[str, Any]:
        calls: object = None
        if self.tool_calls_json:
            try:
                loaded = json.loads(self.tool_calls_json)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, list):
                calls = loaded
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_calls": calls,
            "seq": self.seq,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    session_id: str
    turn_id: str
    user_id: str
    strategy_id: str
    from_version: int
    to_version: int
    from_ir_hash: str
    to_ir_hash: str
    dsl_diff: str
    ir_diff: list[Any]
    candidate_dsl: str
    candidate_name: str
    candidate_ir_strategy_id: str
    status: str
    created_at: str
    decided_at: str | None
    applied_version: int | None
    applied_ir_hash: str | None

    def summary(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "from_ir_hash": self.from_ir_hash,
            "to_ir_hash": self.to_ir_hash,
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "applied_version": self.applied_version,
            "applied_ir_hash": self.applied_ir_hash,
        }

    def detail(self) -> dict[str, Any]:
        body = self.summary()
        body["dsl_diff"] = self.dsl_diff
        body["ir_diff"] = self.ir_diff
        body["candidate_dsl"] = self.candidate_dsl
        body["candidate_name"] = self.candidate_name
        body["candidate_ir_strategy_id"] = self.candidate_ir_strategy_id
        return body


@dataclass(frozen=True, slots=True)
class ProposalUpdate:
    """A proposal write. ``changed`` is false when the status guard missed."""

    proposal: ProposalRecord
    changed: bool
    event: dict[str, Any] | None


class AgentStore:
    """SQLite file for one process. A failed constructor leaves no open handle."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise StoreUnavailable()
        if busy_timeout_ms < 0 or busy_timeout_ms > 60_000:
            raise StoreUnavailable()
        self.path = Path(path)
        self._clock = clock or utc_now
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._conn = sqlite3.connect(
                str(self.path),
                timeout=busy_timeout_ms / 1000,
                check_same_thread=False,
                isolation_level=None,
                uri=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.executescript(_SCHEMA)
        except Exception:
            self.close()
            raise StoreUnavailable() from None

    def close(self) -> None:
        """Close the connection. Later calls raise ``StoreUnavailable``."""
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.close()

    def now(self) -> str:
        return self._clock()

    def create_session(
        self,
        user_id: str,
        *,
        title: str | None,
        tier: str,
        strategy_id: str | None,
    ) -> SessionRecord:
        if tier not in TIERS:
            raise StoreUnavailable()
        ident = uuid.uuid4().hex
        with self._immediate():
            stamp = self._clock()
            self._conn_or_raise().execute(
                """
                INSERT INTO agent_sessions (
                    session_id, user_id, title, tier, strategy_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ident, _user(user_id), title, tier, strategy_id, stamp, stamp),
            )
        return SessionRecord(ident, user_id, title, tier, strategy_id, stamp, stamp)

    def get_session(self, user_id: str, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._session_row(user_id, session_id)
        if row is None:
            return None
        return _session(row)

    def list_sessions(
        self,
        user_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[SessionRecord], int]:
        with self._lock:
            owner = _user(user_id)
            total = self._scalar(
                "SELECT COUNT(*) FROM agent_sessions WHERE user_id = ?",
                (owner,),
            )
            rows = (
                self._conn_or_raise()
                .execute(
                    """
                SELECT session_id, user_id, title, tier, strategy_id, created_at, updated_at
                FROM agent_sessions
                WHERE user_id = ?
                ORDER BY updated_at DESC, session_id ASC
                LIMIT ? OFFSET ?
                """,
                    (owner, limit, offset),
                )
                .fetchall()
            )
        return [_session(row) for row in rows], total

    def add_message(
        self,
        user_id: str,
        session_id: str,
        *,
        turn_id: str | None,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> MessageRecord:
        stored = _cap_text(redact_secrets(content))
        calls_json = None if tool_calls is None else dumps_stored(tool_calls)
        with self._immediate():
            if self._session_row(user_id, session_id) is None:
                raise StoreUnavailable()
            seq = self._next_seq("agent_messages", session_id)
            stamp = self._clock()
            ident = uuid.uuid4().hex
            self._conn_or_raise().execute(
                """
                INSERT INTO agent_messages (
                    message_id, session_id, user_id, turn_id, role, content,
                    tool_call_id, tool_name, tool_calls_json, seq, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ident,
                    session_id,
                    _user(user_id),
                    turn_id,
                    role,
                    stored,
                    tool_call_id,
                    tool_name,
                    calls_json,
                    seq,
                    stamp,
                ),
            )
            self._touch(user_id, session_id, stamp)
        return MessageRecord(
            ident,
            session_id,
            user_id,
            turn_id,
            role,
            stored,
            tool_call_id,
            tool_name,
            calls_json,
            seq,
            stamp,
        )

    def list_messages(self, user_id: str, session_id: str) -> list[MessageRecord]:
        with self._lock:
            if self._session_row(user_id, session_id) is None:
                return []
            rows = (
                self._conn_or_raise()
                .execute(
                    """
                SELECT message_id, session_id, user_id, turn_id, role, content,
                       tool_call_id, tool_name, tool_calls_json, seq, created_at
                FROM agent_messages
                WHERE session_id = ? AND user_id = ?
                ORDER BY seq ASC
                """,
                    (session_id, _user(user_id)),
                )
                .fetchall()
            )
        return [_message(row) for row in rows]

    def append_event(
        self,
        user_id: str,
        session_id: str,
        *,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if event_type not in _EVENT_TYPES:
            raise StoreUnavailable()
        encoded = dumps_stored(payload)
        with self._immediate():
            if self._session_row(user_id, session_id) is None:
                raise StoreUnavailable()
            seq = self._next_seq("agent_events", session_id)
            stamp = self._clock()
            ident = uuid.uuid4().hex
            self._conn_or_raise().execute(
                """
                INSERT INTO agent_events (
                    event_id, session_id, user_id, turn_id, seq, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ident, session_id, _user(user_id), turn_id, seq, event_type, encoded, stamp),
            )
            self._touch(user_id, session_id, stamp)
        loaded = json.loads(encoded)
        if not isinstance(loaded, dict):
            loaded = {"truncated": True}
        return _event(ident, session_id, turn_id, seq, event_type, stamp, loaded)

    def list_events(
        self,
        user_id: str,
        session_id: str,
        *,
        after_seq: int | None,
    ) -> list[dict[str, Any]] | None:
        """Return events, or None when this user has no such session."""
        with self._lock:
            if self._session_row(user_id, session_id) is None:
                return None
            if after_seq is None:
                rows = (
                    self._conn_or_raise()
                    .execute(
                        """
                    SELECT event_id, session_id, turn_id, seq, type, payload_json, created_at
                    FROM agent_events
                    WHERE session_id = ? AND user_id = ?
                    ORDER BY seq ASC
                    """,
                        (session_id, _user(user_id)),
                    )
                    .fetchall()
                )
            else:
                rows = (
                    self._conn_or_raise()
                    .execute(
                        """
                    SELECT event_id, session_id, turn_id, seq, type, payload_json, created_at
                    FROM agent_events
                    WHERE session_id = ? AND user_id = ? AND seq > ?
                    ORDER BY seq ASC
                    """,
                        (session_id, _user(user_id), after_seq),
                    )
                    .fetchall()
                )
        return [_event_row(row) for row in rows]

    def add_proposal(self, proposal: ProposalRecord) -> ProposalRecord:
        ir_diff = json.dumps(
            proposal.ir_diff, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        with self._immediate():
            if self._session_row(proposal.user_id, proposal.session_id) is None:
                raise StoreUnavailable()
            self._conn_or_raise().execute(
                """
                INSERT INTO agent_proposals (
                    proposal_id, session_id, turn_id, user_id, strategy_id,
                    from_version, to_version, from_ir_hash, to_ir_hash,
                    dsl_diff, ir_diff_json, candidate_dsl, candidate_name,
                    candidate_ir_strategy_id, status, created_at, decided_at,
                    applied_version, applied_ir_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.session_id,
                    proposal.turn_id,
                    _user(proposal.user_id),
                    proposal.strategy_id,
                    proposal.from_version,
                    proposal.to_version,
                    proposal.from_ir_hash,
                    proposal.to_ir_hash,
                    proposal.dsl_diff,
                    ir_diff,
                    proposal.candidate_dsl,
                    proposal.candidate_name,
                    proposal.candidate_ir_strategy_id,
                    proposal.status,
                    proposal.created_at,
                    proposal.decided_at,
                    proposal.applied_version,
                    proposal.applied_ir_hash,
                ),
            )
        return proposal

    def get_proposal(self, user_id: str, proposal_id: str) -> ProposalRecord | None:
        with self._lock:
            row = self._proposal_row(user_id, proposal_id)
        if row is None:
            return None
        return _proposal(row)

    def list_proposals(
        self,
        user_id: str,
        *,
        session_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ProposalRecord], int]:
        if status is not None and status not in _PROPOSAL_STATUSES:
            raise StoreUnavailable()
        clauses = ["user_id = ?"]
        params: list[Any] = [_user(user_id)]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._lock:
            total = self._scalar(
                f"SELECT COUNT(*) FROM agent_proposals WHERE {where}", tuple(params)
            )
            rows = (
                self._conn_or_raise()
                .execute(
                    f"""
                SELECT {_PROPOSAL_COLUMNS}
                FROM agent_proposals
                WHERE {where}
                ORDER BY created_at ASC, proposal_id ASC
                LIMIT ? OFFSET ?
                """,
                    (*params, limit, offset),
                )
                .fetchall()
            )
        return [_proposal(row) for row in rows], total

    def settle_proposal(
        self,
        user_id: str,
        proposal_id: str,
        *,
        expect: str,
        status: str,
        decided_at: str | None,
        applied_version: int | None,
        applied_ir_hash: str | None,
        event_type: str | None,
        event_payload: dict[str, Any] | None,
        turn_id: str | None,
    ) -> ProposalUpdate | None:
        """Change status when it is ``expect``. Insert an event in that transaction."""
        if status not in _PROPOSAL_STATUSES or expect not in _PROPOSAL_STATUSES:
            raise StoreUnavailable()
        if event_type is not None and event_type not in _EVENT_TYPES:
            raise StoreUnavailable()
        with self._immediate():
            row = self._proposal_row(user_id, proposal_id)
            if row is None:
                return None
            current = _proposal(row)
            if current.status != expect:
                return ProposalUpdate(current, False, None)
            self._conn_or_raise().execute(
                """
                UPDATE agent_proposals
                SET status = ?, decided_at = ?, applied_version = ?, applied_ir_hash = ?
                WHERE proposal_id = ? AND user_id = ? AND status = ?
                """,
                (
                    status,
                    decided_at,
                    applied_version,
                    applied_ir_hash,
                    proposal_id,
                    _user(user_id),
                    expect,
                ),
            )
            updated_row = self._proposal_row(user_id, proposal_id)
            if updated_row is None:
                raise StoreUnavailable()
            updated = _proposal(updated_row)
            event = None
            if event_type is not None and event_payload is not None:
                event = self._insert_event(
                    user_id,
                    updated.session_id,
                    turn_id=turn_id or updated.turn_id,
                    event_type=event_type,
                    payload=event_payload,
                )
            return ProposalUpdate(updated, True, event)

    def record_validation(self, user_id: str, session_id: str, ir_hash: str) -> None:
        if len(ir_hash) != 64 or any(char not in "0123456789abcdef" for char in ir_hash):
            return
        with self._immediate():
            if self._session_row(user_id, session_id) is None:
                raise StoreUnavailable()
            self._conn_or_raise().execute(
                """
                INSERT OR IGNORE INTO agent_validated_hashes (
                    user_id, session_id, ir_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (_user(user_id), session_id, ir_hash, self._clock()),
            )

    def has_validation(self, user_id: str, session_id: str, ir_hash: str) -> bool:
        with self._lock:
            row = (
                self._conn_or_raise()
                .execute(
                    """
                SELECT 1 FROM agent_validated_hashes
                WHERE user_id = ? AND session_id = ? AND ir_hash = ?
                """,
                    (_user(user_id), session_id, ir_hash),
                )
                .fetchone()
            )
        return row is not None

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        with self._lock:
            conn = self._conn_or_raise()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                self._rollback()
                raise StoreUnavailable() from None
            try:
                yield
                conn.execute("COMMIT")
            except StoreUnavailable:
                self._rollback()
                raise
            except sqlite3.Error:
                self._rollback()
                raise StoreUnavailable() from None
            except Exception:
                self._rollback()
                raise

    def _rollback(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            return

    def _conn_or_raise(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise StoreUnavailable()
        return conn

    def _session_row(self, user_id: str, session_id: str) -> sqlite3.Row | None:
        try:
            return (
                self._conn_or_raise()
                .execute(
                    """
                SELECT session_id, user_id, title, tier, strategy_id, created_at, updated_at
                FROM agent_sessions
                WHERE session_id = ? AND user_id = ?
                """,
                    (session_id, _user(user_id)),
                )
                .fetchone()
            )
        except sqlite3.Error:
            raise StoreUnavailable() from None

    def _proposal_row(self, user_id: str, proposal_id: str) -> sqlite3.Row | None:
        try:
            return (
                self._conn_or_raise()
                .execute(
                    f"""
                SELECT {_PROPOSAL_COLUMNS}
                FROM agent_proposals
                WHERE proposal_id = ? AND user_id = ?
                """,
                    (proposal_id, _user(user_id)),
                )
                .fetchone()
            )
        except sqlite3.Error:
            raise StoreUnavailable() from None

    def _touch(self, user_id: str, session_id: str, stamp: str) -> None:
        self._conn_or_raise().execute(
            """
            UPDATE agent_sessions SET updated_at = ?
            WHERE session_id = ? AND user_id = ?
            """,
            (stamp, session_id, _user(user_id)),
        )

    def _next_seq(self, table: str, session_id: str) -> int:
        if table not in {"agent_messages", "agent_events"}:
            raise StoreUnavailable()
        try:
            row = (
                self._conn_or_raise()
                .execute(
                    f"SELECT COALESCE(MAX(seq), 0) FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
                .fetchone()
            )
        except sqlite3.Error:
            raise StoreUnavailable() from None
        if row is None:
            return 1
        return int(row[0]) + 1

    def _scalar(self, sql: str, params: tuple[Any, ...]) -> int:
        try:
            row = self._conn_or_raise().execute(sql, params).fetchone()
        except sqlite3.Error:
            raise StoreUnavailable() from None
        if row is None:
            return 0
        return int(row[0])

    def _insert_event(
        self,
        user_id: str,
        session_id: str,
        *,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = dumps_stored(payload)
        seq = self._next_seq("agent_events", session_id)
        stamp = self._clock()
        ident = uuid.uuid4().hex
        self._conn_or_raise().execute(
            """
            INSERT INTO agent_events (
                event_id, session_id, user_id, turn_id, seq, type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ident, session_id, _user(user_id), turn_id, seq, event_type, encoded, stamp),
        )
        self._touch(user_id, session_id, stamp)
        loaded = json.loads(encoded)
        if not isinstance(loaded, dict):
            loaded = {"truncated": True}
        return _event(ident, session_id, turn_id, seq, event_type, stamp, loaded)


_PROPOSAL_COLUMNS = """
    proposal_id, session_id, turn_id, user_id, strategy_id,
    from_version, to_version, from_ir_hash, to_ir_hash,
    dsl_diff, ir_diff_json, candidate_dsl, candidate_name,
    candidate_ir_strategy_id, status, created_at, decided_at,
    applied_version, applied_ir_hash
"""


def _user(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128 or "\x00" in user_id:
        raise StoreUnavailable()
    return user_id


def _cap_text(text: str) -> str:
    if len(text) <= MAX_STORED_CHARS:
        return text
    return text[:MAX_STORED_CHARS]


def _session(row: sqlite3.Row) -> SessionRecord:
    title = row["title"]
    strategy_id = row["strategy_id"]
    return SessionRecord(
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        title=None if title is None else str(title),
        tier=str(row["tier"]),
        strategy_id=None if strategy_id is None else str(strategy_id),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _message(row: sqlite3.Row) -> MessageRecord:
    turn_id = row["turn_id"]
    tool_call_id = row["tool_call_id"]
    tool_name = row["tool_name"]
    calls = row["tool_calls_json"]
    return MessageRecord(
        message_id=str(row["message_id"]),
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        turn_id=None if turn_id is None else str(turn_id),
        role=str(row["role"]),
        content=str(row["content"]),
        tool_call_id=None if tool_call_id is None else str(tool_call_id),
        tool_name=None if tool_name is None else str(tool_name),
        tool_calls_json=None if calls is None else str(calls),
        seq=int(row["seq"]),
        created_at=str(row["created_at"]),
    )


def _proposal(row: sqlite3.Row) -> ProposalRecord:
    try:
        ir_diff = json.loads(str(row["ir_diff_json"]))
    except json.JSONDecodeError:
        raise StoreUnavailable() from None
    if not isinstance(ir_diff, list):
        raise StoreUnavailable()
    decided = row["decided_at"]
    applied = row["applied_version"]
    applied_hash = row["applied_ir_hash"]
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        user_id=str(row["user_id"]),
        strategy_id=str(row["strategy_id"]),
        from_version=int(row["from_version"]),
        to_version=int(row["to_version"]),
        from_ir_hash=str(row["from_ir_hash"]),
        to_ir_hash=str(row["to_ir_hash"]),
        dsl_diff=str(row["dsl_diff"]),
        ir_diff=ir_diff,
        candidate_dsl=str(row["candidate_dsl"]),
        candidate_name=str(row["candidate_name"]),
        candidate_ir_strategy_id=str(row["candidate_ir_strategy_id"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        decided_at=None if decided is None else str(decided),
        applied_version=None if applied is None else int(applied),
        applied_ir_hash=None if applied_hash is None else str(applied_hash),
    )


def _event(
    event_id: str,
    session_id: str,
    turn_id: str | None,
    seq: int,
    event_type: str,
    created_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "event_id": event_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "type": event_type,
        "created_at": created_at,
        "payload": payload,
    }


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        raise StoreUnavailable() from None
    if not isinstance(payload, dict):
        payload = {"truncated": True}
    turn_id = row["turn_id"]
    return _event(
        str(row["event_id"]),
        str(row["session_id"]),
        None if turn_id is None else str(turn_id),
        int(row["seq"]),
        str(row["type"]),
        str(row["created_at"]),
        payload,
    )
