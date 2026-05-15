# AlphaSolve

> 一个面向数学研究的多智能体证明工作台：让 LLM 能够进行长程研究，支持人机协作和断点续研，并产出**自然语言**证明。

把一个 `problem.md` 放进空文件夹，运行 `alphasolve`。AlphaSolve 会持续探索，直到解决问题。您也可以在产生的 `verified_propositions` 中手工添加命题，或者在 `knowledge/references` 中添加可参考的论文或笔记 (均要求markdown格式) 以引导和干预 AlphaSolve的后续行为。

<p align="center">
  <img src="docs/assets/alphasolve-dashboard.png" alt="AlphaSolve 实时面板" width="100%">
</p>

## 这是什么？

AlphaSolve 是一个基于大语言模型（LLM）的自动化数学定理证明系统。它大体上遵循一套可持续运行的研究流程：Orchestrator 负责规划方向，多个 Worker 并行尝试证明，Verifier 从不同角度审查和抑制幻觉，Reviser 修复失败证明，Curator 将探索过程中形成的知识整理进 `workspace/knowledge/`，供后续继续使用。在无人干预的情况下，AlphaSolve 能够自主运行几十小时。它特别适合那些需要长时间探索、反复试错、积累中间引理和失败经验的数学问题。当幻觉透过 Verifier 渗入到 `verified_propositions` 中时，人类可以手工删除，并继续启动 AlphaSolve 进行研究。


## 快速开始

> 本节面向**没有编程经验**的数学工作者和数学系学生，跟着下面的说明一步步操作即可。

### 第一步：获取 DeepSeek API 密钥并设为环境变量

AlphaSolve 需要调用大语言模型进行推理。推荐 **DeepSeek**（国内手机号可直接注册，新用户有免费额度）。

1. 打开浏览器，访问 https://platform.deepseek.com/ ，用手机号注册账号
2. 进入「API Keys」页面，点击 **创建 API Key**，复制生成的密钥（格式类似 `sk-xxxxxxxxxxxxxxxx`）。**密钥只显示一次，请立即复制保存**

现在把密钥设为永久环境变量（只需设置一次，之后永久有效）：

3. 按 `Win` 键，输入**「环境」**，点击出现的**「编辑系统环境变量」**
4. 在弹出的窗口中点击右下角的**「环境变量(N)…」**按钮
5. 在**「系统变量(S)」**栏目下点击**「新建(W)…」**
6. **变量名**填写：`DEEPSEEK_API_KEY`，**变量值**粘贴刚才复制的密钥
7. 三个窗口全部点击**「确定」**关闭

### 第二步：安装 AlphaSolve

打开终端（`Win + R`，输入 `cmd` 回车），复制粘贴下面这行命令，回车：

```bash
curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/procedural_knowledge_mem/install.bat -o install.bat && install.bat
```

> macOS / Linux 用户：用 `curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/procedural_knowledge_mem/install.sh | sh`

脚本会自动完成所有安装工作（安装 uv → 下载 AlphaSolve → 安装依赖），不需要单独安装 Python。安装完成后，`alphasolve` 命令全局可用。

### 第三步：写一个数学问题

1. 在任意你喜欢的地方**新建一个空白文件夹**（例如桌面上新建 `my_problem` 文件夹）
2. 进入该文件夹，新建一个文本文件，命名为 `problem.md`（注意后缀是 `.md` 不是 `.txt`）
3. 用记事本打开 `problem.md`，用数学语言写入你想证明的命题或解决的问题

> 务必把问题描述清楚，包含完整的条件和结论。不要写笼统的描述如「把某某结果推广到某某上」。推荐使用 LaTeX 公式，例如：

```
证明：对于任意正整数 n，前 n 个正整数的立方和等于前 n 个正整数和的平方，即
$$\sum_{k=1}^n k^3 = \left(\sum_{k=1}^n k\right)^2$$
```

4. 保存文件

### 第四步：运行

1. 在 `my_problem` 文件夹的**空白处右击**，选择**「在终端中打开」**
2. 输入以下命令，回车：

```
alphasolve
```

你会看到一个实时面板，显示 AlphaSolve 正在工作的各个阶段。不要关闭终端，让它运行即可。如果想中途停止，直接关闭窗口即可。（或者使用 `Ctrl+C` 中断程序）

### 第五步：查看结果

运行结束后，当前文件夹下会生成：

- **`solution.md`** — 如果问题被解决，这里就是完整证明
- **`workspace/verified_propositions/`** — 所有已验证的中间命题
- **`workspace/knowledge/`** — 运行过程中积累的数学知识和思路

如果中途停止或运行结束，已产生的中间结果不会丢失。下一次在同一文件夹运行 `alphasolve` 会自动接续之前的工作。

### 下一步

- 遇到问题？加 `--debug` 运行（`alphasolve --debug`），`logs/` 下会生成详细诊断日志
- 想调整证明策略？见[使用方法](#使用方法)
- 想换个模型？见[配置](#配置)

## 系统架构

```
CLI (alphasolve)
    └── AlphaSolve.run()                        [workflow.py]
            ├── Wolfram 内核探测
            ├── ExecutionGateway (Python / Wolfram 进程池)
            ├── CuratorQueue (后台知识管理 agent)
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
| **Orchestrator** | 读取工作区状态，用有针对性的提示 spawn worker，调用 `wait()` 等待结果。可调用 `research_reviewer` 综述大量文件 |
| **Worker** | 独立线程，运行完整的 生成 → 验证 → 修正 流水线 |
| **Generator** | 提出新命题（猜想 + 证明），写入 worker 目录 |
| **Verifier** | 严格审查证明；有多种 Verifier 策略，可调用 subagent |
| **Reviser** | 根据 Verifier 反馈修正命题，原地重写文件 |
| **TheoremChecker** | 判断已验证命题是否（连同其引用的命题）证明了原问题 |
| **compute subagent** | 配备 `run_python` / `run_wolfram` 工具的计算 subagent |
| **reasoning subagent** | 纯数学推理 subagent（无计算工具） |
| **numerical experiment subagent** | 有界探索、分支检查与局部数值实验 |
| **research_reviewer** | 综述 `verified_propositions/` 和 `knowledge/`，对比 `problem.md` 给出研究方向建议 |
| **curator** | 后台 agent，将 trace 中的数学知识提取整理写入 `knowledge/`，处理知识冲突并进行交叉验证 |

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
- `workspace/knowledge/index.md` 是知识库路线图，Orchestrator 会据此选择要阅读的主题条目或专题文件夹
- 新产生的 worker 目录命名为 `prop-{hash}`，不含序号，不会与上次运行的目录冲突

**人工添加命题：**

人类专家可以直接将自己认为关键的命题（标准 Markdown + LaTeX 格式）放入 `workspace/verified_propositions/`，AlphaSolve 续跑时会将其视为已验证命题，在此基础上继续探索。

### 4. 查看结果

运行结束后，工作目录下会生成：

```
workspace/
    verified_propositions/    # 所有通过验证的命题
    knowledge/          # 运行过程知识管理（index.md 路线图 + 按主题整理的知识条目/文件夹）
solution.md             # 最终解决方案（问题解决时生成）
```

使用 `--debug` 运行时，`logs/` 目录下会额外记录每个 agent 的实时行为 trace：

```
logs/{run_id}/
    orchestrator.log        # Orchestrator 的每一次 LLM 调用、工具使用
    curator/                # 每次 curator chat session 一个文件
        20260428_153045.log
    workers/
        worker_{hash}.log   # 每个 worker 的完整 生成→验证→修正 流水线
```

## 致谢与相关工作

AlphaSolve 的架构参考了如下工作：

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) 及其开源实现 [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)
- [kimi-cli](https://github.com/MoonshotAI/kimi-cli) — 多个工具的参数设计和 tool result 格式参考了其写法
