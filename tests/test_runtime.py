from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest

from agent_continuity.demo import DEMO_WORKFLOW_ID
from agent_continuity.runtime import WorkflowEngine


class ContinuityRuntimeTests(unittest.TestCase):
    def test_workflow_completes_and_audit_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(Path(directory))
            engine.start("workflow-1", ["one", "two"])
            seen: list[str] = []

            def handler(context):
                seen.append(context.idempotency_key)
                return {"step": context.step_name}

            status = engine.run("workflow-1", {"one": handler, "two": handler})
            self.assertEqual(status["status"], "COMPLETED")
            self.assertEqual(len(seen), 2)
            self.assertTrue(engine.ledger.verify().valid)

    def test_retry_reuses_the_same_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(Path(directory))
            engine.start("workflow-retry", ["unstable"])
            keys: list[str] = []

            def unstable(context):
                keys.append(context.idempotency_key)
                if len(keys) == 1:
                    raise RuntimeError("injected failure")
                return {"recovered": True}

            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                engine.run("workflow-retry", {"unstable": unstable})
            status = engine.run("workflow-retry", {"unstable": unstable})
            self.assertEqual(status["status"], "COMPLETED")
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(status["steps"][0]["attempts"], 2)

    def test_changed_definition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(Path(directory))
            engine.start("workflow-definition", ["one"])
            with self.assertRaisesRegex(ValueError, "definition changed"):
                engine.start("workflow-definition", ["one", "two"])

    def test_audit_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(Path(directory))
            engine.start("workflow-tamper", ["one"])
            with closing(sqlite3.connect(engine.database_path)) as connection:
                row = connection.execute(
                    "SELECT body_json FROM audit_events WHERE sequence = 1"
                ).fetchone()
                body = json.loads(row[0])
                body["payload"]["steps"] = ["changed"]
                connection.execute(
                    "UPDATE audit_events SET body_json = ? WHERE sequence = 1",
                    (json.dumps(body, sort_keys=True),),
                )
                connection.commit()
            verification = engine.ledger.verify()
            self.assertFalse(verification.valid)
            self.assertTrue(any("hash mismatch" in error for error in verification.errors))

    def test_process_crash_resumes_without_duplicate_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root / "src")
            command = [
                sys.executable,
                "-m",
                "agent_continuity",
                "demo",
                "--home",
                str(home),
            ]
            crashed = subprocess.run(
                [*command, "--crash-once"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 23)
            interrupted = WorkflowEngine(home).status(DEMO_WORKFLOW_ID)
            self.assertEqual(interrupted["steps"][1]["status"], "RUNNING")

            resumed = subprocess.run(
                command,
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            result = json.loads(resumed.stdout)
            self.assertEqual(result["status"]["status"], "COMPLETED")
            self.assertEqual(result["receipt_count"], 3)
            self.assertTrue(result["audit"]["valid"])
            self.assertTrue(all(item["executions"] == 1 for item in result["receipts"]))
            event_types = [event["event_type"] for event in WorkflowEngine(home).ledger.events()]
            self.assertIn("step_recovered", event_types)


if __name__ == "__main__":
    unittest.main()
