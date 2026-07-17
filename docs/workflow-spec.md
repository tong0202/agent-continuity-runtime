# Workflow specification

The current schema identifier is `agent-continuity.workflow.v1`.

## Root fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema` | string | yes | Must equal `agent-continuity.workflow.v1`. |
| `workflow_id` | string | yes | Stable workflow identity. |
| `steps` | array | yes | Ordered, non-empty list of steps. |

Step IDs must be unique. Once a workflow has started in a runtime home, changing its definition is rejected.

## `file` step

Required fields: `id`, `type`, `target`, and exactly one of `text` or `json`.

The target is resolved relative to the specification file. Existing different content is treated as a conflict.

## `http_json` step

Required fields: `id`, `type`, `url`, and `payload`. Optional fields are `method`, `headers`, and `timeout`.

Execution requires `--allow-http`. The runtime controls the `Idempotency-Key` header; a workflow cannot override it.

## `command` step

Required fields: `id`, `type`, and `argv`. Optional fields are `cwd` and `timeout`. Use `$python` as the first argument to run the active Python interpreter.

Execution requires `--allow-command`. Commands use an argument list with `shell=False`. If the process is interrupted after a command starts and before its receipt is finalized, the runtime requires manual reconciliation.
