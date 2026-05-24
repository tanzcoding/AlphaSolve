# AlphaSolve

[中文](README.md) | English

> Put a math problem in an empty folder. AlphaSolve explores autonomously until it's solved — producing natural-language proofs, with resumable research and human-in-the-loop collaboration.

<p align="center">
  <img src="docs/assets/alphasolve-dashboard.png" alt="AlphaSolve live dashboard" width="100%">
</p>

---

## What It Does

AlphaSolve is a multi-agent mathematical theorem-proving system. It organizes LLMs into a long-running research pipeline:

- **Orchestrator** plans directions and dispatches parallel Workers
- **Generator** proposes conjectures and proofs; **Verifier** scrutinizes them from different angles; **Reviser** patches flaws
- **TheoremChecker** decides whether verified propositions solve the original problem
- **Curator** continuously organizes accumulated knowledge in the background

Without human intervention, AlphaSolve runs autonomously for dozens of hours. It is especially suited to problems requiring repeated trial and error and accumulation of intermediate lemmas.

You can intervene at any point: add propositions to `verified_propositions`, delete hallucinated content, or put papers and notes into `knowledge/references` — AlphaSolve reads these on resume and adjusts its exploration accordingly.

---

## Quick Start

> This section is for mathematicians and math students with no programming experience. Just follow the steps.

### 1. Get an API Key

AlphaSolve calls an LLM for reasoning. **DeepSeek** is recommended (Chinese phone numbers can register directly; new users get free credits).

1. Open https://platform.deepseek.com/ and create an account
2. Go to "API Keys" → **Create API Key** → copy the key (looks like `sk-xxxxxxxxxxxxxxxx`). **The key is shown only once — save it immediately**

Set the key as a permanent environment variable (one-time setup):

On Windows:

3. Press `Win`, type **environment**, open **Edit the system environment variables**
4. Click **Environment Variables...** → under **System variables** click **New...**
5. Variable name: `DEEPSEEK_API_KEY`, Variable value: paste your key
6. Click **OK** on all windows

On macOS/Linux, add to your shell profile:

```bash
export DEEPSEEK_API_KEY=your_key
```

### 2. Install

On Windows, open Command Prompt (`Win + R` → `cmd` → Enter) and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/main/install.bat -o install.bat && install.bat
```

On macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/tanzcoding/AlphaSolve/main/install.sh | sh
```

The script installs uv, downloads AlphaSolve, and installs dependencies. No separate Python needed. After installation, `alphasolve` is available globally.

### 3. Write a Math Problem

1. Create a new empty folder (e.g. `my_problem` on your desktop)
2. Create a file named `problem.md` inside it (extension `.md`, not `.txt`)
3. Open it with a text editor and write the theorem or problem you want to prove

> Describe the problem completely — all assumptions and conclusions. Avoid vague directions like "generalize X to Y." LaTeX is recommended:

```
Prove that for every positive integer n, the sum of the cubes of the first n
positive integers equals the square of the sum of the first n positive integers:
$$\sum_{k=1}^n k^3 = \left(\sum_{k=1}^n k\right)^2$$
```

4. Save the file

### 4. Run

1. Right-click an empty area in the folder → **Open in Terminal**
2. Type `alphasolve` and press Enter

You'll see a live dashboard showing progress. Keep the terminal open. To stop, close the window or press `Ctrl+C`.

### 5. Check Results

After the run, the folder contains:

| Output | Description |
|--------|-------------|
| `solution.md` | The complete proof (appears when the problem is solved) |
| `workspace/verified_propositions/` | All verified intermediate propositions |
| `workspace/knowledge/` | Accumulated mathematical knowledge and insights |

Stopping and running `alphasolve` again in the same folder resumes automatically — verified propositions and the knowledge base are reused.

---

## What Happens During a Run

AlphaSolve's research loop works like a constantly cycling laboratory:

```
problem.md
      |
      v
+-- Orchestrator ----------------------------------------------------+
|  Reads verified_propositions and knowledge                         |
|  Plans directions, dispatches Workers                              |
|  Waits for results, decides next step                              |
+--------------------------------------------------------------------+
      |  spawn_worker(hint)
      v
+-- Worker ----------------------------------------------------------+
|                                                                    |
|  Generator  -->  Writes proposition (conjecture + proof draft)     |
|       |                                                            |
|       v                                                            |
|  Verifier x4  -->  Four strategies scrutinize independently        |
|       |              citation | failure_modes                      |
|       |              stepwise  | premise_chain                     |
|       v                                                            |
|  Review failed? --> Reviser patches, loops back to Verifier        |
|       |             (up to 6 rounds)                               |
|       v                                                            |
|  Review passed --> TheoremChecker: does this solve the problem?    |
|       |             (5 independent checks)                         |
|       v                                                            |
|  Solves problem --> solution.md  [OK]                              |
|  Otherwise --> proposition enters verified_propositions for reuse  |
|                                                                    |
+--------------------------------------------------------------------+
      |
      v  (concurrently, in background)
+-- Curator ---------------------------------------------------------+
|  Extracts mathematical knowledge from Worker traces                |
|  Organizes into knowledge/ for all agents to read                  |
|  Handles conflicts and cross-checks                                |
+--------------------------------------------------------------------+
```

Each Worker runs in an independent thread with a full generate → verify → revise pipeline. Multiple Workers can run in parallel — control concurrency with `--workers 4`.

### Four Verifier Strategies

| Strategy | Review Angle |
|----------|-------------|
| `verifier_citation` | Checks whether cited propositions are correctly applied |
| `verifier_failure_modes` | Identifies common reasoning failure patterns |
| `verifier_stepwise` | Examines each step of the proof chain |
| `verifier_premise_chain` | Traces premise chains for hidden unstated assumptions |

These four strategies rotate across verification rounds. If any round finds a problem, Reviser fixes it and verification restarts. This is why a seemingly simple proposition may go through 6 verify-revise rounds — each round brings a different perspective.

---

## Human-in-the-Loop

AlphaSolve is not just a "press and run" tool. You can intervene at any point:

### Add propositions to `verified_propositions`

Write key propositions in Markdown + LaTeX and place them in `workspace/verified_propositions/`. AlphaSolve treats them as verified on resume and continues from there.

### Delete hallucinated content

If hallucinations slip through the Verifiers into `verified_propositions`, delete them manually and continue running.

### Add references to `knowledge/references`

Put paper summaries, key theorems, or personal notes (Markdown format) into `workspace/knowledge/references/`. Orchestrator and Workers read these to guide subsequent exploration.

### Provide `hint.md`

Create `hint.md` in the problem folder with solution hints or background knowledge. AlphaSolve reads it on startup.

---

## Command-Line Usage

```bash
# Simplest: just have problem.md in the current directory
alphasolve

# Specify problem and hint files
alphasolve --problem ./problem.md --hint ./hint.md

# Adjust concurrency and verification strength
alphasolve --workers 4 --verifier_scaling_factor 3 --max_verify_rounds 4

# Custom agent configuration
alphasolve --config ./my_config/

# List tiers and presets (for understanding model config)
alphasolve --list-tiers
alphasolve --list-presets

# Temporary env var override via --env
alphasolve --env DEEPSEEK_API_KEY=sk-xxx

# Skip Wolfram probe (faster startup)
alphasolve --no_wolfram_prime

# Debug logs (detailed agent traces under logs/)
alphasolve --debug

# Disable live dashboard
alphasolve --no_dashboard

# Interactive Agent REPL (single-agent mode)
alphasolve --agent

# Single-shot Agent mode (non-interactive, prints result)
alphasolve --agent -p "Prove that 1+2+...+n = n(n+1)/2"

# Local demo (no LLM calls)
alphasolve --demo
```

### CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--problem` | `problem.md` | Path to the problem file |
| `--hint` | none | Path to hint file (ignored if missing) |
| `--workers` | 4 | Number of concurrent workers |
| `--config` | built-in config | Custom agents.yaml path or directory |
| `--max_verify_rounds` | 6 | Max verify-revise rounds per proposition |
| `--verifier_scaling_factor` | 4 | Independent verification attempts per round |
| `--subagent_max_depth` | 1 | Max recursive depth for subagents |
| `--max_orchestrator_restarts` | 50 | Max Orchestrator restarts |
| `--debug` | false | Enable debug logs (detailed agent traces under `logs/`) |
| `--tool_executor_size` | 4 | Python execution process-pool size |
| `--no_wolfram_prime` | false | Skip Wolfram kernel probe at startup |
| `--no_dashboard` | false | Disable live terminal dashboard |
| `--agent` | false | Enter interactive Agent REPL |
| `-p` / `--print` | none | Single-shot Agent execution (requires `--agent`) |
| `--list-tiers` | false | List tier mappings and exit |
| `--list-presets` | false | List presets and exit |
| `--env KEY=VAL` | none | Temporary env var override (repeatable) |
| `--demo` | false | Local demo mode (no LLM calls) |

---

## Configuration

### Model Selection: Tier + Preset System

AlphaSolve uses a **Tier → Preset → Model** three-layer mapping for model configuration, rather than per-agent hardcoding.

**Tiers** are three levels. Each agent declares its tier in its YAML config:

| Tier | Default mapping | Usage |
|------|----------------|-------|
| `cheap` | `deepseek-flash` | Curator, compute subagent — background/computation tasks |
| `balanced` | `deepseek-pro` | Generator, Verifier, Reviser — core reasoning tasks |
| `max` | `qwen-3.7-max` | Orchestrator — needs strongest planning capability |

**Presets** define specific API connection parameters (endpoint, key env var, model name, timeout, etc.). View all presets:

```bash
alphasolve --list-presets
```

Built-in presets include: `deepseek-flash`, `deepseek-pro`, `parasail-deepseek`, `longcat`, `moonshot-kimi`, `volcano-doubao`, `volcano-deepseek`, `dashscope-deepseek`, `mimo`, `openrouter-gemini`, `deepseek-pro-anthropic`, `moonshot-kimi-anthropic`, `qwen-3.7-max`.

### Customizing Tiers and Presets

Create `tiers.yaml` and `presets.yaml` under `~/.alphasolve/` to override built-in configs or add your own presets. Format follows the built-in files at `src/alphasolve/config/tiers.yaml` and `presets.yaml`.

For example, to switch the `balanced` tier to Moonshot Kimi:

```yaml
# ~/.alphasolve/tiers.yaml
cheap: deepseek-flash
balanced: moonshot-kimi
max: qwen-3.7-max
```

User files merge with built-in configs, with user files taking priority.

### API Keys

Set environment variables for the providers you use:

| Env var | Provider |
|---------|----------|
| `DEEPSEEK_API_KEY` | DeepSeek |
| `ARK_API_KEY` | Volcengine (ByteDance) |
| `MOONSHOT_API_KEY` | Moonshot / Kimi |
| `DASHSCOPE_API_KEY` | Alibaba Cloud DashScope |
| `LONGCAT_API_KEY` | LongCat |
| `PARASAIL_API_KEY` | Parasail |
| `OPENROUTER_API_KEY` | OpenRouter |
| `MIMO_API_KEY` | Xiaomi MIMO |

Keys can be provided in three ways (highest priority first):

1. `--env KEY=VAL` command-line flags
2. System environment variables (the table above)
3. `.env` files (project-local `.env` or `~/.alphasolve/.env`)

You can also change the config directory location with the `ALPHASOLVE_CONFIG_DIR` env var (default: `~/.alphasolve/`).

### Wolfram Engine (Optional)

AlphaSolve can call the Wolfram kernel for symbolic computation. Without Wolfram installed, it still runs normally — the `RunWolfram` tool simply reports unavailable.

If the Wolfram kernel is not on the default path:

```bash
export WOLFRAM_KERNEL=/path/to/WolframKernel
```

### Agent Configuration Files

Each agent's system prompt, tool list, max_turns, etc. are configured in individual YAML files:

```
src/alphasolve/solver/config/
    agents.yaml                <- entry point: global params and directories
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

Use `--config` to specify your own config directory (place same-named YAMLs to override built-in configs).

### Key Parameters (`agents.yaml`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_verify_rounds` | 6 | Max verify-revise rounds per proposition |
| `verifier_scaling_factor` | 4 | Independent verification attempts per round (four strategies rotate) |
| `verifier_agents` | `verifier_citation`, `verifier_failure_modes`, `verifier_stepwise`, `verifier_premise_chain` | Verifier strategies to use |
| `subagent_max_depth` | 1 | Max recursive depth for subagents |
| `max_orchestrator_restarts` | 50 | Max Orchestrator restarts |

`CHECK_IS_THEOREM_TIMES` (default 5) controls independent theorem-checking attempts and is defined in `src/alphasolve/solver/wolfram_state.py`.

---

## System Architecture

```
CLI (alphasolve)
    +-- AlphaSolve.run()                        [solver/app.py]
            +-- Wolfram kernel probe
            +-- ExecutionGateway (Python / Wolfram process pools)
            +-- CuratorQueue (background knowledge-management agent)
            +-- Orchestrator.run()              [solver/orchestrator.py]
                    +-- WorkerManager
                            +-- Worker x N (threads)  [solver/worker.py]
                                    +-- Generator
                                    +-- Verifier x verifier_scaling_factor
                                    +-- Reviser
                                    +-- TheoremChecker
```

### Core Components

| Component | Tier | Role |
|-----------|------|------|
| **Orchestrator** | max | Plans directions, dispatches Workers, surveys workspace state; can call `research_reviewer` |
| **Generator** | balanced | Proposes conjectures and proof drafts |
| **Verifier** (four strategies) | balanced | Scrutinizes proofs from different angles |
| **Reviser** | balanced | Patches propositions based on Verifier feedback |
| **TheoremChecker** | balanced | Decides whether a verified proposition solves the original problem |
| **Curator** | cheap | Background knowledge organizer; handles conflicts and cross-checks |
| **research_reviewer** | balanced | Surveys `verified_propositions/` and `knowledge/`, suggests research directions |
| **compute subagent** | cheap | Equipped with `RunPython` / `RunWolfram` |
| **reasoning subagent** | balanced | Pure mathematical reasoning (no computation tools) |
| **numerical experiment subagent** | cheap | Bounded exploration and local numerical experiments |

---

## Install from Source

```bash
git clone https://github.com/tanzcoding/AlphaSolve.git
cd AlphaSolve

# uv (recommended)
uv tool install -e .

# pipx
pipx install -e .

# pip (development mode)
pip install -e .
```

---

## Debug Logs

Running with `--debug` records detailed agent behavior traces under `logs/`:

```
logs/{run_id}/
    orchestrator.log        # Every Orchestrator LLM call and tool use
    curator/                # One file per curator session
        20260428_153045.log
    workers/
        worker_{hash}.log   # Each Worker's full generate -> verify -> revise pipeline
```

---

## Acknowledgements and Related Work

- [AI Mathematician (AIM)](https://arxiv.org/html/2505.22451v1) and its open-source implementation [Carlos-Mero/AIM](https://github.com/Carlos-Mero/AIM/)
- [kimi-cli](https://github.com/MoonshotAI/kimi-cli) — informed several tool-parameter designs and tool-result formats