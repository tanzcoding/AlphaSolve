# Orchestrator Harness Optimization Scope

## Benchmark Inputs

Harness optimization must be evaluated against the real workspace snapshots
under this canonical absolute root:

```text
D:\AlphaSolve数学问题\workspace review test
```

For tools, logs, or prompts that need an ASCII-safe spelling, use this escaped
form for the same path:

```text
D:\AlphaSolve\u6570\u5b66\u95ee\u9898\workspace review test
```

The benchmark cases are exactly these four directories:

- `D:\AlphaSolve数学问题\workspace review test\2d elastic wave gwp toy model`
- `D:\AlphaSolve数学问题\workspace review test\bmo optimization`
- `D:\AlphaSolve数学问题\workspace review test\real material blowup 1`
- `D:\AlphaSolve数学问题\workspace review test\real material blowup 2`

These paths are part of the harness contract. Do not optimize against copied
fixtures, synthetic directories, renamed snapshots, or temporary reduced
workspaces unless a separate user-approved benchmark is created.

The expected benchmark behavior is defined in
`docs/orchestrator-harness-benchmark-expected-behavior.md`.

Any `/goal` or automation created for this harness optimization should include
this benchmark root explicitly, rather than relying only on relative fixture
names or the repository docs.

本文档约定 Codex 自动化改进 `alphasolve --agent --profile orchestrator` harness 时的允许范围、语义约束和验证要求。目标是提高真实 orchestrator 在研究断面上的下一步命题决策准确率，同时保持第二层 `alphasolve.agent` 的独立性和通用性。

## 目标

- 让 harness 中的 orchestrator 使用与真实 solver orchestrator 一致的系统提示词、工具集和工具语义。
- 优化 orchestrator 对 `problem.md`、`verified_propositions/`、`knowledge/` 和 reviewer 报告的读取、判断和下一步命题选择。
- 优化 `research_reviewer` 作为 orchestrator 决策辅助者的工具使用、输出结构和证据选择。
- 保持实验变量可解释：每轮改动应能说明它预期改善哪类错误。

## 第一圈：默认允许自动改动

这些改动与 harness 性能直接相关，默认允许 Codex 自动迭代。

### 系统提示词

允许修改：

- `src/alphasolve/solver/prompts/research_reviewer.md`

**不允许修改：**

- `src/alphasolve/solver/prompts/orchestrator.md`

允许的语义级改动包括：

- 对research_reviewer输出结构的要求，举例：比如 `current_state`、`next_proposition`、`why_this_next`、`evidence_files`、`risks_or_unknowns`。
- 明确何时优先做“把 proof tail 已经证明但 Statement 未显式记录的结论整理成新命题”。
- 明确何时调用 `research_reviewer`，以及如何看待 reviewer 结果而不是盲从。
- 不允许为了 benchmark、one-turn review、print mode、harness 观察方式或任何测试入口而改写，防止过拟合one-turn review。任何改动必须放到研究场景中考虑。

### Research Reviewer 的 YAML 配置

允许修改：

- `src/alphasolve/solver/config/subagents/research_reviewer.yaml`

允许的字段包括：

- `tools`
- `tool_parameters`
- `tool_descriptions`
- `max_turns`
- `when_to_use`

允许的语义级改动包括：

- 增删 orchestrator/research_reviewer 可见工具。
- 调整工具描述，使模型更可能选择正确工具。
- 调整参数默认值、范围、枚举和路径约束。
- 为工具增加更明确的使用时机、误用警告和输出消费方式。

### 第三层研究工具

允许修改：

- `src/alphasolve/solver/research_markdown.py`
- `src/alphasolve/solver/tool_runtime.py`

允许的语义级改动包括：

- 修改 `ResearchProgressReview` 的扫描顺序、优先级、证据预算、输出结构和截断策略。
- 修改 `InspectMarkdown` 的 section/tail 选择逻辑和输出格式。
- 修改第三层 override 后的 `Read` tool result，包括 proof-tail hint、index-adjacent audit hint、错误信息和输出预算。
- 新增面向数学研究断面的第三层工具，例如 `ResearchGrep`、`ProofSearch`、`WorkspaceMap`、`VerifiedPropositionMap`。
- 将新增工具接入 orchestrator/research_reviewer 的 YAML 工具集。


## 第二圈：允许但必须谨慎

这些改动影响面更广，允许 Codex 自动尝试，但每轮必须说明风险，并跑全量测试与断面评测。

### Research Reviewer 递归深度

允许调整 orchestrator 调用 `research_reviewer` 时的递归深度，但至多不超过 `2`。

语义约束：

- `research_reviewer` 可以调用一层子 agent 来做局部证据检查或目录分区审阅。
- 不允许递归深度超过 `2`。
- 不允许让 reviewer 变成替代 orchestrator 的全局决策者；reviewer 应输出证据、风险和建议，最终下一步命题仍由 orchestrator 判断。
- 如果启用递归深度 `1`，必须在评测结果中标注，避免和 prompt/tool 描述改动混淆。

### Override Grep 和 Glob

允许在第三层 override 第二层同名 `Grep` 和 `Glob`，即使这会影响 solver 中其他第三层角色。

语义约束：

- override 必须通过第二层正式接口 `ToolRegistry.register(..., replace=True)` 完成。
- 第二层 `alphasolve.agent` 不应包含 AlphaSolve 数学研究排序、路径偏好或 proof/workspace 语义。
- 第三层 `Grep` / `Glob` 可以利用 workspace 结构排序结果，例如优先 `verified_propositions/`、`knowledge/`、`problem.md`、局部 index，或按 proposition/review/knowledge 类型分组。`Grep` 搜索到的结果如果仅在相邻行，其实可以合并成一个结果。
- 输出必须保持紧凑，避免因为排序或分组导致 token 爆炸。
- 如果 override 影响了 generator/verifier/reviser/theorem_checker 等其他角色，必须跑现有测试和工具快照测试；必要时更新快照并说明变化是有意的。

## 第三圈：默认禁止，除非用户明确批准

以下内容**不允许**修改：

- `src/alphasolve/solver/prompts/orchestrator.md`，除非用户以后明确批准解除本冻结规则。
- 第二层 `alphasolve.agent` 的任何东西。
- `src/alphasolve/config/tiers.yaml`、presets、provider、API 环境配置。
- generator、verifier、reviser、theorem_checker、curator 的 prompt 和 YAML 配置。
- `WorkerManager` 的调度语义、worker 生命周期、`SpawnWorker` / `TaskOutput` 的真实执行语义。
- `RoleWorkspaceAccess` 的权限边界和跨 worker 隔离策略。
- 第一圈和第二圈中未提到的任何东西，即只允许修改第一圈和第二圈中提到的东西。

## 每轮改动要求

**不允许**在AlphaSolve代码中硬编码只对特定数学领域生效的名词（例如upper bound、group、ring、category、infimum、estimate），最多只允许硬编码连接逻辑的词（strictly、therefore、hence等等），否则很容易对特定数学问题过拟合！
**不允许**为了one-turn review而优化，所有改动必须放在研究场景中考虑，而不是为了“推荐下一个命题”而考虑！保持最大的泛化性！

每轮自动化优化应输出：

- 改动文件列表。
- 改动意图：它针对哪类错误或哪类失败。
- 是否改动了递归深度。
- 是否新增/override 工具。
- 断面评测结果。
- 至少运行相关单测；影响第三层工具时运行全量测试。
- 至少启动一个subagent来考察该轮改动是否有可能对特定数学问题或领域过拟合，并且对通用性评分。
- 至少启动一个subagent来考察该轮改动是否有可能对单轮测评（即仅要求建议下一个命题）过拟合，在真实的alphasolve研究问题中失效。

如果任何一个上述subagent判定不通过，立刻回退到这一轮改动之前的状态。

## 暂定优化优先级

1. 先优化 research_reviewer 的提示词与 YAML 工具描述。
2. 再优化 `ResearchProgressReview` / `InspectMarkdown` / 第三层 `Read` 的输出结构和排序。
3. 再实验第三层 override `Grep` / `Glob`。
4. 最后再启用 `research_reviewer` 递归深度 `1` 或 `2`。比如让更深层级的 `research_reviewer` 单独去调查某个子目录。
