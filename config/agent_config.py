import os

# 统一的运行时 CONFIG（始终开启“思考/推理”能力，不考虑关闭）
# 说明：
# - 不同供应商开启思考模式的方式已写入 params 字段中，无需在运行时做条件判断。
# - 只需在此处切换 base_url / api_key / model，并按供应商要求设置 params。
# - 任何下游调用都会直接从该字典读取参数构造请求。
# 定义一些常用的供应商预置（可选）：

# DeepSeek 官方
DEEPSEEK_CONFIG = {
    'base_url': 'https://api.deepseek.com',
    'api_key': lambda: os.getenv('DEEPSEEK_API_KEY'),
    # DeepSeek：通过特定模型名启用思考模式
    'model': 'deepseek-reasoner',
    'timeout': 3600,
    'temperature': 1.0,
    'params': {}
}

# Moonshot 官方
MOONSHOT_CONFIG = {
    'base_url': 'https://api.moonshot.cn/v1',
    'api_key': lambda: os.getenv('MOONSHOT_API_KEY'),
    # Moonshot/Kimi：通过特定模型名启用思考模式
    'model': 'kimi-k2-thinking',
    'timeout': 3600,
    'temperature': 1.0,
    'params': {}
}

# 字节跳动火山引擎
VOLCANO_CONFIG = {
    'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
    'api_key': lambda: os.getenv('ARK_API_KEY'),
    'model': 'deepseek-v3-2-251201',
    'timeout': 3600,
    'temperature': 1.0,
    # 火山引擎：通过 extra_body.enable_thinking 开启深度思考
    'params': {
        'extra_body': {
            'enable_thinking': True
        }
    }
}

# 阿里云百炼 DashScope
DASHSCOPE_CONFIG = {
    'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'api_key': lambda: os.getenv('DASHSCOPE_API_KEY'),
    'model': 'deepseek-v3.2',
    'timeout': 3600,
    'temperature': 1.0,
    # 阿里云百炼：通过 extra_body.enable_thinking 开启深度思考
    'params': {
        'extra_body': {
            'enable_thinking': True
        }
    }
}

# OpenRouter 官方
OPENROUTER_CONFIG = {
    'base_url': 'https://openrouter.ai/api/v1',
    'api_key': lambda: os.getenv('OPENROUTER_API_KEY'),
    'model': 'openai/gpt-5',
    'timeout': 3600,
    'temperature': 1.0,
    # OpenRouter：通过 extra_body.reasoning.effort 调整思考强度
    'params': {
        'extra_body': {
            'reasoning': {
                'effort': 'high'
            }
        }
    }
}

# 自定义兼容 OpenAI API 格式的 LLM 服务，例如从淘宝黑市购买的服务
CUSTOM_LLM_CONFIG = {
    'base_url': 'https://api.gpts.vin/v1',
    'api_key': lambda: os.getenv('YUBOAR_API_KEY'),
    'model': 'gpt-5',
    'timeout': 3600,
    'temperature': 1.0,
    # 统一承载“思考/推理”相关的附加参数
    'params': {
        # OpenAI 兼容格式调整思考强度的方式：直接传入 reasoning_effort
        'reasoning_effort': 'high'
    }
}

## 在这里设置 AlphaSolve 使用的 LLM 配置
SOLVER_CONFIG = DEEPSEEK_CONFIG
VERIFIER_CONFIG = DEEPSEEK_CONFIG
REFINER_CONFIG = DEEPSEEK_CONFIG
SUMMARIZER_CONFIG = DEEPSEEK_CONFIG

class AlphaSolveConfig:

    SOLVER = 'solver'
    VERIFIER = 'verifier'
    REFINER = 'refiner'

    REFINER_CONFIG = DEEPSEEK_CONFIG
    REFINER_PROMPT_PATH='prompts/refiner.md'

    SOLVER_CONFIG = DEEPSEEK_CONFIG
    SOLVER_PROMPT_PATH='prompts/solver.md'

    VERIFIER_CONFIG = DEEPSEEK_CONFIG
    VERIFIER_PROMPT_PATH = 'prompts/verifier.md'

    SUMMARIZER_MODEL = 'gemini-3-pro'  
    SUMMARIZER_PROMPT_PATH = 'prompts/refiner.md'

    HINT = 'hint'

    VERIFIER_SCALING_FACTOR = 1
    VERIFY_AND_REFINE_ROUND = 'verifier_refiner_round'
    TOTAL_SOLVER_ROUND = 'solver_round'


    SHARED_CONTEXT = 'shared_context'
    CURRENT_CONJECTURE = 'corrent_conjecture'

    ## 各种状态, 用来管理整个 agent system 的状态迁移
    CONJECTURE_GENERATED  = 'conjecture_generated'

    ## used by verifier
    CONJECTURE_UNVERIFIED = 'conjecture_unverified'
    CONJECTURE_VERIFIED  = 'conjecture_verified'
    DONE = 'done'

    ## used by refiner
    REFINE_SUCCESS = 'refined_success'
    CONJECTURE_WRONG = 'conjecture_wrong'

    ## used by all
    EXIT_ON_EXAUSTED = 'exit_on_exausted'    
    EXIT_ON_ERROR = 'exit_on_error'
    EXIT_ON_SUCCESS = 'exit_on_success'    


    ## 内部状态
    NORMAL = 'normal'
    VERIFIER_EXAUSTED = 'verifier_exausted'
    SOLVER_EXAUSTED = 'solver_exausted'


