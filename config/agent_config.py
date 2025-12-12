import os

class AlphaSolveConfig:

    SOLVER = 'solver'

    REFINE_MODEL='deepseek-reasoner'
    REFINE_PROMPT_PATH='prompts/refiner.md'

    SOLVER_MODEL='deepseek-reasoner'
    SOLVER_PROMPT_PATH='prompts/solver.md'

    VERIFIER_MODEL = 'deepseek-reasoner'
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


class LLMConfig:

    VOLCANO_DS_ARK_API_KEY=os.getenv("ARK_API_KEY")
    VOLCANO_DS_ENDPOINT=''
    VOLCANO_DS_URL = 'https://ark.cn-beijing.volces.com/api/v3'
    VOLCANO_DS_TIMEOUT = 3600

    
    KIMI_API_KEY = os.getenv("MOONSHOT_API_KEY")
    KIMI_URL = 'https://api.moonshot.cn/v1'
    KIMI_TIMEOUT = 3600
    KIMI_MODEL = 'kimi-k2-thinking'


    V32_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    V32_URL = 'https://api.deepseek.com'
    V32_TIMEOUT = 3600
    V32_MODEL = 'deepseek-reasoner'


