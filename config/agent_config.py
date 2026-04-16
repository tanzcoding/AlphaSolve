import os
from llms.tools import (
    PROOF_SUBAGENT_TOOL,
    COMPUTE_SUBAGENT_TOOL,
    GENERATOR_RESPONSE_FORMAT_REMINDER,
    REVISER_RESPONSE_FORMAT_REMINDER,
    PYTHON_TOOL,
    WOLFRAM_TOOL,
    MODIFY_STATEMENT_TOOL,
    MODIFY_PROOF_TOOL,
    READ_LEMMA_TOOL,
    READ_CURRENT_CONJECTURE_AGAIN_TOOL,
    READ_REVIEW_AGAIN_TOOL,
    COT_PROBE_TOOL,
    NUMERICAL_EXPERIMENT_SUBAGENT_TOOL,
)

# 统一的运行时 CONFIG（始终开启"思考/推理"能力，不考虑关闭）
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
    'params': {}
}

PARASAIL_CONFIG = {
    'base_url': 'https://api.parasail.io/v1',
    'api_key': lambda: os.getenv('PARASAIL_API_KEY'),
    'model': 'deepseek-ai/DeepSeek-V3.2',
    'timeout': 3600,
    'params': {
        'extra_body': {
            'enable_thinking': True
        }
    }
}

# LongCat 官方
LONGCAT_CONFIG = {
    'base_url': 'https://api.longcat.chat/openai',
    'api_key': lambda: os.getenv('LONGCAT_API_KEY'),
    # 通过特定模型名启用思考模式
    'model': 'LongCat-Flash-Thinking-2601',
    'timeout': 3600,
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
    'model': 'doubao-seed-2-0-pro-260215',
    'timeout': 180,
    # 火山引擎：通过 extra_body.thinking = enabled 开启深度思考
    'params': {
        'extra_body': {
            'thinking': {
                'type': 'enabled'
            }
        }
    }
}

# 字节跳动火山引擎的coding套餐
VOLCANO_CODING_CONFIG = {
    'base_url': 'https://ark.cn-beijing.volces.com/api/coding/v3',
    'api_key': lambda: os.getenv('ARK_API_KEY'),
    'model': 'ark-code-latest',
    'timeout': 3600,
    'max_tokens': 65536,
    'params': {
        'extra_body': {
            'thinking': {
                'type': 'enabled'
            }
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

# 小米 MIMO
MIMO_CONFIG = {
    'base_url': 'https://api.xiaomimimo.com/v1',
    'api_key': lambda: os.getenv('MIMO_API_KEY'),
    'model': 'mimo-v2-flash',
    'timeout': 3600,
    'temperature': 1.0,
    'top_p': 0.95,
    # 阿里云百炼：通过 extra_body.enable_thinking 开启深度思考
    'params': {
        'extra_body': {
            'thinking': {
                'type': 'enabled'
            }
        }
    }
}

# OpenRouter 官方
OPENROUTER_GPT_5_CONFIG = {
    'base_url': 'https://openrouter.ai/api/v1',
    'api_key': lambda: os.getenv('OPENROUTER_API_KEY'),
    'model': 'openai/gpt-5',
    'timeout': 3600,
    # OpenRouter：通过 extra_body.reasoning.effort 调整思考强度
    'params': {
        'extra_body': {
            'reasoning': {
                'effort': 'high'
            }
        }
    }
}

OPENROUTER_GEMINI_2_0_FLASH_CONFIG = {
    'base_url': 'https://openrouter.ai/api/v1',
    'api_key': lambda: os.getenv('OPENROUTER_API_KEY'),
    'model': 'google/gemini-2.5-flash',
    'timeout': 3600,
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
CUSTOM_LLM_CONFIG_1 = {
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

class AlphaSolveConfig:
    LOG_PATH = 'logs'
    PROGRESS_PATH = 'progress'

    GENERATOR = 'generator'
    VERIFIER = 'verifier'
    REVISER = 'reviser'

    ## 在这里设置 AlphaSolve 使用的 LLM 配置
    
    # Generator 可以使用 subagent，也可以阅读已有 lemma 的证明, COMPUTE_SUBAGENT_TOOL 得后面加回去
    GENERATOR_CONFIG = {
        #**MIMO_CONFIG,
        #**VOLCANO_CONFIG,
        **DEEPSEEK_CONFIG,
        ## 'tools': [PROOF_SUBAGENT_TOOL, READ_LEMMA_TOOL, GENERATOR_RESPONSE_FORMAT_REMINDER, NUMERICAL_EXPERIMENT_SUBAGENT_TOOL]
        'tools': [ NUMERICAL_EXPERIMENT_SUBAGENT_TOOL, PROOF_SUBAGENT_TOOL]
    }
    GENERATOR_PROMPT_PATH='prompts/generator.md'

    # Verifier 可以使用 subagent，可以再读一遍当前猜想及其证明，也可以阅读已有 lemma 的证明
    VERIFIER_CONFIG = {
        ## **MIMO_CONFIG,
        **DEEPSEEK_CONFIG, 
        'tools': [COMPUTE_SUBAGENT_TOOL, READ_LEMMA_TOOL, READ_CURRENT_CONJECTURE_AGAIN_TOOL]
    }
    VERIFIER_PROMPT_PATH = 'prompts/verifier.md'

    # Reviser 可以使用 subagent，可以阅读已有 lemma 的证明，还可以再读一遍当前猜想及其证明
    REVISER_CONFIG = {
        ## **MIMO_CONFIG,
        **DEEPSEEK_CONFIG,
        ##**VOLCANO_CONFIG,
        'tools': [PROOF_SUBAGENT_TOOL, COMPUTE_SUBAGENT_TOOL, READ_LEMMA_TOOL, READ_CURRENT_CONJECTURE_AGAIN_TOOL, READ_REVIEW_AGAIN_TOOL, REVISER_RESPONSE_FORMAT_REMINDER]
    }
    REVISER_PROMPT_PATH='prompts/reviser.md'

    EXTRACTOR_CONFIG = {
        ## **MIMO_CONFIG,
        ## **VOLCANO_CONFIG,
        **DEEPSEEK_CONFIG,
        'tools': [READ_LEMMA_TOOL, READ_CURRENT_CONJECTURE_AGAIN_TOOL, READ_REVIEW_AGAIN_TOOL, REVISER_RESPONSE_FORMAT_REMINDER]
    }
    EXTRACTOR_PROMPT_PATH='prompts/extractor.md'

    # Summarizer 不使用工具
    SUMMARIZER_CONFIG = {
        **DEEPSEEK_CONFIG,
        'tools': None
    }
    SUMMARIZER_PROMPT_PATH = 'prompts/summarizer.md'

    # Compute subagent 可以使用 Python 和 Wolfram
    COMPUTE_SUBAGENT_CONFIG = {
        ## **VOLCANO_CONFIG,
        **DEEPSEEK_CONFIG,
        'tools': [PYTHON_TOOL,WOLFRAM_TOOL, COT_PROBE_TOOL]
    }

    # Proof subagent 默认只允许递归调用 proof_subagent
    PROOF_SUBAGENT_CONFIG = {
        ## **VOLCANO_CONFIG,
        **DEEPSEEK_CONFIG,
        ## 'tools': [PROOF_SUBAGENT_TOOL, COT_PROBE_TOOL]
        'tools': [ COT_PROBE_TOOL]

    }

    PROOF_SUBAGENT_MAX_DEPTH = 1

    ORCHESTRATOR_CONFIG = {
        #**MIMO_CONFIG,
        **LONGCAT_CONFIG,
        'tools': [PROOF_SUBAGENT_TOOL, COMPUTE_SUBAGENT_TOOL, READ_LEMMA_TOOL, GENERATOR_RESPONSE_FORMAT_REMINDER]
    }
    ORCHESTRATOR_PROMPT_PATH=''


    VERIFIER_SCALING_FACTOR = 4
    # NOTE: shared schema keys are defined by SharedContext (single dict-like object).
    # Do NOT add shared-key constants here.

    ## 各种状态, 用来管理整个 agent system 的状态迁移
    CONJECTURE_GENERATED  = 'conjecture_generated'

    ## used by verifier
    CONJECTURE_UNVERIFIED = 'conjecture_unverified'
    CONJECTURE_VERIFIED  = 'conjecture_verified'
    DONE = 'done'

    ## used by reviser
    REFINE_SUCCESS = 'refined_success'
    CONJECTURE_WRONG = 'conjecture_wrong'

    ## used by all
    EXIT_ON_EXAUSTED = 'exit_on_exausted'    
    EXIT_ON_ERROR = 'exit_on_error'
    EXIT_ON_SUCCESS = 'exit_on_success'
    EXIT_ON_FAILURE = 'exit_on_failure'


    ## 内部状态
    NORMAL = 'normal'
    VERIFIER_EXAUSTED = 'verifier_exausted'
    GENERATOR_EXAUSTED = 'generator_exausted'

    ## 
    MAX_LEMMA_NUM = 30
    MAX_VERIFY_AND_REFINE_ROUND = 2
    MAX_WORKER_NUM = 5
    GENERATOR_MAX_RETRY = 3
    REVISER_MAX_RETRY = 2
    CHECK_IS_THEOREM_TIMES = 5

    # LLM API retry policy
    # - Used by llms/utils.py to automatically retry when the streamed response
    #   is interrupted (e.g. finish_reason == "length") to avoid the workflow hanging.
    MAX_API_RETRY = 8

    PROBLEM_PATH = 'problems/problem_1.md'
    STANDARD_SOLUTION_PATH = 'standard_solution.md'


    
