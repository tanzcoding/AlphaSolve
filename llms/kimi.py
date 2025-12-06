import os

from config.agent_config import LLMConfig
from llms.utils import LLMClient


class KimiClient(LLMClient):

    def __init__(self):
        super(KimiClient, self).__init__(LLMConfig.KIMI_URL, LLMConfig.KIMI_API_KEY, LLMConfig.KIMI_MODEL, LLMConfig.KIMI_TIMEOUT)

