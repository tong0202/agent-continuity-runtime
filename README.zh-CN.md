# Agent Continuity Runtime

[English](README.md)

Agent Continuity Runtime 是一个轻量、无第三方运行时依赖的 Python Agent 可靠执行层。它把工作流状态持久化到 SQLite，为每个步骤生成稳定的幂等键，并用 HMAC 哈希链记录可检测篡改的审计事件。

它不是 Agent Skill，不是大模型，也不是自主 Agent 框架。它解决的是 Agent 执行到一半崩溃后，如何恢复、避免盲目重复副作用并留下可核验记录。

## 主要能力

- 进程中断后恢复未完成工作流。
- 同一步骤重试时复用稳定的幂等键。
- 检测持久化审计事件是否被修改。
- 提供文件、HTTP JSON 和无 shell 命令适配器。
- 用 JSON 定义工作流，不需要额外编写 Python 集成代码。
- 命令与 HTTP 步骤必须显式授权。
- CLI 输出结构化 JSON，便于 Agent 和自动化系统读取。

## 环境要求

- Python 3.11 或更高版本
- Windows、Linux 或 macOS

## 从源码安装

```powershell
git clone https://github.com/tong0202/agent-continuity-runtime.git
cd agent-continuity-runtime
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
agent-continuity --version
```

Linux 或 macOS 使用 `source .venv/bin/activate` 激活虚拟环境。

## 快速开始

先只读检查工作流，不执行任何步骤：

```powershell
agent-continuity validate-spec --spec .\workflows\quickstart.json
```

示例包含本地命令，因此运行时必须显式授权：

```powershell
agent-continuity run-spec `
  --spec .\workflows\quickstart.json `
  --home .\.runtime\quickstart `
  --allow-command
```

再次执行同一命令，已完成步骤会直接读取持久状态，不会重复执行。

查看状态：

```powershell
agent-continuity spec-status `
  --spec .\workflows\quickstart.json `
  --home .\.runtime\quickstart
```

## 崩溃恢复演示

第一条命令会在幂等副作用完成后故意以退出码 `23` 中断：

```powershell
agent-continuity demo --home .\demo_state --crash-once
agent-continuity status --home .\demo_state
agent-continuity demo --home .\demo_state
agent-continuity verify --home .\demo_state
```

第二次运行会恢复中断步骤，最终三个副作用各保留一份回执。

## 工作流格式

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

支持 `file`、`http_json` 和 `command` 三种步骤。字段和授权规则见[工作流规范](docs/workflow-spec.zh-CN.md)。

## 真实边界

- 不承诺任意外部系统的绝对 exactly-once。HTTP 接收端必须保存并识别 `Idempotency-Key`。
- 命令已经启动但结果尚未落盘时，运行时会标记结果不确定并要求人工核对，不会自动重跑。
- HMAC 审计链依赖本机密钥未泄露，只能检测本地记录修改；它不是外部时间戳、透明日志或分布式共识。
- `0.4.0` 是单机、单写者版本。
- 工作流文件属于可执行配置。授予 `--allow-command` 或 `--allow-http` 前必须先审查内容。

## 开发与测试

```powershell
python -m unittest discover -s tests -v
```

参与贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 开源许可证

本项目使用 [Apache License 2.0](LICENSE)。
