# Agent 评测结果评价：一手实现审计

> 调研日期：2026-09-12  
> 目的：回答“测试是不是提前写好？grader 是不是 Agent？分数怎么算？”并把结论落到 my-swe-benchmark。  
> 方法：只引用官方仓库中的文档、源码和 schema；链接固定到本次审计的 commit，避免 main 漂移。

## 先给结论

成熟的 Agent benchmark 通常采用下面的闭环：

~~~text
评测作者预先固定任务、初始环境和 verifier
  -> Agent 在隔离环境中执行，产生文件/数据库状态/轨迹
  -> 独立 verifier 读取实际结果并运行测试
  -> 记录“得分”和“运行状态”两条信息
  -> 在任务集合上聚合 resolution rate、平均分、Pass^k 等
~~~

这意味着：

1. **是，测试通常提前写好。** 评测作者在 Agent 启动前准备 instruction、fixture、公开或隐藏 tests/verifier，以及必要的 reference/oracle。Agent 只负责完成任务；隐藏 verifier 不应由 Agent 修改。
2. **grader 通常不是另一个 Agent。** 代码测试、构建、文件结构和数据库状态优先由普通确定性程序检查。只有开放式、难以程序化的质量项，才额外使用 LLM-as-a-judge；它是独立的测量器，不是被测 Agent 的自评。
3. **分数不是“测试数量简单相加”。** 每个 check 先产生 sample-level 结果，再按预先声明的规则（比例、均值、加权平均、乘积、阈值或 all-pass）聚合。超时、进程崩溃、测试器自身报错要保留为独立状态，不能悄悄当成能力为 0。

## 和本项目的对应关系

一次 bench run 应理解为两条并行结果：

~~~text
Agent 结果：completed / timeout / agent_error / process_error
Grader 结果：passed / failed / timeout / error
最终任务状态：由两者组合得到
~~~

当前仓库的 cases/ts-bug-001 是最小 MVP：Runner 复制 fixture 到本次 run 的临时 workspace，启动目标 CLI/SDK；Agent 结束后再启动 grader/check.py，退出码 0 记 100 分、非 0 记 0 分。这个二值规则只适合验证接入链路，不足以代表完整 benchmark。后续应让 grader 输出每个 check 的结构化结果，并把测试失败和基础设施失败分开。

当前实现的临时 workspace 位于 runs/<run_id>/workspace，不是项目根目录。这样每次运行都有干净初始状态，互不污染；运行目录仍保存 prompt.txt、日志、事件、patch.diff、grader.json 和 run.json。默认清理 workspace，但结果文件保留；调试时可用 --keep-workspace。

## 项目横向对比

| 项目 | 被测对象 | 预先定义的验收输入 | 单样本结果 | 集合级聚合 | LLM judge 角色 |
| --- | --- | --- | --- | --- | --- |
| SWE-bench | 代码 patch | FAIL_TO_PASS、PASS_TO_PASS、测试脚本和 Docker 环境 | patch 是否存在/应用成功；每个测试的 PASSED、FAILED、SKIPPED、ERROR 等 | F2P/P2P 比例；RESOLVED_FULL/PARTIAL/NO；resolution rate；另记 incomplete、empty patch、error、infra failure | 不是默认路径 |
| Terminal-Bench/Harbor | Agent 完成后的 workspace/环境 | task 的 instruction.md、tests/test.sh、环境和可选 oracle；verifier 可隐藏 | reward 数值或多维 reward；verifier/agent exception、cancelled 等另记 | Reward Kit 的 weighted mean/sum、all-pass、threshold 等；多 step 可 mean/final | 仅作为 judge criterion，非硬测试替代 |
| Inspect AI | solver 产生的 sample output/state | dataset 的 input/target、scorer、metric 和 epoch 配置 | Score(value, answer, explanation, reason, metadata)；也有 error/unscored | scorer 与 metric 分离；accuracy/mean/stderr/CI；epoch reducer | model-graded scorer 是独立 scorer，可多 grader 投票 |
| OpenAI Evals | completion function 或 solver | registry YAML + JSONL dataset + eval template/custom code | match/metrics/error event | eval run() 自己聚合，例如 accuracy 和 bootstrap std | model-graded YAML 将模型输出分类为预定义 choice |
| tau²/τ³-bench | 工具调用造成的业务环境状态和沟通 | task 的 evaluation_criteria、reference actions、环境 | DB hash、环境断言、沟通、动作和 NL assertion 各自 reward | reward_basis 中组件相乘；另有 Pass^k、cost | NL assertion 为实验性 judge；DB/沟通优先程序化 |
| AgentBench | 多种 task server 环境 | 每个 Task 自己的 sample 逻辑和结果计算 | SampleStatus 与 result 分离 | calculate_overall() 返回 task-specific dict | 由具体 task 决定，不是框架默认 |

## 1. SWE-bench：patch + 回归测试

SWE-bench harness 的流程是：在 Docker 中准备 repository 环境，应用模型 patch，运行 evaluation script，再从测试日志解析结果。它不相信模型最终文本中的“已完成”声明。

关键评分细节（固定 commit [02e7a74](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)）：

- FAIL_TO_PASS 测试要求原本失败的问题测试在 patch 后通过；PASS_TO_PASS 测试用于确认没有破坏原本通过的回归行为。源码对 PASSED 和 XFAIL 视为通过；F2P 的 SKIPPED 视为失败，而 P2P 的 SKIPPED 不算回归。
- compute_fail_to_pass() 和 compute_pass_to_pass() 都是“通过数 / 该组总数”；没有测试时当前实现返回 1（P2P 处有 TODO），因此不能擅自把空测试组解释成真实能力。
- get_resolution_status() 只有 F2P=1 且 P2P=1 才是 RESOLVED_FULL；F2P 在 0 和 1 之间且 P2P=1 是 RESOLVED_PARTIAL；其余是 RESOLVED_NO。
- 单实例报告还记录 patch 是否为 None、是否存在、是否成功应用、resolved 和 infra_failure，可选地保存每个测试的状态。
- 集合报告将 submitted、completed、incomplete、resolved、unresolved、empty_patch、error、likely infrastructure failure 和 ambiguous failure 分开统计。FAQ 定义 resolution rate 为成功解决实例数 / submitted instances。

来源：

- [grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py)
- [reporting.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/reporting.py)
- [run_evaluation.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)
- [FAQ: metrics and Docker](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/docs/faq.md)

**对本项目的启示：** 一个代码修复 case 至少要有“问题测试”和“回归测试”两组；case 发布前应验证 baseline 确实失败、reference fix 确实通过，并保留测试日志，而不是只检查 Agent 有没有输出 patch。

## 2. Terminal-Bench 与 Harbor：任务自带 verifier/reward

Terminal-Bench 当前运行方式已经转向 Harbor。官方 README 要求先用 oracle solution 重复运行（示例为 5 次），用来确认任务和 sandbox 稳定；oracle 是稳定性/可解性检查，不是评分器本身。

Harbor task 通常包含 instruction.md、环境定义和 tests/test.sh。Harbor 将 tests 复制到 verifier 环境，执行 test.sh，并要求 verifier 写入 /logs/verifier/reward.txt 或 /logs/verifier/reward.json。reward 可以是 0/1，也可以是连续数值或多个命名维度。

Reward Kit 支持两类 criteria：

- **programmatic criteria**：检查文件、命令、JSON/CSV、HTTP、图片或轨迹；criterion 返回 bool 或 float。
- **judge criteria**：用 LLM/Agent-as-a-Judge 处理代码质量等开放式维度。它应和程序化 correctness 分开，记录 judge 配置和理由。

同一 reward dimension 内的 criteria 默认 weighted mean，也可显式选择 weighted-sum、all-pass、any-pass、threshold 或 required-pass；子目录可以成为独立 reward dimension，并通过权重聚合。多 step task 还可以按 mean 或 final 合并各 step reward。

Harbor 的 verifier 结果解析会拒绝空文件、非法 JSON、非数字和非有限数值；缺少 reward 文件会报 verifier error。任务配置可以使用 verifier.environment_mode = "separate" 启动独立 verifier 容器，适合隐藏 grading code、不同依赖或不同 OS；文档明确说明 separate 模式不会把 tests 上传到 Agent image。

Terminal-Bench/Harbor 的任务设计规范还强调：任务必须 **verifiable、well specified、outcome-verified**，重复运行 verifier 应稳定；LLM judge 只在罕见且有证据的情况下使用。

来源：

- [Terminal-Bench README](https://github.com/harbor-framework/terminal-bench/blob/e2995b93b0a46edee7bc9942ea5622411a6d5bb9/README.md)
- [Terminal-Bench task template](https://github.com/harbor-framework/terminal-bench/blob/e2995b93b0a46edee7bc9942ea5622411a6d5bb9/docs/task-template.toml)
- [Harbor Reward Kit](https://github.com/harbor-framework/harbor/blob/d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621/docs/content/docs/rewardkit/index.mdx)
- [Harbor task schema](https://github.com/harbor-framework/harbor/blob/d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621/docs/content/docs/tasks/index.mdx)
- [Harbor task differences](https://github.com/harbor-framework/harbor/blob/d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621/docs/content/docs/tasks/task-difference.mdx)
- [Harbor verifier implementation](https://github.com/harbor-framework/harbor/blob/d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621/src/harbor/verifier/verifier.py)
- [Task proposal rubric](https://github.com/harbor-framework/terminal-bench/blob/e2995b93b0a46edee7bc9942ea5622411a6d5bb9/docs/prompts/task-proposal.md)

**对本项目的启示：** 将 grader 放在 case 的 verifier 目录并从 Agent workspace 隔离；先做确定性 checks，再为确实无法程序化的质量维度增加可复现的 judge。发布 case 前至少重复跑 reference/oracle，确认 verifier 不 flaky。

## 3. Inspect AI：Score、Metric 和运行错误分离

Inspect 把“对单个 sample 的判断”和“跨 sample 的统计”明确拆开：scorer 评价 solver 产出相对于 dataset target 的结果；metric 再聚合 scorer 产出的 scores。

重要语义（固定 commit [8ebe620](https://github.com/UKGovernmentBEIS/inspect_ai/tree/8ebe620d74c1eb679438db1b65324e30e2306092)）：

- Score 可包含 value、answer、explanation、reason 和 metadata。value 可以是 CORRECT/INCORRECT、数值或结构化值。
- 内置 metrics 包括 accuracy、mean、variance、std、stderr、bootstrap stderr、置信区间和 frequency。一个 Task 可以有多个 scorer；一个 scorer 也可以返回多个命名 score。
- 评分器若遇到真正执行/输入错误，应抛异常；Inspect 将其记录为 sample error，并由 retry_on_error、fail_on_error、score_on_error 等运行策略处理。
- 评分器预期要给 verdict 但无法给出时，应返回 Score.unscored()。其值是 NaN，被 metric 排除，但 reason/explanation 保留；这和“模型答案错误”不同。
- 内置 model-graded scorer 默认要求 grader 输出可解析的 GRADE: C/GRADE: I；无法解析时记 reason="grader_failed" 的 unscored，而不是自动把 grader 故障算作被测模型答错。多个 grader 可以用 majority reducer，并保存各投票。
- Eval log 顶层 status 是 started、success 或 error；results 是 metrics 聚合，samples 保留 input/output/target/score。eval set 支持失败重试、断点续跑和复用已完成 sample；success=False 表示重试后仍未完成，不等于所有答案都错。
- 默认 grader 模型可能有非零 temperature、没有固定 seed；边界判断可能波动。若使用 judge，应固定 temperature/seed（供应商支持时）并报告多 epoch 方差。

来源：

- [Scorers](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/scorers.qmd)
- [Metrics](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/metrics.qmd)
- [Scoring policy](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/scoring-policy.qmd)
- [Model grading](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/model-graded.qmd)
- [Eval logs](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/eval-logs.qmd)
- [Eval sets](https://github.com/UKGovernmentBEIS/inspect_ai/blob/8ebe620d74c1eb679438db1b65324e30e2306092/docs/eval-sets.qmd)

## 4. OpenAI Evals：JSONL/registry 预定义 case + scorer 聚合

OpenAI Evals 的基本模式是：registry 中的 YAML 指定 eval class/template、数据集路径和 metrics；JSONL 中每行是一个预先准备的 sample。oaieval MODEL EVAL 创建 completion function，逐 sample 调用，再把事件写入 JSONL log。

固定 commit [8eac7a7](https://github.com/openai/evals/tree/8eac7a7de5215c907fbddc30efdaf316913eccdd) 中可以看到：

- basic/Match 用 completion 是否以某个 ideal 答案开头判断；Includes 做包含判断；FuzzyMatch 做模糊匹配。它们都是确定性 evaluator，结果通过 record_match/record_metrics 写入 sample event。
- custom eval 通常实现 eval_sample（生成并检查单样本）和 run（调用 eval_all_samples 后聚合）。官方 arithmetic 示例的 run 返回 accuracy；Match 还返回 bootstrap accuracy std。
- Recorder 的事件类型包括 match、sampling、metrics、error 和 raw_sample；因此原始输出和错误可以和最终 metric 一起审计。
- model-graded template 把被测模型 completion 放进 evaluation prompt，要求 grader 输出预定义 choice；不可解析的 choice 归为 __invalid__，choice 可映射到数值 score。它适合开放式回答，但仍依赖预先写好的 rubric/prompt/choice。
- eval set 有线程超时、进度文件和断点续跑；日志默认写本地 JSONL。重跑时应保留 run id、模型、eval registry 版本和数据集版本。

来源：

- [Custom eval](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/custom-eval.md)
- [Eval templates](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)
- [Run evals](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/run-evals.md)
- [Match evaluator](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/evals/elsuite/basic/match.py)
- [Recorder](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/evals/record.py)
- [Metrics](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/evals/metrics.py)

## 5. tau²/τ³-bench：环境 outcome grader

当前 tau2-bench 文档把 task 的 evaluation_criteria 分成 actions、env_assertions、communicate_info、nl_assertions 和 reward_basis。关键点是：actions 通常只是一条 reference trajectory。Evaluator 在 fresh gold environment 中 replay 这些 actions 得到目标 DB 状态，再将 Agent 结束时的 DB hash 与目标比较；Agent 不必逐字重放这条路径。

组件语义如下：

| RewardType | 检查 | 默认是否影响最终 reward |
| --- | --- | --- |
| DB | 预测环境 DB hash 是否等于 gold hash | 是（默认 basis 的一部分） |
| ENV_ASSERTION | 预测环境上的断言 | 仅在 basis 中时 |
| COMMUNICATE | Agent 消息是否含要求字符串 | 是（默认 basis 的一部分） |
| NL_ASSERTION | LLM 判断自然语言断言 | 仅在 basis 中时；实验/WIP |
| ACTION | 是否匹配 reference tool calls | 仅在 basis 中时；会把 reference path 变成硬要求 |

evaluate_simulation() 在 basis 模式下将 basis 中各 component reward 相乘；RewardInfo 同时保留 reward_breakdown、DB check、action checks、communication checks 和 NL assertions。即使 ACTION 不在 basis，系统仍可输出 partial_action_reward 作为“像不像这条参考轨迹”的诊断，不能把它当正确性。

集合级 agent_metrics 会过滤 infrastructure error simulation，计算平均 reward、pass^k 和成本；pass^k = C(success_count,k) / C(num_trials,k)。因此多次 trial 的稳定通过率和单次平均 reward 是两个不同指标。

来源：

- [τ² evaluation guide](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/docs/evaluation.md)
- [Task schema](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/src/tau2/data_model/tasks.py)
- [Evaluator and reward multiplication](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/src/tau2/evaluator/evaluator.py)
- [Environment evaluator](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/src/tau2/evaluator/evaluator_env.py)
- [RewardInfo](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/src/tau2/data_model/simulation.py)
- [Agent metrics and Pass^k](https://github.com/sierra-research/tau2-bench/blob/2174a603f6d014ef94473ffa95957f6ce27100db/src/tau2/metrics/agent_metrics.py)

## 6. AgentBench：框架状态和 task 分数分离

AgentBench 由 Task Server、Agent Server 和 Client 组成，通过 HTTP 解耦。扩展接口要求 task 实现 get_indices()、start_sample() 和 calculate_overall()：前者枚举样本，中者执行单样本，后者在全体样本结束后返回任意 JSON-serializable 总结并保存到 overall.json。

每个样本的 TaskSampleExecutionResult 有 status 和 result 两个字段。当前 SampleStatus 包括 running、completed、agent context limit、agent validation failed、agent invalid action、task limit reached、unknown 和 task error。这体现了一个重要原则：**“样本是否正常完成”与“完成后的任务分数”不能用同一个字段表达。** 不同 task 可以分别使用 success rate、accuracy、F1、win rate 或 reward。

来源：

- [Introduction](https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/docs/Introduction_en.md)
- [Extension interface](https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/docs/Extension_en.md)
- [Status enum](https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/src/typings/status.py)
- [WebShop task example](https://github.com/THUDM/AgentBench/blob/d1e4a10db08c87075c78972e48ecc182be03e2d5/src/server/tasks/webshop/task.py)

## 对 my-swe-benchmark 的落地设计

### A. case 必须是“预先写好的验收契约”

建议每个 case 至少包含：

~~~text
case.yaml
prompt.md                 # Agent 可见
fixture/                  # Agent 可见的初始项目
public/                   # 可选，允许 Agent 运行的公开测试
grader/                   # 隐藏；Runner 注入或在独立 verifier 环境运行
reference/                # 仅用于作者校验，不进入 Agent workspace
~~~

发布前做三次校验：

1. baseline fixture：目标问题测试必须失败；
2. reference solution：目标测试和回归测试必须通过；
3. verifier 重复运行：在相同输入上多次运行结果稳定，且不会依赖网络或时间偶然性。

### B. grader 输出结构化结果

不要只依赖退出码。建议 grader.json 至少包含：

~~~json
{
  "status": "passed",
  "score": 85,
  "checks": {
    "fail_to_pass": {"passed": 3, "total": 4, "weight": 60, "score": 45},
    "pass_to_pass": {"passed": 5, "total": 5, "weight": 25, "score": 25},
    "build": {"passed": 1, "total": 1, "weight": 15, "score": 15}
  },
  "regressions": [],
  "duration_ms": 812
}
~~~

公式示例：

~~~text
check_score = passed / total * weight
task_score  = sum(check_score)
~~~

如果某项是硬门槛（例如无法构建就不算解决），应显式写 required: true 或使用 all-pass，而不是在代码里隐式把分数清零。

### C. 状态和分数分开存储

建议统一状态枚举：

| 状态 | 含义 | 处理建议 |
| --- | --- | --- |
| passed | Agent 正常结束且硬检查通过 | 计入成功 |
| grader_failed | Agent 正常结束但功能/回归检查失败 | 计入能力失败 |
| timeout | Agent 或 verifier 超时 | 单独统计；是否记 0 分要明确 |
| agent_error | CLI/SDK 崩溃、非零退出或取消 | 单独统计 |
| patch_invalid | patch/workspace 无法使用 | 单独统计 |
| infra_error | 镜像、依赖、测试器本身故障 | 不应和模型能力失败混排 |
| invalid | run 或 grader 输出不符合 schema | 触发数据质量告警 |

OpenAI Evals/Inspect 的经验表明，grader_failed 还应细分“被测答案错误”和“grader 无法判断”；后者应保留 reason，不能自动归为 incorrect。

### D. 套件级报告

至少报告：

~~~text
resolution_rate = passed_tasks / valid_submitted_tasks
mean_score      = average(task_score over valid runs)
timeout_rate    = timeout_runs / all_runs
agent_error_rate= agent_error_runs / all_runs
infra_rate      = infra_error_runs / all_runs
pass^k          = 同一 case 多次运行的组合全通过率（可选）
~~~

duration、tool_calls、token usage、cost 是效率和可观测性指标，不应偷偷混入正确性分数。报告必须带上 agent_system、harness_version、model、cli_version、case_version 和 environment_fingerprint，否则不同环境的结果无法解释。

### E. Agent 产品线和纯模型控制线分榜

第一阶段比较的是完整 agent_system = harness + model + tools + permissions + prompts。同一个模型在 Claude Code、Codex、OpenCode 中的分数不能直接解释成纯模型差异。若要比较纯模型，后续固定一个统一 Harness，再替换模型 API，并使用同一套 grader、工具和提示词。

## 最小实施顺序

1. 先把当前 grader 从“退出码二值”升级为结构化 checks；
2. 为一个 case 增加 baseline/reference/stability 校验；
3. 将 grader/ 与 Agent workspace 隔离，正式运行使用 separate verifier/container；
4. 完善 timeout、agent error、grader error、infra error 的状态映射；
5. 再扩充 cases、矩阵运行和展示网站；
6. 最后实现统一 Harness 的纯模型控制线。

## 参考资料版本清单

| 项目 | commit |
| --- | --- |
| SWE-bench | 02e7a74ffd0b707aab73d203fe87bdc7c76afc8e |
| Terminal-Bench | e2995b93b0a46edee7bc9942ea5622411a6d5bb9 |
| Harbor | d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621 |
| Inspect AI | 8ebe620d74c1eb679438db1b65324e30e2306092 |
| OpenAI Evals | 8eac7a7de5215c907fbddc30efdaf316913eccdd |
| tau²/τ³-bench | 2174a603f6d014ef94473ffa95957f6ce27100db |
| AgentBench | d1e4a10db08c87075c78972e48ecc182be03e2d5 |
