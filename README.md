# Agent Continuity Runtime

[中文说明](README.zh-CN.md)

Agent Continuity Runtime is a small, dependency-free Python runtime for crash-resumable agent workflows. It persists workflow state in SQLite, gives each step a stable idempotency key, and records state changes in a tamper-evident HMAC hash chain.

It is an execution reliability layer, not an agent skill, model, or autonomous agent framework.

## What it provides

- Resume an incomplete workflow after a process interruption.
- Reuse the same idempotency key when a step is retried.
- Detect changes to persisted audit events.
- Run file, HTTP JSON, and shell-free command adapters.
- Define workflows in JSON without writing integration code.
- Require explicit authorization for command and HTTP steps.
- Return machine-readable JSON from the CLI.

## Requirements

- Python 3.11 or newer
- Windows, Linux, or macOS

## Install from source

```bash
git clone https://github.com/tong0202/agent-continuity-runtime.git
cd agent-continuity-runtime
python -m venv .venv
```

Activate the virtual environment, then install the project:

```bash
python -m pip install -e .
agent-continuity --version
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. On Linux or macOS, use `source .venv/bin/activate`.

## Quick start

Inspect a workflow without executing it:

```bash
agent-continuity validate-spec --spec workflows/quickstart.json
```

Run it. The included example contains a local command, so authorization is explicit:

```bash
agent-continuity run-spec --spec workflows/quickstart.json --home .runtime/quickstart --allow-command
```

Run the same command again. Completed steps are read from durable state rather than executed twice.

Inspect status:

```bash
agent-continuity spec-status --spec workflows/quickstart.json --home .runtime/quickstart
```

## Crash recovery demo

The first command intentionally exits with code `23` after an idempotent side effect has completed:

```bash
agent-continuity demo --home demo_state --crash-once
agent-continuity status --home demo_state
agent-continuity demo --home demo_state
agent-continuity verify --home demo_state
```

The second run resumes the interrupted step. The final result contains one receipt for each of the three side effects.

## Workflow format

```json
{
  "schema": "agent-continuity.workflow.v1",
  "workflow_id": "hello-continuity",
  "steps": [
    {
      "id": "write-result",
      "type": "file",
      "target": "../outputs/hello.txt",
      "text": "completed\n"
    }
  ]
}
```

Supported step types are `file`, `http_json`, and `command`. See [Workflow specification](docs/workflow-spec.md) for fields and authorization rules.

## Safety and reliability boundaries

- This project does not promise universal exactly-once delivery. HTTP receivers must persist and deduplicate the `Idempotency-Key` header.
- A command that started but did not commit a result is marked uncertain and requires manual reconciliation. It is not automatically repeated.
- The HMAC audit chain detects modification when its local key remains secret. It is not an external timestamp, transparency log, or distributed consensus system.
- Version `0.4.0` is a single-host, single-writer runtime.
- Workflow files are executable configuration. Review them before granting `--allow-command` or `--allow-http`.

## Development

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
