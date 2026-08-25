# AlphaSolve

中文 | [English](README.en.md)

> 把一个数学问题放进空文件夹，AlphaSolve 自主探索直到解决——产出自然语言证明，支持断点续研与人机协作。

<p align="center">
  <img src="docs/assets/alphasolve-dashboard.png" alt="AlphaSolve 实时面板" width="100%">
</p>

---

## 它能做什么

AlphaSolve 是一个多智能体数学定理证明系统。它把 LLM 组织成一条可持续运行的研究流水线：

- **Orchestrator** 执行研究计划，将研究轨道拆成有界任务，并按可用 slot 并行派出 Worker
- **Research Reviewer** 读取完整的只读 DAG 语义投影和 worker 证据，返回多轨 `research_plan`，而不是直接指定单个 worker 任务
- **Generator** 提出猜想和证明，**Verifier** 从不同角度严格审查，**Reviser** 修复漏洞
- **TheoremChecker** 判断已验证命题是否解决了原问题
- **Task Audit / Process Audit** 分别判断单次任务是否交付、整体研究是否推进
- **Curator** 是 canonical difficulty DAG 的唯一写入者，在后台归档证据、维护节点和关系

无人干预时，AlphaSolve 能自主运行数十小时。它尤其适合需要反复试错、积累中间引理的数学问题。

人类可以在任何时刻介入：往 `verified_propositions` 里手工添加命题、删除幻觉内容、在 `knowledge/references` 中放入论文或笔记——AlphaSolve 会在运行时自动读取这些干预。

---

## 设计理念

### 1. 研究树与 Agent Loop 分离

数学研究过程天然是树形的：一个问题会分裂为多个义务、路线、反例和局部引理；但传统 Agent Loop 通常是线性的：读取上下文、调用工具、等待结果，再决定下一步。让单个 loop 直接维护完整研究树，容易把“最近一次成功”误认为“全局方向正确”，也容易在同一路线内重复尝试。

AlphaSolve 因此将两者分开：

- **离线记忆加工**：`Curator` 处理 worker 事件、verified proposition、失败原因和 handoff，把可核对证据沉淀为 canonical difficulty DAG 与 `knowledge/`。
- **研究策略 subagent**：`research_reviewer` 读取 DAG 的只读语义投影和最新 worker evidence，进行 exploration / exploitation、decompose、reopen、refute 等策略判断，返回多轨 `research_plan`。
- **在线执行 loop**：`Orchestrator` 不维护数学树，也不自行发明路线，只把 research plan 编译为 bounded worker task，并根据 slot 执行。

这样，研究树的长期结构不会被单轮 loop 的局部上下文绑架；策略可以在离线加工后的完整记忆上重新判断，并在后续承载更丰富的 `research strategy`。

### 2. 所有动作都必须有反馈

数学搜索最危险的问题不是单次失败，而是系统把“产出了一个看似正确的命题”误判成“研究方向正在推进”。因此 AlphaSolve 引入独立的 auditor 角色，避免 Orchestrator 自我评价进展：

- **Task Audit**：短周期检查一次 worker 是否完成了当次 assigned obligation；区分 `delivered`、`partial`、`off_target`、`not_delivered`，并记录 residual obligation、rejection locus 和可复用内容。
- **Process Audit**：定期检查整个 portfolio 是否推进原问题；判断 `ADVANCING`、`STALLED` 或 `MISALIGNED`，为切换 explore / exploit 方向提供长期反馈。
- **Worker rejection feedback**：worker 被 rejected 后，不只记录失败，还要求分析是目标错误、证明缺口、局部可修复、执行失败还是路线本身遇到障碍。

核心原则是：**无论是局部动作、整体策略，还是 rejected 产出，都必须产生足够的反馈，下一次决策不能只依赖成功/失败二值信号。** Auditor 只读、只报告，不直接调度 Worker 或修改 canonical DAG。

### 3. Subagent 先作为工具，便于替换和集成

`research_reviewer`、`compute_subagent`、`reasoning_subagent`、`numerical_experiment_subagent` 和 `curator` 都通过工具化边界接入，而不是与 Orchestrator 深度耦合。这样做有几个目的：

- 可以独立限制权限、预算、递归深度和可见数据；
- 可以替换模型或执行后端，而不改变核心编排逻辑；
- 后续可以将 subagent 直接替换成 hook / plugin；
- 便于接入已有 research harness、实验平台和外部评测框架。

工具边界同时也是职责边界：subagent 返回证据或策略，Orchestrator 执行，Curator 归档，Auditor 评价。

### 4. Self-generated knowledge 有价值，但成本高

Worker 和 subagent 在研究过程中产生的大量推理轨迹、失败比较、路线淘汰和局部洞见，即使没有形成 verified proposition，也可能对后续研究有价值。AlphaSolve 尝试将这类 self-generated knowledge（包括部分 CoT 派生的研究摘要）沉淀到 `knowledge/`，作为导航信息提供给后续 Reviewer 和 Worker。

这类知识不能替代 verified proposition：它可能包含错误、重复或过时判断，必须通过引用、审计和后续验证来使用。与此同时，知识提取、压缩、冲突处理和上下文注入的 token/时间成本较高，当前仍是成本较大的实验能力，后续会继续优化其摘要粒度、增量更新和检索方式。

---

## 快速开始

> 本节面向没有编程经验的数学工作者和数学系学生，跟着做就行。

### 1. 获取 API 密钥

AlphaSolve 需要调用大语言模型来推理。推荐 **DeepSeek**（国内手机号直接注册，新用户有免费额度）。

1. 打开 https://platform.deepseek.com/ ，注册账号
2. 进入「API Keys」页面，点击 **创建 API Key**，复制密钥（格式类似 `sk-xxxxxxxxxxxxxxxx`）——密钥只显示一次，立即保存

把密钥设为永久环境变量（只需一次）：

3. 按 `Win` 键，输入**「环境」**，打开**「编辑系统环境变量」**
4. 点击**「环境变量(N)…」** → **「系统变量(S)」** 下点击**「新建(W)…」**
5. 变量名：`DEEPSEEK_API_KEY`，变量值：粘贴密钥
6. 全部点**「确定」**关闭

### 2. 安装

打开终端（`Win + R` → 输入 `cmd` → 回车），粘贴运行：

```bash
curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/main/install.bat -o install.bat && install.bat
```

> macOS / Linux：`curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/main/install.sh | sh`

脚本自动安装 uv、下载 AlphaSolve 和依赖。不需要单独装 Python。完成后 `alphasolve` 命令全局可用。

### 3. 写一个数学问题

1. 新建一个空白文件夹（比如桌面上的 `my_problem`）
2. 在里面新建文件 `problem.md`（注意后缀是 `.md` 不是 `.txt`）
3. 用记事本打开，用数学语言写下你想证明的命题

> 问题描述要完整，包含全部条件和结论。不要写笼统方向如「把某某推广到某某」。推荐 LaTeX：

```
证明：对于任意正整数 n，前 n 个正整数的立方和等于前 n 个正整数和的平方，即
$$\sum_{k=1}^n k^3 = \left(\sum_{k=1}^n k\right)^2$$
```

4. 保存文件

### 4. 运行

1. 在文件夹空白处右击 → **「在终端中打开」**
2. 输入 `alphasolve` 回车

你会看到实时面板，显示各阶段进度。不要关终端，让它跑。想停就关窗口或 `Ctrl+C`。

### 5. 查看结果

运行结束后文件夹下会生成：

| 输出 | 说明 |
|------|------|
| `solution.md` | 完整证明（问题解决时出现） |
| `workspace/verified_propositions/` | 所有已验证的中间命题 |
| `workspace/knowledge/` | 运行过程中积累的数学知识和思路 |
| `workspace/curation_records/difficulty_dag.json` | Curator 维护的 canonical difficulty DAG |
| `workspace/curation_records/research_plans/` | Research Reviewer 生成的 research plan 及执行记录 |
| `workspace/progress_audits/` | Task/Process Audit 的 checkpoint 和证据快照 |

中途停止后再在同一文件夹运行 `alphasolve`，会自动接续——已验证命题和知识库直接复用。

---

## 运行过程中发生了什么

AlphaSolve 的研究流程像一个不断循环的实验室。当前架构将“研究策略”和“执行调度”明确分开：

- `research_reviewer` 只负责比较证据、选择研究轨道、给出 `primary` / `challenger` / `supporting` 方向及禁忌约束；不派发 Worker、不写 DAG、不定义验收 rubric。
- `orchestrator` 根据研究计划和当前空闲 worker slot 选择轨道，把每条轨道拆成一个有界任务，并为任务编写可审计的 rubric；它不自行发明数学路线。
- `curator` 根据可核对证据维护 canonical difficulty DAG；worker handoff、连续无进展和 reviewer 建议本身不会自动创建 child 或改变 canonical 状态。

```text
worker 结果
    │
    ├── Task Audit：这次分派是否完成了 assigned obligation？
    └── Process Audit：整个 portfolio 是否推进了原问题？
             │
             ▼
      Research Reviewer
             │  research_plan（1–4 条 tracks）
             ▼
       Orchestrator
             │  选择 tracks、拆解 bounded tasks、填写 rubric
             ▼
       Worker pool（最多 max_workers 个并行 Worker）
```

```text
problem.md + verified_propositions + knowledge
      │
      ▼
┌─ Orchestrator ──────────────────────────────────────────────────────┐
│  收集 worker 结果、Task Audit、Process Audit                         │
│  必要时调用 research_reviewer                                         │
│  执行 research_plan：选择轨道、拆成 bounded task、填写 rubric       │
│  不创建或修改 canonical DAG                                           │
└──────────────────────────────────────────────────────────────────────┘
      │ RequestResearchPlan
      ▼
┌─ Research Reviewer ────────────────────────────────────────────────┐
│  读取完整的只读 DAG 语义投影和最新 worker evidence                   │
│  返回 1–4 条 tracks：primary / challenger / supporting               │
│  记录 route tabu、exploit / explore / refute 判断                    │
│  不派发 Worker、不写 DAG、不定义 worker acceptance rubric             │
└──────────────────────────────────────────────────────────────────────┘
      │ ExecuteResearchPlan
      ▼
┌─ Worker Pool（默认最多 2 个并行 Worker）────────────────────────────┐
│  每条 track 被 orchestrator 编译为一个有界、可审计的 worker task     │
│                                                                        │
│  Generator ──→ Verifier ×5 ──→ Reviser（最多 6 轮）                  │
│                                      │                                 │
│                                      ▼                                 │
│                           TheoremChecker（最多 5 次）                 │
│                                                                        │
│  Task Audit：检查 assigned obligation 是否 delivered                   │
│  Process Audit：检查 portfolio 是否推进原问题                         │
└────────────────────────────────────────────────────────────────────────┘
      │ verified proposition / worker evidence / audit outcome
      ▼
┌─ Curator（后台、canonical DAG 唯一写入者）──────────────────────────┐
│  归档不可变 worker 事件和 verified evidence                           │
│  维护 difficulty nodes、edges、aliases、statuses                      │
│  整理 knowledge；不选择研究策略                                        │
└──────────────────────────────────────────────────────────────────────┘
```

每个 Worker 运行完整的 **生成 → 审查 → 修复 → 定理检查** 流水线。多个 Worker 可以并行，但并发上限只是资源上限，不代表一定启动同样数量的 Worker；是否启动第二条轨道由 reviewer research plan 和当前可用 slot 共同决定。

### 五种 Verifier 策略

| 策略 | 审查角度 |
|------|----------|
| `verifier_format_references` | 检查 Statement、引用和文件格式是否满足命题协议 |
| `verifier_citation` | 检查引用的已有命题是否被正确应用 |
| `verifier_failure_modes` | 识别常见推理失效模式 |
| `verifier_stepwise` | 逐步检查证明链条的每一步 |
| `verifier_premise_chain` | 回溯前提链，检查是否有隐含的未声明假设 |

每轮审查中，Verifier 按配置的策略和 scaling factor 独立运行。任何一轮发现问题，Reviser 就会修正后再重新审查，最多执行 `max_verify_rounds` 轮。这就是为什么一个看似简单的命题可能经历多轮验证-修正才通过——每轮都是不同视角的严格审查。

### Research Plan 与研究轨道

当局部 worker 证据不足以决定下一步，Orchestrator 调用 `RequestResearchPlan`。`research_reviewer` 返回一个包含 1–4 条轨道的 `research_plan`：

| 轨道 | 含义 |
|------|------|
| `primary` | 当前证据最支持的主研究路线 |
| `challenger` | 使用不同数学机制、检验关键前提或攻击 parent/ancestor 的独立挑战路线 |
| `supporting` | 为主路线提供必要桥梁的辅助路线 |

Reviewer 只决定研究方向、路线身份、理由和禁忌；它不直接派发 Worker，也不写 acceptance rubric。Orchestrator 使用 `ExecuteResearchPlan`，根据可用 slot 选择轨道，把每条轨道拆成一个 bounded task，并为每个任务编写 3–6 条可由 Statement 检查的 rubric。

仅改变 `method_id` 不会构成新的数学路线。最近尝试过的 `route_label` 默认作为 tabu，除非有新证据、明确局部修复或目标发生实质变化。`verified` 但 `off_target` / `partial` 的命题不会自动算作 assigned obligation 的进展。

### 两类审计与 canonical DAG

- **Task Audit**：短周期审计单次派发是否完成 assigned obligation；它可以返回 `delivered`、`partial`、`off_target` 或 `not_delivered`。
- **Process Audit**：长周期审计整个研究 portfolio 是否推进原问题；`STALLED` / `MISALIGNED` 是 reviewer 的策略证据，不是自动调度指令。
- **Difficulty DAG**：Curator 是 canonical DAG 的唯一写入者。worker handoff、连续无进展、reviewer observation 和失败记录会先作为证据归档；它们不会自动创建 child、split difficulty 或改变节点状态。

---

## 人机协作

AlphaSolve 不只是一个「按下就跑」的工具。你可以在任何时刻介入：

### 往 `verified_propositions` 添加命题

用 Markdown + LaTeX 写下你认为关键的命题，放入 `workspace/verified_propositions/`。AlphaSolve 续跑时会将其视为已验证命题，在此基础上继续探索。

### 删除幻觉内容

如果 Verifier 未能捕获的幻觉渗入了 `verified_propositions`，手工删除即可，然后继续运行。

### 往 `knowledge/references` 添加参考

把论文摘要、关键定理、个人笔记（Markdown 格式）放入 `workspace/knowledge/references/`。Orchestrator 和 Worker 会阅读这些材料来引导后续方向。

### 提供 `hint.md`

在问题文件夹中创建 `hint.md`，写上解题提示或背景知识。AlphaSolve 启动时会读取它。

---

## 命令行用法

```bash
# 最简单：当前目录下有 problem.md 即可
alphasolve

# 指定问题和提示文件
alphasolve --problem ./problem.md --hint ./hint.md

# 调整并发和验证强度（默认 max_workers=2）
alphasolve --workers 2 --verifier_scaling_factor 3 --max_verify_rounds 4

# 自定义 agent 配置
alphasolve --config ./my_config/

# 查看 tier 和 preset 列表（用于理解模型配置）
alphasolve --list-tiers
alphasolve --list-presets

# 通过 --env 临时设置环境变量（不修改系统）
alphasolve --env DEEPSEEK_API_KEY=sk-xxx

# 跳过 Wolfram 探测（加快启动）
alphasolve --no_wolfram_prime

# 调试日志（logs/ 下记录每个 agent 的详细行为）
alphasolve --debug

# 关闭实时面板
alphasolve --no_dashboard

# 交互式 Agent REPL（第二层单 agent 模式）
alphasolve --agent

# 单次 Agent 模式（非交互，输出结果后退出）
alphasolve --agent -p "证明 1+2+...+n = n(n+1)/2"

# 本地 demo（不调用 LLM）
alphasolve --demo
```

### CLI 参数一览

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--problem` | `problem.md` | 问题文件路径 |
| `--hint` | 无 | 提示文件路径（不存在则忽略） |
| `--workers` | 2 | 最大并发 worker 数；实际数量由 research plan、依赖关系和可用 slot 决定 |
| `--config` | 内置配置 | agents.yaml 路径或目录 |
| `--max_verify_rounds` | 6 | 每个命题最大验证-修正轮数 |
| `--verifier_scaling_factor` | 5 | 每轮独立验证次数 |
| `--subagent_max_depth` | 1 | subagent 最大递归深度 |
| `--max_orchestrator_restarts` | 50 | Orchestrator 最大重启次数 |
| `--debug` | false | 启用调试日志（`logs/` 下记录每个 agent 的详细 trace） |
| `--tool_executor_size` | 4 | Python 执行进程池大小 |
| `--no_wolfram_prime` | false | 跳过 Wolfram 探测 |
| `--no_dashboard` | false | 关闭实时终端面板 |
| `--agent` | false | 进入交互式 Agent REPL |
| `-p` / `--print` | 无 | 单次 Agent 执行（需配合 `--agent`） |
| `--list-tiers` | false | 列出 tier 配置后退出 |
| `--list-presets` | false | 列出 preset 配置后退出 |
| `--env KEY=VAL` | 无 | 临时环境变量（可重复使用） |
| `--demo` | false | 本地 demo（无 LLM 调用） |

---

## 配置

### 模型选择：Tier + Preset 系统

AlphaSolve 用 **Tier → Preset → 模型** 的三层映射来配置模型，而不是逐个 agent 硬编码。

**Tier** 是三个档位，每个 agent 在 YAML 中声明自己属于哪个档位：

| Tier | 默认映射 | 用途 |
|------|----------|------|
| `cheap` | `deepseek-flash` | Curator、compute subagent 等后台/计算任务 |
| `balanced` | `deepseek-pro` | Generator、Verifier、Reviser 等核心推理任务 |
| `max` | `qwen-3.7-max` | Orchestrator（需要最强的规划能力） |

**Preset** 定义具体的 API 连接参数（endpoint、密钥环境变量、模型名、超时等）。查看全部 preset：

```bash
alphasolve --list-presets
```

当前内置 preset 包括：`deepseek-flash`、`deepseek-pro`、`parasail-deepseek`、`longcat`、`moonshot-kimi`、`volcano-doubao`、`volcano-deepseek`、`dashscope-deepseek`、`mimo`、`openrouter-gemini`、`deepseek-pro-anthropic`、`moonshot-kimi-anthropic`、`qwen-3.7-max`。

### 自定义 Tier 和 Preset

在 `~/.alphasolve/` 下创建 `tiers.yaml` 和 `presets.yaml`，可以覆盖内置配置或新增自己的 preset。格式参考内置的 `src/alphasolve/config/tiers.yaml` 和 `presets.yaml`。

例如，想把 `balanced` 档位换成 Moonshot Kimi：

```yaml
# ~/.alphasolve/tiers.yaml
cheap: deepseek-flash
balanced: moonshot-kimi
max: qwen-3.7-max
```

用户文件与内置配置合并，用户文件优先。

### API 密钥

根据使用的 provider 设置环境变量：

| 环境变量 | Provider |
|----------|----------|
| `DEEPSEEK_API_KEY` | DeepSeek |
| `ARK_API_KEY` | 火山引擎（字节跳动） |
| `MOONSHOT_API_KEY` | Moonshot / Kimi |
| `DASHSCOPE_API_KEY` | 阿里云 DashScope |
| `LONGCAT_API_KEY` | LongCat |
| `PARASAIL_API_KEY` | Parasail |
| `OPENROUTER_API_KEY` | OpenRouter |
| `MIMO_API_KEY` | 小米 MIMO |

密钥可以通过三种方式提供（优先级从高到低）：

1. `--env KEY=VAL` 命令行参数
2. 系统环境变量（即上面表格中的方式）
3. `.env` 文件（项目目录下的 `.env` 或 `~/.alphasolve/.env`）

也可以通过 `ALPHASOLVE_CONFIG_DIR` 环境变量改变配置目录的位置（默认 `~/.alphasolve/`）。

### Wolfram Engine（可选）

AlphaSolve 可调用 Wolfram 内核做符号计算。未安装 Wolfram 时仍可正常运行（compute subagent 的 `RunWolfram` 工具会提示不可用）。

若 Wolfram 内核不在默认路径：

```bash
export WOLFRAM_KERNEL=/path/to/WolframKernel
```

### Agent 配置文件

每个 agent 的 system prompt、工具列表、max_turns 等参数在独立 YAML 文件中配置：

```
src/alphasolve/solver/config/
    agents.yaml                ← 入口：全局参数和 agent 目录
    agents/
        orchestrator.yaml
        generator.yaml
        verifier.yaml
        verifier_format_references.yaml
        verifier_citation.yaml
        verifier_failure_modes.yaml
        verifier_stepwise.yaml
        verifier_premise_chain.yaml
        verifier_adversarial.yaml  # 可选，当前默认 verifier_agents 未启用
        reviser.yaml
        theorem_checker.yaml
    subagents/
        compute_subagent.yaml
        reasoning_subagent.yaml
        numerical_experiment_subagent.yaml
        curator.yaml
        research_reviewer.yaml
```

可通过 `--config` 指定自己的配置目录（里面放同名 YAML 即可覆盖内置配置）。

### 关键参数（`agents.yaml`）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_verify_rounds` | 6 | 每个命题最大验证-修正轮数 |
| `verifier_scaling_factor` | 5 | 每轮独立验证次数 |
| `verifier_agents` | `verifier_format_references`, `verifier_citation`, `verifier_failure_modes`, `verifier_stepwise`, `verifier_premise_chain` | Verifier 策略列表 |
| `subagent_max_depth` | 1 | subagent 最大递归深度 |
| `max_orchestrator_restarts` | 50 | Orchestrator 最大重启次数 |

`CHECK_IS_THEOREM_TIMES`（默认 5）控制定理检查的独立尝试次数，定义在 `src/alphasolve/solver/wolfram_state.py`。

---

## 系统架构

```
CLI (alphasolve)
    └── AlphaSolve.run()                        [solver/app.py]
            ├── Wolfram 内核探测
            ├── ExecutionGateway (Python / Wolfram 进程池)
            ├── CuratorQueue (后台知识管理 agent)
            └── Orchestrator.run()              [solver/orchestrator.py]
                    └── WorkerManager
                            └── Worker × N (线程)  [solver/worker.py]
                                    ├── Generator
                                    ├── Verifier × verifier_scaling_factor
                                    ├── Reviser
                                    └── TheoremChecker
```

### 核心组件

| 组件 | Tier | 作用 |
|------|------|------|
| **Orchestrator** | max | 收集审计和 worker 证据，执行 research plan，按 slot 拆解并派发 bounded task |
| **Research Reviewer** | balanced | 读取 DAG 只读投影和证据，返回多轨 `research_plan`；不派发、不写 DAG |
| **Generator** | balanced | 提出猜想和证明草稿 |
| **Verifier** (五种策略) | balanced | 从不同角度独立审查证明 |
| **Reviser** | balanced | 根据 Verifier 反馈修正命题 |
| **TheoremChecker** | balanced | 判断已验证命题是否解决了原问题 |
| **Task Audit** | balanced | 判断单次 worker 任务是否交付 |
| **Process Audit** | balanced | 判断累计研究是否推进原问题 |
| **Curator** | cheap | 唯一写入 canonical difficulty DAG，后台整理知识和证据 |
| **compute subagent** | cheap | 配备 `RunPython` / `RunWolfram` 的计算 subagent |
| **reasoning subagent** | balanced | 纯数学推理 subagent（无计算工具） |
| **numerical experiment subagent** | cheap | 有界探索与局部数值实验 |

---

## 从源码安装

```bash
git clone https://github.com/tanzcoding/AlphaSolve.git
cd AlphaSolve

# uv（推荐）
uv tool install -e .

# pipx
pipx install -e .

# pip（开发模式）
pip install -e .
```

---

## 调试日志与研究状态

使用 `--debug` 运行时，`logs/` 下会记录每个 agent 的详细行为 trace；运行时研究状态和可复现证据写入问题目录的 `workspace/`：

```
logs/{run_id}/
    token_usage.jsonl       # 每个 agent / worker turn 的 token 和时间
    search_tree.jsonl       # spawn/result 的尝试谱系（只读观测）
    workers/                # 每个 Worker 的完整生成→审查→修正流水线
    subagents/              # research_reviewer、compute 等 subagent 会话
    curator/                # curator 会话

workspace/
    curation_records/
        difficulty_dag.json                 # curator-owned canonical DAG
        research_plans/plan-*.json          # reviewer research plan 与执行状态
        events.jsonl                        # 不可变编排/归档事件
    progress_audits/
        checkpoint-*/audit.md               # Process Audit 长周期判断
        checkpoint-*/evidence.md             # 该 checkpoint 的证据快照
    progress_audit_outcomes.jsonl           # settled worker outcome ledger
    attempt_graph.jsonl                     # worker attempt / parent provenance
    verified_propositions/                   # 通过验证的数学命题
    knowledge/                               # 导航性知识、失败经验和参考资料
```

查看研究进度时，优先区分三类信息：

1. `verified_propositions/`：已验证的数学事实；
2. `progress_audits/` 和 `progress_audit_outcomes.jsonl`：任务交付与整体推进事实；
3. `curation_records/difficulty_dag.json`：Curator 归档后的 canonical 节点、边和状态。

`worker handoff`、连续 `no_progress`、`STALLED` 或 reviewer 的图观察首先都是证据，不会自动修改 canonical DAG。

---

## 致谢与相关工作

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) 及其开源实现 [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)
- [kimi-cli](https://github.com/MoonshotAI/kimi-cli) — 多个工具的参数设计和 tool result 格式参考了其写法