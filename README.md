# AlphaSolve

中文 | [English](README.en.md)

> 把一个数学问题放进空文件夹，AlphaSolve 自主探索直到解决——产出自然语言证明，支持断点续研与人机协作。

<p align="center">
  <img src="docs/assets/alphasolve-dashboard.png" alt="AlphaSolve 实时面板" width="100%">
</p>

---

## 它能做什么

AlphaSolve 是一个多智能体数学定理证明系统。它把 LLM 组织成一条可持续运行的研究流水线：

- **Orchestrator** 规划研究方向，派出多个 Worker 并行尝试
- **Generator** 提出猜想和证明，**Verifier** 从不同角度严格审查，**Reviser** 修复漏洞
- **TheoremChecker** 判断已验证命题是否解决了原问题
- **Curator** 在后台持续整理探索过程中积累的知识

无人干预时，AlphaSolve 能自主运行数十小时。它尤其适合需要反复试错、积累中间引理的数学问题。

人类可以在任何时刻介入：往 `verified_propositions` 里手工添加命题、删除幻觉内容、在 `knowledge/references` 中放入论文或笔记——AlphaSolve 会在运行时自动读取这些干预。

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

中途停止后再在同一文件夹运行 `alphasolve`，会自动接续——已验证命题和知识库直接复用。

---

## 运行过程中发生了什么

AlphaSolve 的研究流程像一个不断循环的实验室：

```
problem.md
      │
      ▼
┌─ Orchestrator ─────────────────────────────────────────────────┐
│  阅读 verified_propositions 和 knowledge                        │
│  制定方向，派出 Worker                                          │
│  等待结果，决定下一步                                            │
└────────────────────────────────────────────────────────────────┘
      │  spawn_worker(hint)
      ▼
┌─ Worker ───────────────────────────────────────────────────────┐
│                                                                │
│  Generator  ──→  写出命题（猜想 + 证明草稿）                     │
│       │                                                        │
│       ▼                                                        │
│  Verifier ×4  ──→  四种审查策略独立审查                          │
│       │              citation │ failure_modes                  │
│       │              stepwise │ premise_chain                  │
│       ▼                                                        │
│  审查未通过？──→  Reviser 修复，再回到 Verifier                  │
│       │          （最多 6 轮）                                  │
│       ▼                                                        │
│  审查通过 ──→  TheoremChecker 判断是否解决了原问题               │
│       │          （5 次独立检查）                               │
│       ▼                                                        │
│  解决原问题 ──→  solution.md  ✅                               │
│  否则 ──→  命题进入 verified_propositions，供后续复用            │
│                                                                │
└────────────────────────────────────────────────────────────────┘
      │
      ▼  （同时，后台运行）
┌─ Curator ──────────────────────────────────────────────────────┐
│  从 Worker 探索轨迹中提取数学知识                                │
│  整理写入 knowledge/，供所有 Worker 和 Orchestrator 阅读         │
│  处理知识冲突、交叉验证                                          │
└────────────────────────────────────────────────────────────────┘
```

每个 Worker 是一条独立线程，运行完整的 **生成 → 审查 → 修复** 流水线。多个 Worker 可以并行——你可以用 `--workers 4` 控制并发数。

### 四种 Verifier 策略

| 策略 | 审查角度 |
|------|----------|
| `verifier_citation` | 检查引用的已有命题是否被正确应用 |
| `verifier_failure_modes` | 识别常见推理失效模式 |
| `verifier_stepwise` | 逐步检查证明链条的每一步 |
| `verifier_premise_chain` | 回溯前提链，检查是否有隐含的未声明假设 |

每轮审查中，这四种策略轮替运行。任何一轮发现问题，Reviser 就会修正后再重新审查。这就是为什么一个看似简单的命题可能经历多轮验证-修正才通过——每轮都是不同视角的严格审查。

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

# 调整并发和验证强度
alphasolve --workers 4 --verifier_scaling_factor 3 --max_verify_rounds 4

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
| `--workers` | 4 | 并发 worker 数 |
| `--config` | 内置配置 | agents.yaml 路径或目录 |
| `--max_verify_rounds` | 6 | 每个命题最大验证-修正轮数 |
| `--verifier_scaling_factor` | 4 | 每轮独立验证次数 |
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
        verifier_citation.yaml
        verifier_failure_modes.yaml
        verifier_stepwise.yaml
        verifier_premise_chain.yaml
        verifier_adversarial.yaml
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
| `verifier_scaling_factor` | 4 | 每轮独立验证次数（四种策略轮替） |
| `verifier_agents` | `verifier_citation`, `verifier_failure_modes`, `verifier_stepwise`, `verifier_premise_chain` | Verifier 策略列表 |
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
| **Orchestrator** | max | 规划方向、派出 Worker、综述工作区状态；可调用 `research_reviewer` |
| **Generator** | balanced | 提出猜想和证明草稿 |
| **Verifier** (四种策略) | balanced | 从不同角度独立审查证明 |
| **Reviser** | balanced | 根据 Verifier 反馈修正命题 |
| **TheoremChecker** | balanced | 判断已验证命题是否解决了原问题 |
| **Curator** | cheap | 后台整理知识、处理冲突、交叉验证 |
| **research_reviewer** | balanced | 综述 `verified_propositions/` 和 `knowledge/`，建议研究方向 |
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

## 调试日志

使用 `--debug` 运行时，`logs/` 下会记录每个 agent 的详细行为 trace：

```
logs/{run_id}/
    orchestrator.log        # Orchestrator 的每一次 LLM 调用、工具使用
    curator/                # 每次 curator session 一个文件
        20260428_153045.log
    workers/
        worker_{hash}.log   # 每个 Worker 的完整 生成→审查→修正 流水线
```

---

## 致谢与相关工作

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) 及其开源实现 [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)
- [kimi-cli](https://github.com/MoonshotAI/kimi-cli) — 多个工具的参数设计和 tool result 格式参考了其写法