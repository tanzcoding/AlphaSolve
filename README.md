# AlphaSolve

An AI-powered mathematical research system designed to accelerate mathematical problem-solving and theorem discovery.

## Workflow Architecture

```mermaid
flowchart TD
    Start([开始]) --> Solver

    Solver["<b>Solver</b><br/>───────────────<br/>• 生成新的猜想(conjecture)或最终证明<br/>• 使用LLM分析问题和已验证的引理<br/>• 提取猜想内容、证明和依赖关系<br/>• 可生成中间引理或定理的最终证明<br/>• 轮次限制: TOTAL_SOLVER_ROUND"]
    
    Verifier["<b>Verifier</b><br/>───────────────<br/>• 验证Solver生成的猜想证明是否正确<br/>• 使用测试时扩展(test-time scaling)<br/>• 多次验证以提高准确性<br/>• 检查证明逻辑和推理路径<br/>• 生成详细的review反馈"]
    
    Refiner["<b>Refiner</b><br/>───────────────<br/>• 根据Verifier的反馈改进猜想<br/>• 修正证明中的错误<br/>• 判断猜想是否根本性错误<br/>• 生成改进后的猜想和证明<br/>• 保持猜想的依赖关系"]
    
    Summarizer["<b>Summarizer</b><br/>───────────────<br/>• 总结整个求解过程<br/>• 汇总所有已验证的引理<br/>• 生成最终报告"]
    
    End([结束])

    %% Solver的出口
    Solver -->|"生成猜想<br/>(CONJECTURE_GENERATED)"| Verifier
    Solver -->|"执行错误<br/>(EXIT_ON_ERROR)<br/>重试"| Solver
    Solver -->|"轮次耗尽<br/>(EXIT_ON_EXAUSTED)"| Summarizer
    
    %% Verifier的出口
    Verifier -->|"发现错误<br/>(CONJECTURE_UNVERIFIED)<br/>需要改进"| Refiner
    Verifier -->|"引理正确<br/>(CONJECTURE_VERIFIED)<br/>继续探索"| Solver
    Verifier -->|"定理完成<br/>(DONE)<br/>问题已解决"| Summarizer
    Verifier -->|"verify-refine轮次耗尽<br/>(EXIT_ON_EXAUSTED)"| Solver
    
    %% Refiner的出口
    Refiner -->|"改进成功<br/>(REFINE_SUCCESS)<br/>重新验证"| Verifier
    Refiner -->|"猜想根本性错误<br/>(CONJECTURE_WRONG)<br/>重新生成"| Solver
    Refiner -->|"执行错误<br/>(EXIT_ON_ERROR)<br/>重试"| Refiner
    
    %% 结束
    Summarizer --> End

    style Solver fill:#e1f5ff,stroke:#01579b,stroke-width:2px
    style Verifier fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    style Refiner fill:#fff3e0,stroke:#e65100,stroke-width:2px
    style Summarizer fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px
    style Start fill:#fce4ec,stroke:#880e4f,stroke-width:2px
    style End fill:#fce4ec,stroke:#880e4f,stroke-width:2px
```

### Usage

1. Set up your API keys in the environment variables:
```bash
export DEEPSEEK_API_KEY="your_deepseek_api_key"
# or alternatively for other providers
export MOONSHOT_API_KEY="your_moonshot_api_key"
export ARK_API_KEY="your_ark_api_key"
export DASHSCOPE_API_KEY="your_dashscope_api_key"
export OPENROUTER_API_KEY="your_openrouter_api_key"
```
2. Place your mathematical problem in the [`problem.md`](problem.md) file
3. Run the main solver:
```bash
python main.py
```

### Configuration

AlphaSolve supports multiple LLM providers. You can configure which provider to use by modifying the [`config/agent_config.py`](config/agent_config.py) file:

```python
class AlphaSolveConfig:
    # Available configurations: DEEPSEEK_CONFIG, MOONSHOT_CONFIG, VOLCANO_CONFIG,
    # DASHSCOPE_CONFIG, OPENROUTER_CONFIG, CUSTOM_LLM_CONFIG
    
    # Configure LLM providers for each agent
    REFINER_CONFIG = VOLCANO_CONFIG
    SOLVER_CONFIG = VOLCANO_CONFIG
    VERIFIER_CONFIG = VOLCANO_CONFIG
    SUMMARIZER_CONFIG = VOLCANO_CONFIG
```

Each agent (Solver, Verifier, Refiner, Summarizer) can be configured with a different LLM provider based on your needs.

## Benchmark

### Running Benchmarks

To evaluate AlphaSolve's performance:

1. Place the standard solution in the [`standard_solution.md`](standard_solution.md) file
2. Run the benchmark script:
```bash
python benchmark.py
```

### How Benchmarking Works

The benchmark system executes AlphaSolve **10 times** on the same problem to calculate accuracy. For each run:

1. AlphaSolve generates a solution using its solve-verify-refine workflow
2. An LLM evaluator compares AlphaSolve's answer against the standard solution
3. The evaluator determines whether the solution is correct or incorrect
4. Results are aggregated to calculate the overall accuracy rate
