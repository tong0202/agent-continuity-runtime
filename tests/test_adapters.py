from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_continuity.adapters import (
    AdapterConflictError,
    CommandAdapter,
    FileArtifactAdapter,
    HttpJsonAdapter,
    UncertainCommandOutcome,
)
from agent_continuity.runtime import StepContext


def context(home: Path, key: str = "a" * 64) -> StepContext:
    return StepContext("adapter-tests", 0, "adapter", 1, key, home)


class _Handler(BaseHTTPRequestHandler):
    responses: dict[str, dict[str, object]] = {}
    side_effect_count = 0

    def do_POST(self) -> None:
        key = self.headers.get("Idempotency-Key", "")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if key not in self.responses:
            type(self).side_effect_count += 1
            self.responses[key] = {
                "key": key,
                "payload": payload,
                "effect": type(self).side_effect_count,
            }
        body = json.dumps(self.responses[key]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class AdapterTests(unittest.TestCase):
    def test_file_adapter_replays_and_rejects_conflicting_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / "result.txt"
            adapter = FileArtifactAdapter()
            first = adapter.write_text(context(home), target, "stable")
            replay = adapter.write_text(context(home), target, "stable")
            self.assertTrue(first["target_created"])
            self.assertTrue(replay["replayed"])
            with self.assertRaises(AdapterConflictError):
                adapter.write_text(context(home), target, "changed")

    def test_http_adapter_sends_stable_key_and_replays_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _Handler.responses = {}
            _Handler.side_effect_count = 0
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                adapter = HttpJsonAdapter()
                step_context = context(Path(directory), "b" * 64)
                url = f"http://127.0.0.1:{server.server_port}/events"
                first = adapter.request(step_context, url, {"value": 7})
                replay = adapter.request(step_context, url, {"value": 7})
                self.assertEqual(first["response"]["key"], "b" * 64)
                self.assertTrue(replay["replayed"])
                self.assertEqual(_Handler.side_effect_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_adapter_prevents_idempotency_key_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "controlled by the runtime"):
                HttpJsonAdapter().request(
                    context(Path(directory)),
                    "http://127.0.0.1/example",
                    {},
                    headers={"Idempotency-Key": "caller-value"},
                )

    def test_http_error_receipt_never_becomes_success(self) -> None:
        class ErrorHandler(BaseHTTPRequestHandler):
            request_count = 0

            def do_POST(self) -> None:
                type(self).request_count += 1
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"unavailable"}')

            def log_message(self, format: str, *args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                adapter = HttpJsonAdapter()
                step_context = context(Path(directory), "e" * 64)
                url = f"http://127.0.0.1:{server.server_port}/events"
                with self.assertRaisesRegex(RuntimeError, "status 503"):
                    adapter.request(step_context, url, {"value": 9})
                with self.assertRaisesRegex(RuntimeError, "status 503"):
                    adapter.request(step_context, url, {"value": 9})
                self.assertEqual(ErrorHandler.request_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_command_adapter_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            output = home / "command-count.txt"
            script = (
                "from pathlib import Path; "
                f"p=Path({str(output)!r}); "
                "p.write_text((p.read_text() if p.exists() else '')+'x\\n')"
            )
            adapter = CommandAdapter()
            step_context = context(home, "c" * 64)
            first = adapter.run(step_context, [sys.executable, "-c", script])
            replay = adapter.run(step_context, [sys.executable, "-c", script])
            self.assertFalse(first["replayed"])
            self.assertTrue(replay["replayed"])
            self.assertEqual(output.read_text(encoding="utf-8"), "x\n")

    def test_uncertain_command_is_not_automatically_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            adapter = CommandAdapter()
            step_context = context(home, "d" * 64)
            command = [sys.executable, "-c", "print('unknown')"]
            with patch("agent_continuity.adapters.subprocess.run", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    adapter.run(step_context, command)
            with self.assertRaises(UncertainCommandOutcome):
                adapter.run(step_context, command)


if __name__ == "__main__":
    unittest.main()
