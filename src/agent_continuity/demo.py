from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .runtime import StepContext, WorkflowEngine


DEMO_WORKFLOW_ID = "agent-continuity-demo"
DEMO_STEPS = ["prepare_input", "publish_report", "finalize"]


def _write_json_once(path: Path, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return json.loads(path.read_text(encoding="utf-8")), False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return payload, True


def _effect_handler(label: str, *, crash_once: bool = False):
    def handler(context: StepContext) -> dict[str, Any]:
        receipt_path = context.home / "receipts" / f"{context.idempotency_key}.json"
        receipt = {
            "label": label,
            "idempotency_key": context.idempotency_key,
            "executions": 1,
        }
        stored, created = _write_json_once(receipt_path, receipt)
        if crash_once:
            marker = context.home / "crash_once.marker"
            _, marker_created = _write_json_once(
                marker,
                {"crashed_after_idempotent_effect": context.idempotency_key},
            )
            if marker_created:
                os._exit(23)
        return {
            "receipt": str(receipt_path),
            "created": created,
            "executions": int(stored["executions"]),
        }

    return handler


def run_demo(home: Path, *, crash_once: bool = False) -> dict[str, Any]:
    engine = WorkflowEngine(home)
    engine.start(DEMO_WORKFLOW_ID, DEMO_STEPS)
    handlers = {
        "prepare_input": _effect_handler("prepare_input"),
        "publish_report": _effect_handler("publish_report", crash_once=crash_once),
        "finalize": _effect_handler("finalize"),
    }
    status = engine.run(DEMO_WORKFLOW_ID, handlers)
    verification = engine.ledger.verify()
    receipts = sorted((home / "receipts").glob("*.json"))
    return {
        "status": status,
        "audit": {
            "valid": verification.valid,
            "event_count": verification.event_count,
            "tail_hash": verification.tail_hash,
            "errors": list(verification.errors),
        },
        "receipt_count": len(receipts),
        "receipts": [json.loads(path.read_text(encoding="utf-8")) for path in receipts],
    }
