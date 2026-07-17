from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable

from .lease import RuntimeLease
from .ledger import AuditLedger, canonical_json, utc_now


StepHandler = Callable[["StepContext"], dict[str, Any] | None]


@dataclass(frozen=True)
class StepContext:
    workflow_id: str
    position: int
    step_name: str
    attempt: int
    idempotency_key: str
    home: Path


def _definition_hash(definition: object) -> str:
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def _idempotency_key(workflow_id: str, position: int, step_name: str) -> str:
    material = f"{workflow_id}\0{position}\0{step_name}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class WorkflowEngine:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.database_path = self.home / "continuity.db"
        self.ledger = AuditLedger(self.database_path, self.home / "audit.key")
        self.lease = RuntimeLease(self.home / "runtime.lock")
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    definition_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    workflow_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    result_json TEXT,
                    last_error TEXT,
                    PRIMARY KEY (workflow_id, position),
                    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                );
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return self.ledger.connect()

    def _begin(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def start(
        self,
        workflow_id: str,
        step_names: Iterable[str],
        *,
        definition: object | None = None,
    ) -> dict[str, Any]:
        names = list(step_names)
        if not workflow_id or not names or any(not name for name in names):
            raise ValueError("workflow_id and non-empty step names are required")
        expected_hash = _definition_hash(definition if definition is not None else names)
        with self.lease:
            verification = self.ledger.verify()
            if not verification.valid:
                raise RuntimeError(f"refuse start with invalid audit chain: {verification.errors}")
            connection = self._begin()
            try:
                existing = connection.execute(
                    "SELECT definition_hash FROM workflows WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing:
                    if str(existing["definition_hash"]) != expected_hash:
                        raise ValueError("workflow definition changed for existing workflow_id")
                    connection.rollback()
                    return self.status(workflow_id)
                now = utc_now()
                connection.execute(
                    "INSERT INTO workflows VALUES (?, 'PENDING', ?, ?, ?)",
                    (workflow_id, expected_hash, now, now),
                )
                for position, name in enumerate(names):
                    connection.execute(
                        """
                        INSERT INTO workflow_steps(
                            workflow_id, position, step_name, status, attempts,
                            idempotency_key, result_json, last_error
                        ) VALUES (?, ?, ?, 'PENDING', 0, ?, NULL, NULL)
                        """,
                        (
                            workflow_id,
                            position,
                            name,
                            _idempotency_key(workflow_id, position, name),
                        ),
                    )
                self.ledger.append(
                    "workflow_started",
                    workflow_id,
                    {"steps": names, "definition_hash": expected_hash},
                    connection=connection,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.status(workflow_id)

    def _recover_interrupted(self, workflow_id: str) -> None:
        connection = self._begin()
        try:
            rows = connection.execute(
                """
                SELECT position, step_name, attempts, idempotency_key
                FROM workflow_steps
                WHERE workflow_id = ? AND status = 'RUNNING'
                ORDER BY position
                """,
                (workflow_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE workflow_steps
                    SET status = 'PENDING', last_error = 'interrupted before commit'
                    WHERE workflow_id = ? AND position = ?
                    """,
                    (workflow_id, int(row["position"])),
                )
                self.ledger.append(
                    "step_recovered",
                    workflow_id,
                    {
                        "position": int(row["position"]),
                        "step_name": str(row["step_name"]),
                        "previous_attempt": int(row["attempts"]),
                        "idempotency_key": str(row["idempotency_key"]),
                    },
                    connection=connection,
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def run(
        self,
        workflow_id: str,
        handlers: dict[str, StepHandler],
    ) -> dict[str, Any]:
        with self.lease:
            verification = self.ledger.verify()
            if not verification.valid:
                raise RuntimeError(f"refuse run with invalid audit chain: {verification.errors}")
            self._recover_interrupted(workflow_id)
            while True:
                with closing(self._connect()) as read_connection:
                    workflow = read_connection.execute(
                        "SELECT status FROM workflows WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchone()
                    if not workflow:
                        raise KeyError(f"unknown workflow: {workflow_id}")
                    row = read_connection.execute(
                        """
                        SELECT position, step_name, status, attempts, idempotency_key
                        FROM workflow_steps
                        WHERE workflow_id = ? AND status != 'COMPLETED'
                        ORDER BY position LIMIT 1
                        """,
                        (workflow_id,),
                    ).fetchone()
                if row is None:
                    if str(workflow["status"]) != "COMPLETED":
                        connection = self._begin()
                        try:
                            connection.execute(
                                "UPDATE workflows SET status = 'COMPLETED', updated_at = ? WHERE workflow_id = ?",
                                (utc_now(), workflow_id),
                            )
                            self.ledger.append(
                                "workflow_completed",
                                workflow_id,
                                {},
                                connection=connection,
                            )
                            connection.commit()
                        except BaseException:
                            connection.rollback()
                            raise
                        finally:
                            connection.close()
                    return self.status(workflow_id)

                step_name = str(row["step_name"])
                handler = handlers.get(step_name)
                if handler is None:
                    raise KeyError(f"missing handler for step: {step_name}")
                position = int(row["position"])
                attempt = int(row["attempts"]) + 1
                idempotency_key = str(row["idempotency_key"])
                connection = self._begin()
                try:
                    connection.execute(
                        """
                        UPDATE workflow_steps
                        SET status = 'RUNNING', attempts = ?, last_error = NULL
                        WHERE workflow_id = ? AND position = ?
                        """,
                        (attempt, workflow_id, position),
                    )
                    connection.execute(
                        "UPDATE workflows SET status = 'RUNNING', updated_at = ? WHERE workflow_id = ?",
                        (utc_now(), workflow_id),
                    )
                    self.ledger.append(
                        "step_started",
                        workflow_id,
                        {
                            "position": position,
                            "step_name": step_name,
                            "attempt": attempt,
                            "idempotency_key": idempotency_key,
                        },
                        connection=connection,
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

                context = StepContext(
                    workflow_id=workflow_id,
                    position=position,
                    step_name=step_name,
                    attempt=attempt,
                    idempotency_key=idempotency_key,
                    home=self.home,
                )
                try:
                    result = handler(context) or {}
                    result_json = canonical_json(result)
                except Exception as exc:
                    connection = self._begin()
                    try:
                        connection.execute(
                            """
                            UPDATE workflow_steps
                            SET status = 'PENDING', last_error = ?
                            WHERE workflow_id = ? AND position = ?
                            """,
                            (str(exc), workflow_id, position),
                        )
                        self.ledger.append(
                            "step_failed",
                            workflow_id,
                            {
                                "position": position,
                                "step_name": step_name,
                                "attempt": attempt,
                                "error": str(exc),
                                "idempotency_key": idempotency_key,
                            },
                            connection=connection,
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        connection.close()
                    raise

                connection = self._begin()
                try:
                    connection.execute(
                        """
                        UPDATE workflow_steps
                        SET status = 'COMPLETED', result_json = ?, last_error = NULL
                        WHERE workflow_id = ? AND position = ?
                        """,
                        (result_json, workflow_id, position),
                    )
                    self.ledger.append(
                        "step_completed",
                        workflow_id,
                        {
                            "position": position,
                            "step_name": step_name,
                            "attempt": attempt,
                            "idempotency_key": idempotency_key,
                            "result": result,
                        },
                        connection=connection,
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

    def status(self, workflow_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            workflow = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if not workflow:
                return {"workflow_id": workflow_id, "status": "NOT_FOUND", "steps": []}
            rows = connection.execute(
                """
                SELECT position, step_name, status, attempts, idempotency_key,
                       result_json, last_error
                FROM workflow_steps WHERE workflow_id = ? ORDER BY position
                """,
                (workflow_id,),
            ).fetchall()
        steps = []
        for row in rows:
            steps.append(
                {
                    "position": int(row["position"]),
                    "step_name": str(row["step_name"]),
                    "status": str(row["status"]),
                    "attempts": int(row["attempts"]),
                    "idempotency_key": str(row["idempotency_key"]),
                    "result": json.loads(str(row["result_json"]))
                    if row["result_json"]
                    else None,
                    "last_error": row["last_error"],
                }
            )
        return {
            "workflow_id": workflow_id,
            "status": str(workflow["status"]),
            "definition_hash": str(workflow["definition_hash"]),
            "steps": steps,
        }
