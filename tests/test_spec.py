from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_continuity.spec import load_spec, run_spec


def write_spec(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def base_spec() -> dict:
    return {
        "schema": "agent-continuity.workflow.v1",
        "workflow_id": "spec-test-workflow",
        "steps": [
            {
                "id": "run-command",
                "type": "command",
                "argv": ["$python", "-c", "print('spec-command-ok')"],
            },
            {
                "id": "write-file",
                "type": "file",
                "target": "result.txt",
                "text": "spec-file-ok\n",
            },
        ],
    }


class WorkflowSpecTests(unittest.TestCase):
    def test_json_spec_runs_without_python_integration_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "workflow.json"
            write_spec(spec_path, base_spec())
            result = run_spec(spec_path, root / "state", allow_command=True)
            self.assertEqual(result["status"]["status"], "COMPLETED")
            self.assertTrue(result["audit"]["valid"])
            self.assertEqual((root / "result.txt").read_text(), "spec-file-ok\n")
            self.assertIn(
                "spec-command-ok",
                result["status"]["steps"][0]["result"]["stdout"],
            )

    def test_same_step_ids_with_changed_configuration_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "workflow.json"
            state = root / "state"
            value = base_spec()
            write_spec(spec_path, value)
            run_spec(spec_path, state, allow_command=True)
            value["steps"][1]["text"] = "changed\n"
            write_spec(spec_path, value)
            with self.assertRaisesRegex(ValueError, "definition changed"):
                run_spec(spec_path, state, allow_command=True)

    def test_command_steps_require_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "workflow.json"
            write_spec(spec_path, base_spec())
            with self.assertRaisesRegex(PermissionError, "explicit authorization"):
                run_spec(spec_path, root / "state")

    def test_cli_permission_error_is_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "workflow.json"
            write_spec(spec_path, base_spec())
            project_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_continuity",
                    "run-spec",
                    "--spec",
                    str(spec_path),
                    "--home",
                    str(root / "state"),
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            error = json.loads(completed.stdout)
            self.assertEqual(error["status"], "ERROR")
            self.assertEqual(error["error_type"], "PermissionError")
            self.assertEqual(completed.stderr, "")

    def test_duplicate_step_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory) / "workflow.json"
            value = base_spec()
            value["steps"][1]["id"] = value["steps"][0]["id"]
            write_spec(spec_path, value)
            with self.assertRaisesRegex(ValueError, "must be unique"):
                load_spec(spec_path)

    def test_unsupported_step_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory) / "workflow.json"
            value = base_spec()
            value["steps"][0]["type"] = "arbitrary-python"
            write_spec(spec_path, value)
            with self.assertRaisesRegex(ValueError, "unsupported step type"):
                load_spec(spec_path)


if __name__ == "__main__":
    unittest.main()
