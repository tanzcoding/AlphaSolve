# AlphaSolve

AlphaSolve 是一个基于大语言模型（LLM）的自动化数学定理证明系统。它采用**编排器驱动的并行工作线程**架构，通过生成-验证-修正的迭代循环，逐步构建完整的数学证明。

## 核心特性

- **Orchestrator 驱动**：一个 LLM 编排器读取已验证引理和知识摘要，动态决定何时派生新工作线程及使用何种提示
- **并行 LemmaWorker**：多个工作线程同时独立探索，每个线程运行完整的 Generator → Verifier → Reviser → TheoremChecker 流水线
- **多验证器人格**：每轮验证由多种验证器（`verifier_failure_modes`、`verifier_stepwise` 等）独立审查，提高可靠性
- **测试时扩展验证**：`verifier_scaling_factor` 次独立验证尝试，多数通过才算验证成功
- **子代理系统**：Generator/Verifier/Reviser 可调用计算子代理（Python/Wolfram）和推理子代理辅助探索
- **知识摘要**：后台 `knowledge_digest` 代理持续将运行轨迹摘要写入 `workspace/knowledge/log.md`，供编排器参考
- **多 LLM 提供商**：支持 DeepSeek、火山引擎、Moonshot、DashScope、LongCat、Parasail、OpenRouter、MIMO 等

## 系统架构

```
CLI (alphasolve)
    └── AlphaSolve.run()                        [workflow.py]
            ├── Wolfram 内核探测
            ├── ExecutionGateway (Python/Wolfram 进程池)
            ├── KnowledgeDigestQueue (后台知识摘要代理)
            └── Orchestrator.run()              [orchestrator.py]
                    └── WorkerManager
                            └── LemmaWorker × N (线程)  [lemma_worker.py]
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
    │  读取 verified_lemmas/ 和 knowledge/log.md
    │  调用 spawn_worker(hint) / wait()
    │
    ├──► LemmaWorker
    │        │
    │        ├─ Generator      → 生成引理 .md（陈述 + 证明）
    │        ├─ Verifier × N   → 多验证器独立审查，输出 pass/fail
    │        ├─ Reviser        → 根据反馈修正（最多 max_verify_rounds 轮）
    │        └─ TheoremChecker → 判断是否解决原问题（CHECK_IS_THEOREM_TIMES 次）
    │
    ▼
verified_lemmas/   ←  通过验证的引理（供所有线程和编排器读取）
    │
    └── 某引理解决原问题 → solution.md
```

### 核心组件

| 组件 | 角色 |
|------|------|
| **Orchestrator** | 研究总监；读取工作区，用有针对性的提示派生工作线程，调用 `wait()` 等待结果 |
| **LemmaWorker** | 独立线程，运行完整的生成-验证-修正流水线 |
| **Generator** | 提出新引理（猜想 + 证明），写入工作线程目录 |
| **Verifier** | 严格审查证明；支持多种验证器人格，可调用计算子代理 |
| **Reviser** | 根据验证器反馈修正引理，原地重写文件 |
| **TheoremChecker** | 判断已验证引理是否（连同其引用的引理）证明了原问题 |
| **compute_subagent** | 有 `run_python` / `run_wolfram` 工具的计算子代理 |
| **reasoning_subagent** | 纯数学推理子代理（无计算工具） |
| **knowledge_digest** | 后台代理，将轨迹摘要写入 `knowledge/log.md` |

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
export MOONSHOT_API_KEY=your_key      # Moonshot/Kimi
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
ORCHESTRATOR_CONFIG = {**DEEPSEEK_PRO_CONFIG}   # 编排器默认使用 DeepSeek Pro
```

支持的预置：`DEEPSEEK_CONFIG`、`DEEPSEEK_PRO_CONFIG`、`VOLCANO_CONFIG`、`MOONSHOT_CONFIG`、`DASHSCOPE_CONFIG`、`LONGCAT_CONFIG`、`PARASAIL_CONFIG`、`OPENROUTER_CONFIG`、`MIMO_CONFIG`

每个代理的详细参数（system prompt、工具列表、max_turns 等）在 `src/alphasolve/config/agents/` 下的 YAML 文件中配置。

### 关键参数（`agents.yaml`）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_verify_rounds` | 6 | 每个引理的最大验证-修正轮数 |
| `verifier_scaling_factor` | 5 | 每轮独立验证器尝试次数 |
| `verifier_agents` | `verifier_failure_modes`, `verifier_stepwise` | 使用的验证器人格列表 |
| `subagent_max_depth` | 2 | 子代理最大递归深度 |

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
alphasolve --lemmaworkers 4 --verifier_scaling_factor 3 --max_verify_rounds 4

# 使用自定义配置目录
alphasolve --config ./my_config/

# 跳过 Wolfram 探测（加快启动）
alphasolve --no_wolfram_prime

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
| `--lemmaworkers` | 4 | 并发工作线程数 |
| `--config` | 内置配置 | 自定义 agents.yaml 路径或目录 |
| `--max_verify_rounds` | 来自 agents.yaml | 每个引理最大验证-修正轮数 |
| `--verifier_scaling_factor` | 来自 agents.yaml | 每轮独立验证次数 |
| `--subagent_max_depth` | 来自 agents.yaml | 子代理最大递归深度 |
| `--tool_executor_size` | 4 | Python 执行进程池大小 |
| `--no_wolfram_prime` | false | 跳过启动时的 Wolfram 探测 |
| `--no_dashboard` | false | 关闭实时终端面板 |
| `--demo` | false | 本地 demo 模式（无 LLM 调用） |

### 3. 查看结果

运行结束后，工作目录下会生成：

```
workspace/
    verified_lemmas/    # 所有通过验证的引理
    knowledge/log.md    # 运行过程知识摘要
solution.md             # 最终解决方案（问题解决时生成）
logs/
    startup.json            # 启动配置快照
    orchestrator_trace.json # 编排器完整 LLM 轨迹
    worker_results.json     # 所有工作线程结果
```

## 致谢与相关工作

AlphaSolve 的生成-验证-修正循环主要受到以下工作启发：

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) 及其开源实现 [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)：多步探索、共享记忆、验证和精炼机制
- [Long-horizon Reasoning Agent for Olympiad-Level Mathematical Problem Solving](https://arxiv.org/html/2512.10739v2)：面向奥林匹克数学的多轮层级推理与引理记忆
- [Towards Autonomous Mathematics Research (Aletheia)](https://arxiv.org/html/2602.10177v3)：生成、验证和修正的自然语言数学研究流程
