# Agent 结果评价调研

本笔记整理公开 benchmark 的一手实现/文档，回答一个问题：Agent 改完代码后，别人如何判断结果好不好。

> 调研日期：2026-09-12。本文引用的链接均指向评测项目的官方仓库、数据集说明或实现文件。

## 结论先行

成熟方案通常不是让 Agent 自己打分，也不是只比较最终文本，而是：

```text
固定任务环境
  -> Agent 执行并产生文件/状态变化
  -> 独立 evaluator 运行隐藏测试或环境检查
  -> 记录 pass/fail、timeout、infra error 等状态
  -> 在任务集合上聚合成 pass rate / resolution rate
```

LLM judge 只适合“结果有多种合理表达、无法完全程序化验证”的部分；代码是否能运行、测试是否通过、数据库状态是否正确，应优先使用确定性 evaluator。

## 先用大白话回答三个问题

### 1. 测试是提前写好的吗？

是。评测作者在运行 Agent **之前**，先把任务、初始代码、公开测试（可选）和隐藏 grader 固定在 case 里。Agent 通常只能看到任务说明和工作区，不能看到或修改隐藏测试。

一个合格的 case 还要先做两次验证：

1. 在原始 fixture 上运行，问题测试确实失败（否则无法证明 Agent 修好了问题）。
2. 在参考修复上运行，问题测试和回归测试都通过（否则评分规则本身不可靠）。

这也是 SWE-bench 把测试拆成 `FAIL_TO_PASS` 和 `PASS_TO_PASS` 的原因：前者验证目标问题被修复，后者防止修复带来回归。

### 2. grader 是另一个 Agent 吗？

不是。当前设计里的 grader 是一个**独立、普通、确定性的程序或测试进程**：Agent 退出后，Runner 把它放在最终 workspace 上运行，读取文件、执行测试并输出结果。它不读取 Agent 的“我已经完成了”文本，也不会自行修改代码或调用模型。

只有在“代码能否运行”之外仍存在开放式质量（例如文案是否自然、回答是否符合风格）时，才考虑增加 LLM judge；LLM judge 必须是单独的 scorer，不能替代硬测试，也不能和硬测试结果混为一谈。

### 3. 分数是按测试用例数量简单相加吗？

不是。测试数量只是某一组检查里的通过比例；每组检查的**权重和通过条件由 case 作者预先声明**。例如：

```text
FAIL_TO_PASS  通过 3/4 -> 3/4 × 60 = 45 分
PASS_TO_PASS  通过 5/5 -> 5/5 × 25 = 25 分
build/type    通过 1/1 -> 1/1 × 15 = 15 分
task_score = 45 + 25 + 15 = 85 分
```

集合级别再计算 `resolution_rate`、平均分、超时率等指标。`timeout`、Agent 崩溃和评测基础设施故障要单独记状态，不能悄悄当成“模型答错”。

## 对应到本项目：一次运行到底发生什么

```text
评测作者预先准备 case
  ├─ fixture/                 初始项目（Agent 可见）
  ├─ prompt.md                任务说明（Agent 可见）
  └─ grader/check.py          隐藏验收脚本（Agent 不可见）
           │
           ▼
Runner 复制 fixture 到临时 workspace，启动 Claude/Codex/OpenCode/Gemini
           │
           ▼
Agent 读文件、改代码、运行它自己的命令；Runner 记录日志和事件
           │
           ▼
Agent 退出后，Runner 独立启动 grader，检查最终 workspace
           │
           ▼
保存 grader.json、run.json、patch.diff，并聚合成矩阵结果
```

当前仓库的例子是 [`cases/ts-bug-001`](../../cases/ts-bug-001)：

- Agent 得到 `prompt.md` 和 `fixture/src/value.ts`。
- `grader/check.py` 在 Agent 结束后运行，检查最终文件是否保留 `0` 并且没有修改测试。
- 这是一个**最小二值 MVP**：grader 退出码为 `0` 时记 `100` 分，非 `0` 时记 `0` 分。
- 该脚本目前用字符串断言验证占位行为，还没有真正启动 TypeScript 测试框架；正式案例应改成可重复运行的公开/隐藏测试命令，并输出结构化 checks。

本地运行示例：

```powershell
python -m bench.cli run `
  --case cases/ts-bug-001 `
  --target codex-gpt `
  --targets-file targets.example.yaml
```

这条命令的重点不是 Agent 最后说了什么，而是它留下的 workspace 能否通过独立 grader。

### grader 的边界

grader 的工作目录是本次运行创建的临时 workspace，而不是评测仓库根目录。它可以读取 Agent 的改动和 fixture 中的依赖，但不应读取其他 run，也不应访问包含答案的 reference patch。Runner 将 grader 作为独立子进程启动，并分别保存 stdout、stderr、退出码和超时信息。

因此一次运行至少有两类结果：

- **正确性结果**：测试/状态检查通过了多少，换算成 `score`。
- **运行状态**：Agent 是否超时、崩溃，grader 或镜像是否出错。

两者要同时记录。例如 Agent 修改正确但 grader 所需依赖下载失败，应记为 `infra_error`，不能把它当成模型能力为 0 分。

## 参考方案

### SWE-bench：补丁 + 回归测试

SWE-bench 的 harness 在 Docker 中创建任务环境，把模型 patch 复制进去并尝试 `git apply`，然后运行任务的 evaluation script。它从测试日志解析各测试的状态，并生成 resolution report，而不是相信模型声称“已修复”。

它把测试分为至少两类：

- `FAIL_TO_PASS`：原来失败、修复后必须通过的问题测试。
- `PASS_TO_PASS`：原来已经通过、修复后不能被破坏的回归测试。

最终报告除了 `resolved_instances` / `unresolved_instances`，还单独统计 empty patch、infra failure、ambiguous failure、error 等类别。主指标是 resolution rate。

来源：

- [SWE-bench grading harness](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py)
- [SWE-bench reporting](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/reporting.py)
- [SWE-bench FAQ: metrics](https://github.com/SWE-bench/SWE-bench/blob/main/docs/faq.md)

### 横向对比

| 评测 | 主要验收对象 | 常见结果/指标 | 对本项目的启示 |
| --- | --- | --- | --- |
| SWE-bench | 代码补丁和测试结果 | resolution rate；另记 empty patch、infra/error | 隐藏测试 + `FAIL_TO_PASS`/`PASS_TO_PASS` |
| Terminal-Bench | 终端任务完成后的环境 | `PASS` / `FAIL` / `TIMEOUT` / `ERROR` | Agent 阶段与测试阶段分离，状态不能混淆 |
| tau-bench | 数据库/业务环境状态与沟通约束 | 由多个 reward 组件组合 | 工作流任务应检查真实状态，不只看文本 |
| AgentBench | 各类环境中的任务成功条件 | task-specific accuracy/success rate | 通用 Runner 统一生命周期，具体成功条件交给 case |
| OpenAI Evals / Inspect AI | scorer 产出的原子结果 | accuracy、均值、重复运行统计等 | scorer 与 metric 分离，可同时有硬测和 LLM judge |

### Terminal-Bench：任务脚本 + oracle + 状态分类

Terminal-Bench 的单个 task 包含 instruction、测试脚本和 reference/oracle solution。评测器先启动隔离容器和 Agent，再执行测试脚本，结果分类为 `PASS`、`FAIL`、`TIMEOUT`、`ERROR`。

这里的重点是：测试脚本是任务的一部分，Agent 阶段和测试阶段分开，容器在最后清理；资源隔离也属于评测定义的一部分。

来源：

- [Terminal-Bench running guide](https://harborframework.com/docs/running-tbench)
- [Terminal-Bench task contribution guide](https://github.com/harbor-framework/terminal-bench/blob/main/CONTRIBUTING.md)
- [Terminal-Bench task structure](https://github.com/harbor-framework/terminal-bench/blob/main/README.md)

### tau-bench：环境状态奖励，不只看文本

tau-bench 为每个任务定义 evaluation criteria，例如必须执行的 action、必须沟通的信息和自然语言约束。最终 reward 可以由多个组件组合，文档中的例子是：数据库状态 reward 乘以沟通 reward。

这适合客服、工作流和工具调用任务：结果是否正确由环境状态决定，轨迹只作为解释和辅助指标。

来源：

- [tau2 evaluation criteria](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)
- [tau2 trajectory evaluation](https://github.com/sierra-research/tau2-bench/blob/main/docs/cli-reference.md)

### AgentBench：任务类型各自定义成功条件

AgentBench 覆盖操作系统、数据库、知识图谱、网页购物等环境。它让 Agent 在容器化环境中通过工具行动，每个 task 自己实现 `start_sample` 和 `calculate_overall`，最终聚合成 accuracy、success rate 等指标。

这说明通用框架只应统一生命周期和结果格式，不能假设所有任务都用同一种 grader。

来源：

- [AgentBench introduction](https://github.com/THUDM/AgentBench/blob/main/docs/Introduction_en.md)
- [AgentBench extension interface](https://github.com/THUDM/AgentBench/blob/main/docs/Extension_en.md)

### OpenAI Evals / Inspect AI：scorer 与 metric 分离

OpenAI Evals 既支持确定性 Match/FuzzyMatch，也支持 model-graded classification。Inspect AI 将 solver（执行过程）和 scorer（评分器）分开，一个 task 可以配置多个 scorer 和多个 metric，并支持 epochs 重复运行。

适合我们的启示：单个 scorer 返回原子结果，聚合层再计算平均分、准确率、标准误和重复运行统计；不要把所有逻辑塞进一个最终分数。

来源：

- [OpenAI Evals custom eval](https://github.com/openai/evals/blob/main/docs/custom-eval.md)
- [OpenAI Evals model-graded eval](https://github.com/openai/evals/blob/main/docs/eval-templates.md)
- [Inspect AI scorers and metrics](https://github.com/ukgovernmentbeis/inspect_ai/blob/main/docs/_builtin-scorers.md)

### OpenHands / SWE-smith：可复现环境和任务生成验证

OpenHands 的 evaluation harness 使用固定 runtime、镜像和 timeout，并把 dataset preparation、agent run、输出文件和多 worker 聚合分开。SWE-smith 在生成任务后还先运行 validation，过滤掉不能稳定复现的任务，再用于训练或评测。

启示是：一个案例先要证明“基线确实失败、参考修复确实通过、测试稳定”，否则模型分数没有解释力。

来源：

- [OpenHands benchmark repository](https://github.com/OpenHands/benchmarks)
- [OpenHands evaluation harness guide](https://docs.openhands.dev/openhands/usage/developers/evaluation-harness)
- [SWE-smith validation](https://github.com/SWE-bench/SWE-smith/blob/main/docs/guides/train_swe_agent.md)

## 对本项目的建议

### v1：代码修复任务用硬测试

每个 case 建议定义：

```yaml
grader:
  command: [python, grader/check.py]
  checks:
    - id: fail_to_pass
      weight: 60
    - id: pass_to_pass
      weight: 25
    - id: build
      weight: 15
```

grader 输出结构化结果，而不是只返回退出码：

```json
{
  "status": "passed",
  "score": 100,
  "checks": {
    "fail_to_pass": {"passed": 3, "total": 3, "score": 60},
    "pass_to_pass": {"passed": 5, "total": 5, "score": 25},
    "build": {"passed": 1, "total": 1, "score": 15}
  },
  "regressions": [],
  "duration_ms": 812
}
```

当前仓库的 `0/100` 二值 grader 只能作为 MVP 占位，后续应升级为上面的结构化输出。

### 状态和分数分开

建议区分：

| 状态 | 含义 | 是否算任务失败 |
| --- | --- | --- |
| `passed` | Agent 正常结束且所有硬检查通过 | 否 |
| `grader_failed` | Agent 正常结束，但功能或回归检查失败 | 是 |
| `timeout` | Agent 或 grader 超时 | 单独统计，也可在任务分数记 0 |
| `agent_error` | Agent 非正常结束 | 单独统计，也可在任务分数记 0 |
| `patch_invalid` | 改动无法应用或 workspace 不完整 | 单独统计 |
| `infra_error` | 镜像、依赖、测试基础设施出错 | 不应和模型能力失败混为一谈 |

SWE-bench 也会单独记录 infra failure、empty patch 和 error；排行榜至少要同时展示成功率和这些失败计数。

### 集合级指标

单案例：

```text
task_score = sum(通过的 check 权重)
```

整个评测集：

```text
resolution_rate = passed_tasks / valid_submitted_tasks
mean_score      = average(task_score)
timeout_rate    = timeout_tasks / all_tasks
infra_rate      = infra_error_tasks / all_tasks
```

对于 agent system，还要同时保存 duration、tool_calls、usage、cost_usd；这些是效率指标，不应偷偷混进正确性分数。

### 纯模型与 Agent 产品要分榜

不同 CLI 的结果不能直接叫“模型分数”，因为 Harness、工具和权限都不同。建议展示两张榜：

1. `agent_system_score`：Claude Code/Codex/OpenCode/Gemini 的整体结果。
2. `model_score`：同一统一 Harness 下只替换模型 API 的结果。

## 落到当前实现的下一步

1. 保留现有独立 grader 和 `grader.json`。
2. 把 grader 返回值从二值退出码扩展为结构化 checks。
3. 增加 `FAIL_TO_PASS`、`PASS_TO_PASS` 两类测试声明。
4. 把 `timeout`、`agent_error`、`infra_error` 从 `passed/failed` 中单独统计。
5. matrix 汇总 `resolution_rate`、平均分、耗时、成本和失败原因。
6. 只有开放式质量项才接 LLM judge，并保存 judge rubric、原始理由和 judge 版本。

