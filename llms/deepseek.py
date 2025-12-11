import os

from config.agent_config import LLMConfig
from llms.utils import LLMClient

class DeepSeekClient(LLMClient):

    def __init__(self):
        super(DeepSeekClient, self).__init__(LLMConfig.V32_URL, LLMConfig.V32_API_KEY, LLMConfig.V32_MODEL, LLMConfig.V32_TIMEOUT)

