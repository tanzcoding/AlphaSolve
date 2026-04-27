# AlphaSolve

AlphaSolve 是一个基于大语言模型（LLM）的自动化数学定理证明系统。它采用 **Orchestrator 驱动、多 worker 并行**的架构，通过 Generator → Verifier → Reviser 的迭代循环，逐步构建完整的数学证明。

## 核心特性

- **Orchestrator 驱动**：LLM Orchestrator 读取已验证命题和 knowledge/ 下的知识摘要，动态决定何时 spawn 新 worker 以及使用什么提示
- **并行 Worker**：多个 worker 同时独立探索，每个 worker 运行完整的 Generator → Verifier → Reviser 流水线
- **多 Verifier 协同审查**：每轮验证启动多次独立尝试，在多种 Verifier 之间轮换，各自从不同角度审查证明，一次不通过则视为不通过。支持通过 YAML 自行配置 Verifier 的工作方式，支持为 Verifier 添加 SKILLS
- **Subagent 系统**：Generator、Verifier、Reviser 可调用 compute subagent（Python / Wolfram）和 reasoning subagent 辅助探索
- **知识摘要**：后台 `knowledge_digest` agent 持续将运行 trace 摘要写入 `workspace/knowledge/`，供 Orchestrator 参考
- **多 LLM 提供商**：支持 DeepSeek、火山引擎、Moonshot、DashScope、LongCat、Parasail、OpenRouter、MIMO 等

## 系统架构

```
CLI (alphasolve)
    └── AlphaSolve.run()                        [workflow.py]
            ├── Wolfram 内核探测
            ├── ExecutionGateway (Python / Wolfram 进程池)
            ├── KnowledgeDigestQueue (后台知识摘要 agent)
            └── Orchestrator.run()              [orchestrator.py]
                    └── WorkerManager
                            └── Worker × N (线程)  [worker.py]
                                    ├── Generator
                                    ├── Verifier (× verifier_scaling_factor)
                                    ├── Reviser
                                    └── TheoremChecker
```

### 工作流程

```
problem.md
    │
    ▼
Orchestrator (LLM)
    │  读取 verified_propositions/ 和 knowledge/
    │  调用 spawn_worker(hint) / wait()
    │
    ├──► Worker
    │        │
    │        ├─ Generator      → 生成命题 proposition.md（陈述 + 证明）
    │        ├─ Verifier × N   → 多个 Verifier 独立审查，LLM 综合判定
    │        ├─ Reviser        → 根据反馈修正（最多 max_verify_rounds 轮）
    │        └─ TheoremChecker → 判断是否解决原问题（CHECK_IS_THEOREM_TIMES 次独立检查）
    │
    ▼
verified_propositions/   ←  通过验证的命题（供所有 worker 和 Orchestrator 读取）
    │
    └── 某命题解决原问题 → solution.md
```

### 核心组件

| 组件 | 作用 |
|------|------|
| **Orchestrator** | 读取工作区状态，用有针对性的提示 spawn worker，调用 `wait()` 等待结果 |
| **Worker** | 独立线程，运行完整的 生成 → 验证 → 修正 流水线 |
| **Generator** | 提出新命题（猜想 + 证明），写入 worker 目录 |
| **Verifier** | 严格审查证明；有多种 Verifier 策略，可调用 subagent |
| **Reviser** | 根据 Verifier 反馈修正命题，原地重写文件 |
| **TheoremChecker** | 判断已验证命题是否（连同其引用的命题）证明了原问题 |
| **compute subagent** | 配备 `run_python` / `run_wolfram` 工具的计算 subagent |
| **reasoning subagent** | 纯数学推理 subagent（无计算工具） |
| **knowledge_digest** | 后台 agent，将 trace 中的数学知识提取摘要写入 `knowledge/`，处理知识冲突并进行交叉验证 |

## 安装

推荐使用 **uv**（或 pipx）安装，以便在任意目录运行 `alphasolve`。

```bash
# 克隆仓库
git clone https://github.com/tanzcoding/AlphaSolve.git
cd AlphaSolve

# 方式一：uv（推荐）
uv tool install -e .

# 方式二：pipx
pipx install -e .

# 方式三：pip（开发模式）
pip install -e .
```

## 配置

### API 密钥

根据使用的 LLM 提供商设置环境变量：

```bash
export DEEPSEEK_API_KEY=your_key      # DeepSeek
export ARK_API_KEY=your_key           # 火山引擎（字节跳动）
export MOONSHOT_API_KEY=your_key      # Moonshot / Kimi
export DASHSCOPE_API_KEY=your_key     # 阿里云 DashScope
export LONGCAT_API_KEY=your_key       # LongCat
export PARASAIL_API_KEY=your_key      # Parasail
export OPENROUTER_API_KEY=your_key    # OpenRouter
export MIMO_API_KEY=your_key          # 小米 MIMO
```

### Wolfram Engine（可选）

若 Wolfram 内核不在默认路径，设置：

```bash
export WOLFRAM_KERNEL=/path/to/WolframKernel
```

### 选择模型

编辑 `src/alphasolve/config/agent_config.py`，修改各组件使用的预置配置：

```python
GENERATOR_CONFIG    = {**DEEPSEEK_CONFIG}
VERIFIER_CONFIG     = {**DEEPSEEK_CONFIG}
REVISER_CONFIG      = {**DEEPSEEK_CONFIG}
ORCHESTRATOR_CONFIG = {**DEEPSEEK_PRO_CONFIG}   # Orchestrator 默认使用 DeepSeek Pro
```

支持的预置：`DEEPSEEK_CONFIG`、`DEEPSEEK_PRO_CONFIG`、`VOLCANO_CONFIG`、`MOONSHOT_CONFIG`、`DASHSCOPE_CONFIG`、`LONGCAT_CONFIG`、`PARASAIL_CONFIG`、`OPENROUTER_CONFIG`、`MIMO_CONFIG`

每个 agent 的详细参数（system prompt、工具列表、max_turns 等）在 `src/alphasolve/config/agents/` 下的独立 YAML 文件中配置，顶层 `src/alphasolve/config/agents.yaml` 作为入口。

### 关键参数（`agents.yaml`）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_verify_rounds` | 6 | 每个命题的最大验证-修正轮数 |
| `verifier_scaling_factor` | 5 | 每轮独立验证尝试次数 |
| `verifier_agents` | `verifier_failure_modes`, `verifier_stepwise` | 使用的 Verifier 列表 |
| `subagent_max_depth` | 2 | subagent 最大递归深度 |

`CHECK_IS_THEOREM_TIMES`（默认 5）在 `agent_config.py` 中配置，控制定理检查的独立尝试次数。

## 使用方法

### 1. 准备问题文件

在任意工作目录创建 `problem.md`，写入数学问题（支持 LaTeX）：

```bash
mkdir my_problem && cd my_problem
cat > problem.md << 'EOF'
证明：对于任意正整数 n，1 + 2 + ... + n = n(n+1)/2。
EOF
```

可选：创建 `hint.md` 提供解题提示或背景知识。

### 2. 运行

```bash
# 基本运行（读取当前目录的 problem.md）
alphasolve

# 常用选项
alphasolve --problem ./problem.md --hint ./hint.md

# 调整并发和验证强度
alphasolve --workers 4 --verifier_scaling_factor 3 --max_verify_rounds 4

# 使用自定义配置目录
alphasolve --config ./my_config/

# 跳过 Wolfram 探测（加快启动）
alphasolve --no_wolfram_prime

# 启用调试日志（在 logs/ 下记录每个 agent 的详细行为 trace）
alphasolve --debug

# 关闭实时终端面板
alphasolve --no_dashboard

# 本地 demo 模式（不调用 LLM API）
alphasolve --demo
```

### CLI 参数一览

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--problem` | `problem.md` | 问题文件路径 |
| `--hint` | 无 | 提示文件路径 |
| `--workers` | 4 | 并发 worker 数 |
| `--config` | 内置配置 | 自定义 agents.yaml 路径或目录 |
| `--max_verify_rounds` | 来自 agents.yaml | 每个命题最大验证-修正轮数 |
| `--verifier_scaling_factor` | 来自 agents.yaml | 每轮独立验证次数 |
| `--subagent_max_depth` | 来自 agents.yaml | subagent 最大递归深度 |
| `--debug` | false | 启用调试日志，在 `logs/` 下记录每个 agent 的详细行为 trace |
| `--tool_executor_size` | 4 | Python 执行进程池大小 |
| `--no_wolfram_prime` | false | 跳过启动时的 Wolfram 探测 |
| `--no_dashboard` | false | 关闭实时终端面板 |
| `--demo` | false | 本地 demo 模式（无 LLM 调用） |

### 3. 从断点继续研究

AlphaSolve 支持在已有工作基础上继续运行。只需在同一工作目录下再次执行 `alphasolve`：

```bash
cd my_problem   # 已有 workspace/、problem.md 的目录
alphasolve
```

**续跑机制：**

- `workspace/verified_propositions/` 中已验证的命题会被新的 Orchestrator 自动读取，作为已知知识直接复用
- `workspace/knowledge/log.md` 中积累的知识摘要同样供 Orchestrator 参考
- 新产生的 worker 目录命名为 `prop-{hash}`，不含序号，不会与上次运行的目录冲突

**人工添加命题：**

人类专家可以直接将自己认为关键的命题（标准 Markdown + LaTeX 格式）放入 `workspace/verified_propositions/`，AlphaSolve 续跑时会将其视为已验证命题，在此基础上继续探索。

### 4. 查看结果

运行结束后，工作目录下会生成：

```
workspace/
    verified_propositions/    # 所有通过验证的命题
    knowledge/          # 运行过程知识摘要（log.md + 按主题整理的知识条目）
solution.md             # 最终解决方案（问题解决时生成）
```

使用 `--debug` 运行时，`logs/` 目录下会额外记录每个 agent 的实时行为 trace：

```
logs/{run_id}/
    orchestrator.log        # Orchestrator 的每一次 LLM 调用、工具使用
    digests/                # 每次 knowledge_digest chat session 一个文件
        20260428_153045.log
    workers/
        worker_{hash}.log   # 每个 worker 的完整 生成→验证→修正 流水线
```

## 致谢与相关工作

AlphaSolve 的架构参考了如下工作：

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) 及其开源实现 [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)
