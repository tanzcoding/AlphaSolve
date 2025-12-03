

class AlphaSolveConfig:

    REFINE_MODEL='gemini-3-pro'
    REFINE_PROMPT_PATH='prompts/refiner.md'

    SOLVER_MODEL='gemini-3-pro'
    SOLVER_PROMPT_PATH='prompts/solver.md'

    VERIFIER_MODEL = 'gemini-3-pro'
    VERIFIER_PROMPT_PATH = 'prompts/verifier.md'

    VERIFY_TEST_TIME_ROUND = 3
    VERIFY_AND_REFINE_ROUND = 3

    TOTAL_SOLVER_ROUND = 3



class LLMConfig:
    VOLCANO_DS_ARK_API_KEY=''
    VOLCANO_DS_ENDPOINT=''
    VOLCANO_DS_URL = 'https://ark.cn-beijing.volces.com/api/v3'
    VOLCANO_DS_TIMEOUT = 3600
