"""Append-only production decision journal for deterministic replay.

The journal stores normalized decisions together with the raw market inputs that
produced them.  It deliberately excludes credentials and authentication data.
SQLite WAL mode keeps writes durable while validation workers record events in
parallel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("algo_trader")

_SECRET_KEYS = {
    "access_token",
    "apikey",
    "api_key",
    "authorization",
    "client_id",
    "clientcode",
    "feed_token",
    "jwt_token",
    "password",
    "pin",
    "refresh_token",
    "secret",
    "totp",
    "totp_secret",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, Path)):
        return str(value)
    return repr(value)


def redact_secrets(value: Any) -> Any:
    """Recursively remove known credential fields before persistence."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            compacted = normalized.replace("_", "")
            sensitive = (
                normalized in _SECRET_KEYS
                or compacted in {
                    "accesstoken",
                    "apikey",
                    "authorization",
                    "clientid",
                    "feedtoken",
                    "jwttoken",
                    "password",
                    "refreshtoken",
                    "totpsecret",
                }
                or normalized.endswith(
                    (
                        "_access_token",
                        "_api_key",
                        "_client_id",
                        "_password",
                        "_pin",
                        "_secret",
                        "_totp",
                    )
                )
            )
            cleaned[str(key)] = "[REDACTED]" if sensitive else redact_secrets(child)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_secrets(child) for child in value]
    return value


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        redact_secrets(value),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_git_revision(root: str | Path | None = None) -> str:
    """Return the deployed source revision without failing outside Git."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class DecisionJournal:
    """Thread-safe append-only store for production inputs and outcomes."""

    def __init__(
        self,
        path: str | Path = "data/decision_journal.sqlite3",
        *,
        snapshot_dir: str | Path = "data/universe_snapshots",
        mode: str = "paper",
        broker: str = "angelone",
        config: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex
        self._cycle_id = ""
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()
        safe_config = redact_secrets(config or {})
        self._connection.execute(
            """
            INSERT INTO runs (
                run_id, started_at_utc, mode, broker, git_revision,
                config_hash, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                _utc_now(),
                mode,
                broker,
                current_git_revision(Path(__file__).resolve().parents[1]),
                stable_hash(safe_config),
                json.dumps(safe_config, sort_keys=True, default=_json_default),
            ),
        )
        self._connection.commit()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at_utc TEXT NOT NULL,
                mode TEXT NOT NULL,
                broker TEXT NOT NULL,
                git_revision TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                config_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decision_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                exchange_timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                token TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_decision_events_run_sequence
                ON decision_events(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_decision_events_type
                ON decision_events(event_type, recorded_at_utc);

            CREATE TABLE IF NOT EXISTS universe_snapshots (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                captured_at_utc TEXT NOT NULL,
                universe_hash TEXT NOT NULL,
                instrument_count INTEGER NOT NULL,
                instruments_json TEXT NOT NULL,
                UNIQUE(trading_date, universe_hash),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE TRIGGER IF NOT EXISTS decision_events_no_update
            BEFORE UPDATE ON decision_events BEGIN
                SELECT RAISE(ABORT, 'decision events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS decision_events_no_delete
            BEFORE DELETE ON decision_events BEGIN
                SELECT RAISE(ABORT, 'decision events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS universe_snapshots_no_update
            BEFORE UPDATE ON universe_snapshots BEGIN
                SELECT RAISE(ABORT, 'universe snapshots are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS universe_snapshots_no_delete
            BEFORE DELETE ON universe_snapshots BEGIN
                SELECT RAISE(ABORT, 'universe snapshots are append-only');
            END;
            """
        )
        self._connection.commit()

    def set_cycle(self, cycle_id: str) -> None:
        self._cycle_id = cycle_id

    def record(
        self,
        event_type: str,
        *,
        symbol: str = "",
        token: str = "",
        decision: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
        exchange_timestamp: str = "",
        cycle_id: str | None = None,
    ) -> str:
        """Append one redacted event and return its immutable event ID."""
        event_id = uuid.uuid4().hex
        safe_payload = redact_secrets(payload or {})
        payload_json = json.dumps(
            safe_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO decision_events (
                    event_id, run_id, cycle_id, recorded_at_utc,
                    exchange_timestamp, event_type, symbol, token,
                    decision, reason, payload_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    self.run_id,
                    cycle_id if cycle_id is not None else self._cycle_id,
                    _utc_now(),
                    exchange_timestamp,
                    event_type,
                    symbol,
                    token,
                    decision,
                    reason,
                    payload_json,
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                ),
            )
            self._connection.commit()
        return event_id

    def snapshot_universe(
        self,
        trading_date: str,
        instruments: list[dict[str, Any]],
    ) -> Path:
        """Persist a dated, normalized universe in SQLite and an immutable file."""
        normalized = sorted(
            (
                {
                    "symbol": str(item.get("symbol", "")),
                    "token": str(item.get("token", "")),
                    "name": str(item.get("name", "")),
                    "restricted_reason": str(item.get("restricted_reason", "")),
                    "is_fno": item.get("is_fno"),
                    "tradability_lists_complete": item.get(
                        "tradability_lists_complete"
                    ),
                }
                for item in instruments
                if item.get("symbol") and item.get("token")
            ),
            key=lambda item: (item["symbol"], item["token"]),
        )
        universe_hash = stable_hash(normalized)
        instruments_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO universe_snapshots (
                    run_id, trading_date, captured_at_utc, universe_hash,
                    instrument_count, instruments_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    trading_date,
                    _utc_now(),
                    universe_hash,
                    len(normalized),
                    instruments_json,
                ),
            )
            self._connection.commit()

        path = self.snapshot_dir / f"{trading_date}_{universe_hash[:12]}.json"
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(instruments_json + "\n", encoding="utf-8")
            temporary.replace(path)
        self.record(
            "universe_snapshot",
            decision="captured",
            payload={
                "trading_date": trading_date,
                "instrument_count": len(normalized),
                "universe_hash": universe_hash,
                "path": str(path),
            },
        )
        return path

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        """Read this run's events in causal order."""
        query = (
            "SELECT sequence, event_id, cycle_id, recorded_at_utc, "
            "exchange_timestamp, event_type, symbol, token, decision, reason, "
            "payload_json FROM decision_events WHERE run_id = ?"
        )
        parameters: list[Any] = [self.run_id]
        if event_type:
            query += " AND event_type = ?"
            parameters.append(event_type)
        query += " ORDER BY sequence"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            {
                "sequence": row[0],
                "event_id": row[1],
                "cycle_id": row[2],
                "recorded_at_utc": row[3],
                "exchange_timestamp": row[4],
                "event_type": row[5],
                "symbol": row[6],
                "token": row[7],
                "decision": row[8],
                "reason": row[9],
                "payload": json.loads(row[10]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> DecisionJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
