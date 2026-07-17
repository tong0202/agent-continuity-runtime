# 工作流规范

当前规范标识为 `agent-continuity.workflow.v1`。

## 根字段

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `schema` | 字符串 | 是 | 必须为 `agent-continuity.workflow.v1`。 |
| `workflow_id` | 字符串 | 是 | 稳定的工作流身份。 |
| `steps` | 数组 | 是 | 有序且非空的步骤列表。 |

步骤 ID 不能重复。工作流在某个运行目录中启动后，如果定义发生变化，运行时会拒绝继续执行。

## `file` 步骤

必填字段：`id`、`type`、`target`，并且 `text` 和 `json` 必须二选一。

目标路径相对于工作流规范文件解析。目标文件已存在且内容不同时，会报告冲突。

## `http_json` 步骤

必填字段：`id`、`type`、`url`、`payload`。可选字段：`method`、`headers`、`timeout`。

执行时必须提供 `--allow-http`。`Idempotency-Key` 请求头由运行时控制，工作流不能覆盖。

## `command` 步骤

必填字段：`id`、`type`、`argv`。可选字段：`cwd`、`timeout`。首个参数可使用 `$python` 指向当前 Python 解释器。

执行时必须提供 `--allow-command`。命令以参数列表和 `shell=False` 运行。如果命令启动后、最终回执写入前进程中断，必须人工核对结果。
