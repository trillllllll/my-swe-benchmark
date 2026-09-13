# Agent Adapter Hub

一个用于比较 Claude Code、Codex CLI、OpenCode、Gemini CLI，以及官方 Agent SDK 的本地评测 runner。

首版比较的是完整的 **agent system**：模型、Harness、工具策略和默认提示词的组合。Runner 不改写这些 agent 的内部循环，只准备工作区、启动目标、记录过程，并用独立 grader 检查最终文件和测试结果。

## 两种接入方式

| 接入方式 | Runner 做什么 | 目标名 |
| --- | --- | --- |
| CLI subprocess | 启动 `claude`、`codex`、`opencode` 或 `gemini` 子进程，设置 cwd 和权限，实时读取 stdout/stderr | `claude-code`、`codex`、`opencode`、`gemini` |
| Python SDK | 直接导入 `claude_agent_sdk` 或 `openai_codex`，创建 SDK session/thread，消费原生事件流 | `claude-agent-sdk`、`codex-sdk` |

因此，CLI target 需要 `executable`；SDK target 不需要手写命令，也不需要把 prompt 拼成一行 shell 命令。两种方式最后都进入同一个 Runner 和 grader 流程。

```text
case fixture -> 临时 workspace -> adapter -> agent
                                      |          |
                              归一化事件       文件改动
                                      v          v
                              events.jsonl   patch.diff
                                                 |
                                           独立 grader -> run.json
```

## 安装

安装基础 runner（包含 `bench tui`）及其依赖：

```powershell
python -m pip install -e .
```

要启用官方 SDK adapter，安装可选依赖：

```powershell
python -m pip install -e ".[sdk]"
```

SDK 是懒加载的；不安装 SDK 也可以继续运行四个 fake CLI target 和真实 CLI adapter。

## TUI：同时观察多个 CLI

安装项目后可以打开交互式运行器：

```powershell
python -m pip install -e .
bench tui --targets-file targets.example.yaml
```

TUI 会先扫描 `cases/**/case.yaml`，然后让你选择一个 case 和多个 CLI target。确认预览页后才会启动进程；每个 target 都会复制到独立的 `runs/<run-id>/workspace`，不会互相接力修改。

左侧栏可以切换主控总览或某个 CLI。主控页显示聚合状态和时间线，CLI 页显示事件、stdout 和 stderr。常用操作：`s` 停止当前项，`S` 停止全部，`r` 重跑当前项，`o` 打开 workspace，`d` 打开 diff，`f` 切换日志视图，`q` 退出。运行中的 `q` 会先要求确认。

TUI 默认保留 workspace，便于在运行结束后检查实际文件；主动停止的 run 会写入 `status=stopped`，grader 标记为 `not_run`，不会伪造评分。真实 target 建议使用 `targets.real.example.yaml`，预览确认后才会产生模型调用。

## 先做离线验证

`targets.example.yaml` 里的四个 CLI target 故意指向 `tests/fake_agent.py`，不会调用模型或消耗额度：

```powershell
python -m bench.cli --targets-file targets.example.yaml adapters
python -m bench.cli --targets-file targets.example.yaml targets
python -m bench.cli matrix `
  --case cases/ts-bug-001 `
  --targets claude-sonnet,codex-gpt,opencode-qwen,gemini-pro `
  --targets-file targets.example.yaml
```

`matrix` 是当前的多 Agent 入口：同一个 case 会被复制成多个相互隔离的 workspace，然后并行启动所选 target。每个 target 就是一套完整的 Agent system（Harness + 模型 + 工具/权限配置），不是多个 Agent 共用一个目录接力修改。这样不同模型的结果不会互相污染，也能同时看到各自的实时事件。

```text
一个 case
  ├─ claude-sonnet -> workspace A -> run.json
  ├─ codex-gpt     -> workspace B -> run.json
  ├─ opencode-qwen-> workspace C -> run.json
  └─ gemini-pro    -> workspace D -> run.json
```

终端事件会带 target 前缀，运行结束后还会打印汇总表，并在 `runs/` 下写入一个 `matrix-*.json` 报告：

```text
[codex-gpt][stdout] tool_call: write
[claude-sonnet][stdout] assistant_delta: ...
target          model       status   score   duration   tool_calls   run_dir
codex-gpt       gpt-5       passed   100     123ms      1            runs/...
claude-sonnet   sonnet      passed   100     140ms      1            runs/...
matrix_report   runs/matrix-a1b2c3d4e5.json
```

默认并发启动全部 target；机器或额度有限时可以限制并发数，或者保留 workspace 便于检查 Agent 改了什么：

```powershell
python -m bench.cli matrix `
  --case cases/ts-bug-001 `
  --targets claude-sonnet,codex-gpt `
  --max-concurrency 1 `
  --keep-workspace `
  --targets-file targets.example.yaml
```

这里先解决“一个任务跑多个 Agent/模型”的编排问题。多个 Agent 在同一个 workspace 上协作（例如先让一个 Agent 分析，再让另一个 Agent 实现）是另一种 pipeline 模式，后续再单独设计，不能和横向比较混在一起。

运行时事件会边到达边显示在当前终端，例如：

```text
[stdout] session.started: session.started
[stdout] tool_call: write
[stdout] tool_result: tool_result
[stdout] turn.completed: turn.completed
codex-gpt  passed  100  123ms  ...
```

同一批事件同时追加到各自 run 目录的 `events.jsonl`，所以终端适合实时观察，文件适合后续分析或网页消费。需要脚本消费时，可加 `--json` 让 `matrix` 在汇总表后输出同一份 JSON 报告；`--no-live` 可关闭实时事件。

## Suite：多个 case × 多个 target

`matrix` 目前只覆盖“一个 case × 多个 target”。当案例库扩大后，suite 会把一组预先编写好的 case 与一组 target 做笛卡尔积，逐个运行并汇总结果：

```text
cases/                         targets
  ts-bug-001 ─┬─ claude-sonnet   -> run/<case>__<target>__<id>/
  py-bug-001  ├─ codex-gpt       -> run/<case>__<target>__<id>/
  api-task-003└─ opencode-qwen   -> ...
```

每个 `(case, target)` 组合都会得到自己的 workspace、grader、`run.json` 和 `patch.diff`；组合之间不会共享工作目录，也不会因为某一个 target 超时而取消其他组合。`max-concurrency` 是全局并发上限，因此可以控制机器资源和 API 额度。结果应按 case 声明顺序、再按 target 声明顺序输出，便于在网页或 CI 中稳定比较。

### 预期用法

命令行入口已经可用：

```powershell
# 运行一个 suite 清单中的全部 case × target
python -m bench.cli suite `
  --cases cases/smoke.yaml `
  --targets claude-sonnet,codex-gpt,opencode-qwen `
  --max-concurrency 4 `
  --targets-file targets.example.yaml
```

suite 清单只负责选择 case，不改变 case 自己的 `case.yaml`、prompt 或 grader。例如：

```yaml
# cases/smoke.yaml
name: smoke
version: 1
cases:
  - cases/ts-bug-001
  - cases/py-bug-001
```

Python 调用层也可以直接使用：

```python
from pathlib import Path
from bench.runner import execute_suite

report = execute_suite(
    cases=["cases/ts-bug-001", "cases/py-bug-001"],
    targets=[targets["claude-sonnet"], targets["codex-gpt"]],
    runs_root=Path("runs"),
    max_concurrency=4,
)
```

suite 报告会保留每个单次 run 的原始路径和状态，并提供当前 MVP 的总数、通过数、失败数、编排错误数和超时数。后续加入正式评分后，再在聚合层计算 `resolution_rate`、平均分、timeout/agent-error/infra-error 比例，以及耗时、tool calls、usage/cost 等指标；基础设施错误会单独列出，不伪装成模型失败。

suite 运行结束会打印 `case × target` 表格，并在 `runs/`（或 `--runs-dir` 指定目录）写入 `suite-*.json`。实时事件前缀包含两级标识，例如 `[ts-bug-001/codex-gpt][stdout]`。当前 `cases/smoke.yaml` 包含两个离线 fake case，适合先验证编排链路；把 `targets.example.yaml` 换成真实 target 配置后即可接入本机 CLI/SDK。

## 接入真实 CLI

真实 CLI 的 adapter 已经实现。可以直接复制 [`targets.real.example.yaml`](C:/Users/13431/Documents/ChatGPT/my-swe-benchmark/targets.real.example.yaml)；真实 target 不要保留 fake 配置里的 `command_prefix: [python]`：

```yaml
targets:
  codex-real:
    adapter: codex
    executable: codex
    model: gpt-5
    sandbox: workspace-write
    approval_policy: never
```

Runner 会由对应 adapter 生成供应商命令。例如当前 Codex adapter 会生成类似下面的命令（审批参数放在 `exec` 前，适配当前 Codex CLI）：

```text
codex --ask-for-approval never exec --model gpt-5 --sandbox workspace-write --json --ephemeral --cd WORKSPACE -
```

其他 CLI 也是同样模式：`adapter` 决定参数布局，`model`、权限和工作目录由 Runner 的 `RunSpec` 传入。Windows 上可以先检查命令是否在 PATH 中：

```powershell
where.exe claude
where.exe codex
where.exe opencode
where.exe gemini
```

这里的 `where.exe` 是 Windows 自带的查找命令，不是本项目生成的文件。

当前机器已发现四个命令（版本检查不会调用模型）：Claude Code 2.1.138、Codex CLI 0.130.0、OpenCode 1.2.21、Gemini CLI 0.32.1。`bench` 会用 Python 的进程启动器直接启动它们；Windows 下如果 PATH 中有多个安装，建议把 `executable` 写成你想使用的绝对路径。

注意：**“adapter 已接入”与“真实模型 smoke 已执行”是两件事。** 目前已验证命令发现、版本/帮助参数、工作目录和进程管理，以及 fake target 的完整链路；真实模型运行还需要登录/凭据，并会产生模型调用费用。准备好后可先只跑一个 target：

```powershell
python -m bench.cli run `
  --case cases/ts-bug-001 `
  --target codex-gpt-real `
  --targets-file targets.real.example.yaml `
  --timeout 300 `
  --keep-workspace
```

## 直接接入官方 SDK

示例配置已经包含两个 SDK target：

```yaml
  codex-sdk-gpt:
    adapter: codex-sdk
    model: gpt-5.6-terra
    sandbox: workspace-write
    approval_policy: never
    pass_env: [OPENAI_API_KEY, CODEX_API_KEY]

  claude-agent-sonnet:
    adapter: claude-agent-sdk
    model: claude-sonnet-4-6
    permission_mode: acceptEdits
    pass_env: [ANTHROPIC_API_KEY]
```

adapter 内部的核心调用等价于：

```python
# Codex SDK：创建 thread，再实时消费 turn 事件
with Codex() as codex:
    thread = codex.thread_start(cwd=workspace, model=model, sandbox=sandbox)
    for event in thread.turn(prompt).stream():
        emit_event(event)

# Claude Agent SDK：query 本身返回异步消息流
async for message in query(
    prompt=prompt,
    options=ClaudeAgentOptions(cwd=workspace, model=model, permission_mode=permission_mode),
):
    emit_event(message)
```

做一次可选的真实 smoke（任选一个 target）：

```powershell
python -m bench.cli run `
  --case cases/ts-bug-001 `
  --target codex-sdk-gpt `
  --targets-file targets.example.yaml `
  --timeout 300

python -m bench.cli run `
  --case cases/ts-bug-001 `
  --target claude-agent-sonnet `
  --targets-file targets.example.yaml `
  --timeout 300
```

这会在 Python 进程里调用 SDK：Codex 使用 `openai-codex` 创建临时 thread，Claude 使用 `claude-agent-sdk` 的 `query()` 读取消息流。SDK 原生对象会被转换成可保存的 JSON；无法统一的字段仍保留在 `raw`/`data.payload` 中。

SDK target 可能产生真实模型费用。第一次运行前确认本机已经登录对应 CLI，或在当前 shell 设置 API key。Runner 默认过滤敏感环境变量，只有 `pass_env` 列出的变量才会传给 agent；不存在的变量会被忽略。

## 配置字段

- `adapter`：选择 Harness/接入实现。
- `executable`：CLI 可执行文件；SDK target 通常留空。
- `model`：传给目标的模型名。
- `mode`：CLI 输出模式，例如 `stream-json`。
- `permission_mode`：Claude/Gemini 的权限模式。
- `sandbox`：Codex 文件系统边界，例如 `read-only`、`workspace-write`。
- `approval_policy`：Codex 审批策略，例如 `never`。
- `pass_env`：允许传入 agent 的凭据环境变量白名单。

工作目录不写在 YAML 里：每次运行都会把 case 的 `fixture` 复制到 `runs/<run-id>/workspace`，并把它作为 CLI cwd 或 SDK 的 `cwd`。任务 prompt 来自 case 的 `instruction` 文件。

## 运行产物

每次运行写入 `runs/<run-id>/`：

```text
meta.json            target、model、transport、版本和环境指纹
prompt.txt           注入给 agent 的任务
stdout.log           CLI 标准输出（SDK 通常为空）
stderr.log           CLI 标准错误（SDK 通常为空）
events.jsonl         实时归一化事件和原始 payload
patch.diff           fixture 基线到最终工作区的 diff
grader.*.log         独立 grader 输出
grader.json          结构化 grader 状态和评分
run.json             最终状态、耗时、事件数和评分
```

grader 不相信 agent 的最终文本或“已完成”声明，只检查工作区状态。`usage`、`cost_usd`、`tool_calls` 如果目标没有提供可靠数据，会保留为 `unknown` 或空值，不会伪造为零。

当前结果标记为 `local-unisolated`。正式评测应在固定 Docker 镜像中运行，并记录 `agent_system`、`harness_version`、`model`、`cli_version`、`case_version` 和 `environment_fingerprint`；这不会改变 adapter 接口。

## 常见问题

`AdapterUnavailableError`：执行 `python -m pip install -e ".[sdk]"`，确认当前 Python 环境和运行 `bench` 的环境一致。

CLI 找不到：用上面的 `where.exe` 检查 PATH，或把 `executable` 改成绝对路径。

运行超时：`--timeout` 分别限制 agent 和 grader；CLI 会终止进程树，SDK 会尝试中断当前 turn，并把状态记录为 `timeout`。

想比较纯模型：不要直接把不同 CLI 的分数叫作模型分数。后续需要同一个统一 Harness，只替换模型 API；本项目首阶段先比较完整 agent system。
