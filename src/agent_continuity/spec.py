from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any
import urllib.parse

from .adapters import CommandAdapter, FileArtifactAdapter, HttpJsonAdapter
from .ledger import canonical_json
from .runtime import StepContext, StepHandler, WorkflowEngine


SPEC_SCHEMA = "agent-continuity.workflow.v1"
SUPPORTED_STEP_TYPES = {"command", "file", "http-json"}


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_step(raw: object, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"steps[{index}] must be an object")
    step = dict(raw)
    step_id = _require_string(step.get("id"), f"steps[{index}].id")
    step_type = _require_string(step.get("type"), f"steps[{index}].type")
    if step_type not in SUPPORTED_STEP_TYPES:
        raise ValueError(f"unsupported step type: {step_type}")
    normalized: dict[str, Any] = {"id": step_id, "type": step_type}
    if step_type == "file":
        normalized["target"] = _require_string(
            step.get("target"), f"steps[{index}].target"
        )
        if not isinstance(step.get("text"), str):
            raise ValueError(f"steps[{index}].text must be a string")
        normalized["text"] = step["text"]
    elif step_type == "http-json":
        url = _require_string(step.get("url"), f"steps[{index}].url")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"steps[{index}].url must be an absolute HTTP URL")
        headers = step.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError(f"steps[{index}].headers must contain string pairs")
        timeout = float(step.get("timeout", 15.0))
        if not 0 < timeout <= 3600:
            raise ValueError(f"steps[{index}].timeout must be within 0 and 3600 seconds")
        normalized.update(
            {
                "url": url,
                "method": str(step.get("method", "POST")).upper(),
                "payload": step.get("payload"),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
    else:
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or any(
            not isinstance(item, str) or not item for item in argv
        ):
            raise ValueError(f"steps[{index}].argv must be a non-empty string array")
        timeout = float(step.get("timeout", 60.0))
        if not 0 < timeout <= 3600:
            raise ValueError(f"steps[{index}].timeout must be within 0 and 3600 seconds")
        normalized.update(
            {
                "argv": list(argv),
                "cwd": str(step.get("cwd", ".")),
                "timeout": timeout,
            }
        )
    return normalized


def load_spec(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid workflow JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("workflow spec must be an object")
    if raw.get("schema") != SPEC_SCHEMA:
        raise ValueError(f"workflow schema must be {SPEC_SCHEMA}")
    workflow_id = _require_string(raw.get("workflow_id"), "workflow_id")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 100:
        raise ValueError("steps must contain between 1 and 100 entries")
    steps = [_validate_step(step, index) for index, step in enumerate(raw_steps)]
    step_ids = [step["id"] for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("step ids must be unique")
    return {"schema": SPEC_SCHEMA, "workflow_id": workflow_id, "steps": steps}


def spec_sha256(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def validate_spec(spec_path: Path) -> dict[str, Any]:
    resolved = spec_path.resolve()
    spec = load_spec(resolved)
    step_types = {step["type"] for step in spec["steps"]}
    required_authorizations = []
    if "command" in step_types:
        required_authorizations.append("allow-command")
    if "http-json" in step_types:
        required_authorizations.append("allow-http")
    return {
        "schema": SPEC_SCHEMA,
        "workflow_id": spec["workflow_id"],
        "spec_path": str(resolved),
        "spec_sha256": spec_sha256(spec),
        "steps": [
            {"position": index, "id": step["id"], "type": step["type"]}
            for index, step in enumerate(spec["steps"])
        ],
        "required_authorizations": required_authorizations,
        "valid": True,
    }


def _resolve_relative(base: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _handler_for_step(step: dict[str, Any], spec_dir: Path) -> StepHandler:
    step_type = step["type"]
    if step_type == "file":
        adapter = FileArtifactAdapter()
        target = _resolve_relative(spec_dir, step["target"])
        text = step["text"]

        def file_handler(context: StepContext) -> dict[str, Any]:
            return adapter.write_text(context, target, text)

        return file_handler
    if step_type == "http-json":
        adapter = HttpJsonAdapter()

        def http_handler(context: StepContext) -> dict[str, Any]:
            return adapter.request(
                context,
                step["url"],
                step["payload"],
                method=step["method"],
                headers=step["headers"],
                timeout=step["timeout"],
            )

        return http_handler
    adapter = CommandAdapter()
    argv = [sys.executable if item == "$python" else item for item in step["argv"]]
    cwd = _resolve_relative(spec_dir, step["cwd"])

    def command_handler(context: StepContext) -> dict[str, Any]:
        return adapter.run(context, argv, cwd=cwd, timeout=step["timeout"])

    return command_handler


def run_spec(
    spec_path: Path,
    home: Path,
    *,
    allow_command: bool = False,
    allow_http: bool = False,
) -> dict[str, Any]:
    resolved_spec_path = spec_path.resolve()
    spec = load_spec(resolved_spec_path)
    step_types = {step["type"] for step in spec["steps"]}
    if "command" in step_types and not allow_command:
        raise PermissionError("workflow contains command steps; explicit authorization is required")
    if "http-json" in step_types and not allow_http:
        raise PermissionError("workflow contains HTTP steps; explicit authorization is required")
    engine = WorkflowEngine(home)
    step_names = [step["id"] for step in spec["steps"]]
    engine.start(spec["workflow_id"], step_names, definition=spec)
    handlers = {
        step["id"]: _handler_for_step(step, resolved_spec_path.parent)
        for step in spec["steps"]
    }
    status = engine.run(spec["workflow_id"], handlers)
    verification = engine.ledger.verify()
    return {
        "schema": SPEC_SCHEMA,
        "workflow_id": spec["workflow_id"],
        "spec_path": str(resolved_spec_path),
        "spec_sha256": spec_sha256(spec),
        "status": status,
        "audit": {
            "valid": verification.valid,
            "event_count": verification.event_count,
            "tail_hash": verification.tail_hash,
            "errors": list(verification.errors),
        },
    }


def spec_status(spec_path: Path, home: Path) -> dict[str, Any]:
    spec = load_spec(spec_path)
    engine = WorkflowEngine(home)
    return {
        "workflow_id": spec["workflow_id"],
        "spec_sha256": spec_sha256(spec),
        "status": engine.status(spec["workflow_id"]),
    }
