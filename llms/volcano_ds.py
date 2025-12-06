import os

from config.agent_config import LLMConfig
from llms.utils import LLMClient

class VolcanoDeepSeekClient(LLMClient):

    def __init__(self):
        super(VolcanoDeepSeekClient, self).__init__(LLMConfig.VOLCANO_DS_URL, LLMConfig.VOLCANO_DS_ARK_API_KEY, LLMConfig.VOLCANO_DS_ENDPOINT, LLMConfig.VOLCANO_DS_TIMEOUT)

