from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


GENESIS_HASH = "0" * 64


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    event_count: int
    tail_hash: str
    errors: tuple[str, ...]


class AuditLedger:
    """HMAC-signed hash chain stored in SQLite."""

    def __init__(self, database_path: Path, key_path: Path):
        self.database_path = database_path.resolve()
        self.key_path = key_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.key = self._load_or_create_key()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY,
                    body_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    signature TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _load_or_create_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            key = self.key_path.read_bytes()
        else:
            key = os.urandom(32)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        if len(key) != 32:
            raise RuntimeError(f"invalid audit key length: {self.key_path}")
        return key

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def append(
        self,
        event_type: str,
        workflow_id: str,
        payload: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if not event_type or not workflow_id or not isinstance(payload, dict):
            raise ValueError("event_type, workflow_id and object payload are required")
        owns_connection = connection is None
        active = connection or self.connect()
        try:
            row = active.execute(
                "SELECT sequence, event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = (int(row["sequence"]) + 1) if row else 1
            parent_hash = str(row["event_hash"]) if row else GENESIS_HASH
            body = {
                "sequence": sequence,
                "timestamp": utc_now(),
                "event_type": event_type,
                "workflow_id": workflow_id,
                "payload": payload,
                "parent_hash": parent_hash,
            }
            body_json = canonical_json(body)
            event_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
            signature = hmac.new(
                self.key,
                event_hash.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            active.execute(
                "INSERT INTO audit_events(sequence, body_json, event_hash, signature) VALUES (?, ?, ?, ?)",
                (sequence, body_json, event_hash, signature),
            )
            if owns_connection:
                active.commit()
            return {**body, "event_hash": event_hash, "signature": signature}
        finally:
            if owns_connection:
                active.close()

    def events(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT sequence, body_json, event_hash, signature FROM audit_events ORDER BY sequence"
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            body = json.loads(str(row["body_json"]))
            events.append(
                {
                    **body,
                    "event_hash": str(row["event_hash"]),
                    "signature": str(row["signature"]),
                }
            )
        return events

    def verify(self) -> AuditVerification:
        errors: list[str] = []
        parent_hash = GENESIS_HASH
        try:
            events = self.events()
        except (json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            return AuditVerification(False, 0, GENESIS_HASH, (str(exc),))
        for expected_sequence, event in enumerate(events, 1):
            body = {
                key: event.get(key)
                for key in (
                    "sequence",
                    "timestamp",
                    "event_type",
                    "workflow_id",
                    "payload",
                    "parent_hash",
                )
            }
            if body["sequence"] != expected_sequence:
                errors.append(f"sequence mismatch at event {expected_sequence}")
            if body["parent_hash"] != parent_hash:
                errors.append(f"parent hash mismatch at event {expected_sequence}")
            expected_hash = hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest()
            if event.get("event_hash") != expected_hash:
                errors.append(f"event hash mismatch at event {expected_sequence}")
            expected_signature = hmac.new(
                self.key,
                expected_hash.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(
                str(event.get("signature", "")), expected_signature
            ):
                errors.append(f"signature mismatch at event {expected_sequence}")
            parent_hash = str(event.get("event_hash", parent_hash))
        return AuditVerification(
            valid=not errors,
            event_count=len(events),
            tail_hash=parent_hash,
            errors=tuple(errors),
        )
