from __future__ import annotations

import os

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AlphaSolveConfig:
    WOLFRAM_AVAILABLE = True
    WOLFRAM_STATUS = "not_checked"

    # Provider presets — reference these by name in agents.yaml model_config
    DEEPSEEK_CONFIG = {
        "base_url": "https://api.deepseek.com",
        "api_key": lambda: os.getenv("DEEPSEEK_API_KEY"),
        "model": "deepseek-v4-flash",
        "timeout": 3600,
        "params": {},
    }
    DEEPSEEK_PRO_CONFIG = {
        "base_url": "https://api.deepseek.com",
        "api_key": lambda: os.getenv("DEEPSEEK_API_KEY"),
        "model": "deepseek-v4-pro",
        "timeout": 3600,
        "params": {"extra_body": {"reasoning": {"effort": "max"}}},
    }
    PARASAIL_CONFIG = {
        "base_url": "https://api.parasail.io/v1",
        "api_key": lambda: os.getenv("PARASAIL_API_KEY"),
        "model": "deepseek-ai/DeepSeek-V3.2",
        "timeout": 3600,
        "params": {"extra_body": {"enable_thinking": True}},
    }
    LONGCAT_CONFIG = {
        "base_url": "https://api.longcat.chat/openai",
        "api_key": lambda: os.getenv("LONGCAT_API_KEY"),
        "model": "LongCat-Flash-Thinking-2601",
        "timeout": 3600,
        "params": {},
    }
    MOONSHOT_CONFIG = {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": lambda: os.getenv("MOONSHOT_API_KEY"),
        "model": "kimi-k2-thinking",
        "timeout": 3600,
        "temperature": 1.0,
        "params": {},
    }
    VOLCANO_CONFIG = {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": lambda: os.getenv("ARK_API_KEY"),
        "model": "doubao-seed-2-0-pro-260215",
        "timeout": 180,
        "params": {"extra_body": {"thinking": {"type": "enabled"}}},
    }
    VOLCANO_DS_CONFIG = {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": lambda: os.getenv("ARK_API_KEY"),
        "model": "deepseek-v3-2-251201",
        "timeout": 180,
        "params": {"extra_body": {"thinking": {"type": "enabled"}}},
    }
    DASHSCOPE_CONFIG = {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": lambda: os.getenv("DASHSCOPE_API_KEY"),
        "model": "deepseek-v3.2",
        "timeout": 3600,
        "temperature": 1.0,
        "params": {"extra_body": {"enable_thinking": True}},
    }
    MIMO_CONFIG = {
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key": lambda: os.getenv("MIMO_API_KEY"),
        "model": "mimo-v2-flash",
        "timeout": 3600,
        "temperature": 1.0,
        "params": {"extra_body": {"thinking": {"type": "enabled"}}},
    }
    OPENROUTER_CONFIG = {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": lambda: os.getenv("OPENROUTER_API_KEY"),
        "model": "google/gemini-2.5-flash",
        "timeout": 3600,
        "params": {"extra_body": {"reasoning": {"effort": "high"}}},
    }

    # Default model configs referenced by agents.yaml
    GENERATOR_CONFIG = {**DEEPSEEK_PRO_CONFIG}
    VERIFIER_CONFIG = {**DEEPSEEK_PRO_CONFIG}
    REVISER_CONFIG = {**DEEPSEEK_PRO_CONFIG}
    COMPUTE_SUBAGENT_CONFIG = {**DEEPSEEK_CONFIG}
    PROOF_SUBAGENT_CONFIG = {**DEEPSEEK_CONFIG}
    ORCHESTRATOR_CONFIG = {**DEEPSEEK_PRO_CONFIG}

    CHECK_IS_THEOREM_TIMES = 5

    @classmethod
    def configure_wolfram_availability(cls, available: bool, reason: str = "") -> None:
        cls.WOLFRAM_AVAILABLE = bool(available)
        cls.WOLFRAM_STATUS = reason or ("available" if available else "unavailable")
