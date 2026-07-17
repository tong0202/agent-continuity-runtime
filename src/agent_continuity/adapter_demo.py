from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any

from .adapters import CommandAdapter, FileArtifactAdapter, HttpJsonAdapter
from .runtime import StepContext, WorkflowEngine


ADAPTER_DEMO_WORKFLOW_ID = "agent-continuity-adapter-demo"
ADAPTER_DEMO_STEPS = ["write_artifact", "send_http", "run_command"]


class _IdempotentHandler(BaseHTTPRequestHandler):
    responses: dict[str, dict[str, Any]] = {}
    side_effect_count = 0

    def do_POST(self) -> None:
        key = self.headers.get("Idempotency-Key", "")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if key not in self.responses:
            type(self).side_effect_count += 1
            self.responses[key] = {
                "accepted": True,
                "idempotency_key": key,
                "payload": payload,
                "side_effect_number": type(self).side_effect_count,
            }
        body = json.dumps(self.responses[key]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def run_adapter_demo(home: Path) -> dict[str, Any]:
    _IdempotentHandler.responses = {}
    _IdempotentHandler.side_effect_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IdempotentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = WorkflowEngine(home)
        engine.start(ADAPTER_DEMO_WORKFLOW_ID, ADAPTER_DEMO_STEPS)
        file_adapter = FileArtifactAdapter()
        http_adapter = HttpJsonAdapter()
        command_adapter = CommandAdapter()
        target = home / "artifacts" / "trial-report.txt"
        url = f"http://127.0.0.1:{server.server_port}/events"
        handlers = {
            "write_artifact": lambda context: file_adapter.write_text(
                context, target, "agent-continuity-adapter-demo\n"
            ),
            "send_http": lambda context: http_adapter.request(
                context, url, {"event": "trial-ready"}
            ),
            "run_command": lambda context: command_adapter.run(
                context,
                [sys.executable, "-c", "print('command-adapter-ok')"],
                cwd=home,
            ),
        }
        status = engine.run(ADAPTER_DEMO_WORKFLOW_ID, handlers)
        verification = engine.ledger.verify()
        return {
            "status": status["status"],
            "steps": status["steps"],
            "artifact_exists": target.exists(),
            "artifact_sha256": status["steps"][0]["result"]["content_sha256"],
            "http_side_effect_count": _IdempotentHandler.side_effect_count,
            "audit_valid": verification.valid,
            "audit_event_count": verification.event_count,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
