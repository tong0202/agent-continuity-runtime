from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_continuity.spec import validate_spec


def trial_spec() -> dict:
    return {
        "schema": "agent-continuity.workflow.v1",
        "workflow_id": "distribution-test",
        "steps": [
            {
                "id": "command",
                "type": "command",
                "argv": ["$python", "-c", "print('ok')"],
            },
            {
                "id": "file",
                "type": "file",
                "target": "output.txt",
                "text": "ok\n",
            },
        ],
    }


class DistributionTests(unittest.TestCase):
    def test_validate_spec_reports_steps_and_authorizations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "trial.json"
            spec_path.write_text(json.dumps(trial_spec()), encoding="utf-8")
            result = validate_spec(spec_path)
            self.assertTrue(result["valid"])
            self.assertEqual(result["workflow_id"], "distribution-test")
            self.assertEqual(result["required_authorizations"], ["allow-command"])
            self.assertEqual([step["type"] for step in result["steps"]], ["command", "file"])

    def test_validate_cli_has_no_execution_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "trial.json"
            spec_path.write_text(json.dumps(trial_spec()), encoding="utf-8")
            project_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_continuity",
                    "validate-spec",
                    "--spec",
                    str(spec_path),
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["valid"])
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["trial.json"])

    def test_version_command_is_available(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "src")
        completed = subprocess.run(
            [sys.executable, "-m", "agent_continuity", "--version"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertRegex(completed.stdout.strip(), r"^agent-continuity \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
