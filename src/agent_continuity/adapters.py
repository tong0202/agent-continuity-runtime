from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .ledger import canonical_json, utc_now
from .runtime import StepContext


MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_CHARS = 64 * 1024


class AdapterConflictError(RuntimeError):
    pass


class UncertainCommandOutcome(RuntimeError):
    pass


class CommandExecutionError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _operation_hash(value: object) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _receipt_path(context: StepContext, adapter: str) -> Path:
    return (
        context.home
        / "adapter_receipts"
        / adapter
        / f"{context.idempotency_key}.json"
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AdapterConflictError(f"receipt is not an object: {path}")
    return value


def _write_json_once(path: Path, value: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _existing_receipt(path: Path, expected_operation_hash: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    receipt = _read_json(path)
    if receipt.get("operation_hash") != expected_operation_hash:
        raise AdapterConflictError(
            "the idempotency key is already bound to a different operation"
        )
    return receipt


class FileArtifactAdapter:
    def write_text(
        self,
        context: StepContext,
        target: Path,
        text: str,
        *,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        return self.write_bytes(context, target, text.encode(encoding))

    def write_bytes(
        self,
        context: StepContext,
        target: Path,
        content: bytes,
    ) -> dict[str, Any]:
        resolved = target.resolve()
        content_hash = _sha256_bytes(content)
        operation = {
            "adapter": "file",
            "target": str(resolved),
            "content_sha256": content_hash,
            "size": len(content),
        }
        operation_hash = _operation_hash(operation)
        receipt_path = _receipt_path(context, "file")
        existing = _existing_receipt(receipt_path, operation_hash)
        if existing is not None:
            if not resolved.exists() or _sha256_bytes(resolved.read_bytes()) != content_hash:
                raise AdapterConflictError("file receipt exists but target content does not match")
            return {**existing, "replayed": True}

        resolved.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            descriptor = os.open(
                resolved,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if _sha256_bytes(resolved.read_bytes()) != content_hash:
                raise AdapterConflictError(f"target already exists with different content: {resolved}")
        else:
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        receipt = {
            **operation,
            "operation_hash": operation_hash,
            "idempotency_key": context.idempotency_key,
            "status": "COMPLETED",
            "target_created": created,
            "completed_at": utc_now(),
        }
        if not _write_json_once(receipt_path, receipt):
            return {
                **_existing_receipt(receipt_path, operation_hash),
                "replayed": True,
            }
        return {**receipt, "replayed": False}


class HttpJsonAdapter:
    def request(
        self,
        context: StepContext,
        url: str,
        payload: object,
        *,
        method: str = "POST",
        headers: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("HTTP adapter requires an absolute http or https URL")
        normalized_method = method.upper()
        supplied_headers = dict(headers or {})
        if any(name.lower() == "idempotency-key" for name in supplied_headers):
            raise ValueError("Idempotency-Key is controlled by the runtime")
        operation = {
            "adapter": "http-json",
            "method": normalized_method,
            "url": url,
            "payload": payload,
            "headers_sha256": _operation_hash(supplied_headers),
        }
        operation_hash = _operation_hash(operation)
        receipt_path = _receipt_path(context, "http")
        existing = _existing_receipt(receipt_path, operation_hash)
        if existing is not None:
            if existing.get("status") != "COMPLETED":
                raise RuntimeError(
                    f"previous HTTP request ended with status {existing.get('status_code')}"
                )
            return {**existing, "replayed": True}

        body = canonical_json(payload).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **supplied_headers,
            "Idempotency-Key": context.idempotency_key,
        }
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=normalized_method,
        )
        response_read_error: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status_code = int(response.status)
                response_headers = dict(response.headers.items())
                try:
                    response_bytes = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
                except OSError as exc:
                    response_bytes = b""
                    response_read_error = f"{type(exc).__name__}: {exc}"
        except urllib.error.HTTPError as exc:
            try:
                status_code = int(exc.code)
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                try:
                    response_bytes = exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
                except OSError as read_error:
                    response_bytes = b""
                    response_read_error = (
                        f"{type(read_error).__name__}: {read_error}"
                    )
            finally:
                exc.close()
        if len(response_bytes) > MAX_HTTP_RESPONSE_BYTES:
            raise RuntimeError("HTTP response exceeds the 1 MiB receipt limit")
        response_text = response_bytes.decode("utf-8", errors="replace")
        try:
            response_value: object = json.loads(response_text)
        except json.JSONDecodeError:
            response_value = response_text
        receipt = {
            **operation,
            "operation_hash": operation_hash,
            "idempotency_key": context.idempotency_key,
            "status": "COMPLETED" if 200 <= status_code < 300 else "HTTP_ERROR",
            "status_code": status_code,
            "response": response_value,
            "response_content_type": response_headers.get("Content-Type"),
            "completed_at": utc_now(),
        }
        if response_read_error is not None:
            receipt["response_read_error"] = response_read_error
        if not _write_json_once(receipt_path, receipt):
            return {
                **_existing_receipt(receipt_path, operation_hash),
                "replayed": True,
            }
        if not 200 <= status_code < 300:
            raise RuntimeError(f"HTTP request returned status {status_code}")
        return {**receipt, "replayed": False}


class CommandAdapter:
    def run(
        self,
        context: StepContext,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        command = [str(item) for item in argv]
        if not command or any(not item for item in command):
            raise ValueError("command argv must contain non-empty strings")
        resolved_cwd = cwd.resolve() if cwd else context.home
        operation = {
            "adapter": "command",
            "argv": command,
            "cwd": str(resolved_cwd),
        }
        operation_hash = _operation_hash(operation)
        receipt_path = _receipt_path(context, "command")
        existing = _existing_receipt(receipt_path, operation_hash)
        if existing is not None:
            if existing.get("status") == "COMPLETED":
                return {**existing, "replayed": True}
            if existing.get("status") == "STARTED":
                raise UncertainCommandOutcome(
                    "command started previously but no final result was committed; manual reconciliation required"
                )
            raise CommandExecutionError(
                f"previous command attempt ended with status {existing.get('status')}"
            )

        intent = {
            **operation,
            "operation_hash": operation_hash,
            "idempotency_key": context.idempotency_key,
            "status": "STARTED",
            "started_at": utc_now(),
        }
        if not _write_json_once(receipt_path, intent):
            raise UncertainCommandOutcome("command intent already exists without a result")
        try:
            completed = subprocess.run(
                command,
                cwd=resolved_cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except Exception as exc:
            failed = {
                **intent,
                "status": "FAILED",
                "error": str(exc),
                "completed_at": utc_now(),
            }
            _replace_json(receipt_path, failed)
            raise CommandExecutionError(str(exc)) from exc
        receipt = {
            **intent,
            "status": "COMPLETED" if completed.returncode == 0 else "FAILED",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-MAX_COMMAND_OUTPUT_CHARS:],
            "stderr": completed.stderr[-MAX_COMMAND_OUTPUT_CHARS:],
            "completed_at": utc_now(),
        }
        _replace_json(receipt_path, receipt)
        if completed.returncode != 0:
            raise CommandExecutionError(
                f"command exited with code {completed.returncode}"
            )
        return {**receipt, "replayed": False}
