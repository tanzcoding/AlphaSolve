from typing import Dict, List
from openai import OpenAI
#from config.agent_config import DEEPSEEK_CONFIG, MOONSHOT_CONFIG, VOLCANO_CONFIG, OPENROUTER_CONFIG, CUSTOM_LLM_CONFIG

class LLMClient:
    def __init__(self, config: Dict):
        """
        初始化 LLM 客户端
        
        Args:
            config: 包含供应商配置的字典，包括 base_url, api_key, model 等
        """
        self.config = config

        def _resolve(v):
            return v() if callable(v) else v

        self.base_url = _resolve(config.get('base_url'))
        self.api_key = _resolve(config.get('api_key'))
        self.model = _resolve(config.get('model'))
        self.timeout = _resolve(config.get('timeout', 3600))
        self.temperature = _resolve(config.get('temperature', 1.0))
        self._static_params = _resolve(config.get('params', {})) or {}
        
        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )
    
    def _get_model_params(self) -> Dict:
        """直接从配置中读取参数，统一在 params 中定义“思考/推理”相关开关"""
        params: Dict = {
            "model": self.model,
            "temperature": self.temperature,
        }
        # 合并静态 params（包含 extra_body / reasoning_effort 等供应商差异化键）
        params.update(self._static_params)
        return params
    
    def get_result(self, messages: List[Dict], print_to_console: bool = False) -> str:
        """
        获取 LLM 的回复，始终使用流式输出，以防止网络超时
        
        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            print_to_console: 是否将流式输出打印到控制台
            
        Returns:
            answer_content: 最终回答内容
            reasoning_content: 完整思考过程内容
        """
        model_params = self._get_model_params()
        
        # 处理特殊参数
        extra_body = model_params.pop("extra_body", None)
        
        # 创建流式请求
        completion = self.client.chat.completions.create(
            messages=messages,
            stream=True,
            **model_params,
            **({"extra_body": extra_body} if extra_body else {})
        )

        reasoning_content = ""  # 完整思考过程
        answer_content = ""  # 完整回复
        is_answering = False  # 是否进入回复阶段

        if print_to_console:
            print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                print("此模型不返回思维链内容，仅打印最终回答\n")
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    if not is_answering:
                        print(delta.reasoning_content, end="", flush=True)
                    reasoning_content += delta.reasoning_content
                
                # 收到content，开始进行回复
                if hasattr(delta, "content") and delta.content:
                    if not is_answering:
                        print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                        is_answering = True
                    print(delta.content, end="", flush=True)
                    answer_content += delta.content    
        else:
            print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    if not is_answering:
                        print(delta.reasoning_content, end="", flush=True)
                    reasoning_content += delta.reasoning_content
                
                # 收到content，开始进行回复
                if hasattr(delta, "content") and delta.content:
                    if not is_answering:
                        print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                        is_answering = True
                    print(delta.content, end="", flush=True)
                    answer_content += delta.content

        return answer_content, reasoning_content


# get_result函数的使用示例
if __name__ == "__main__":
    import os
    CONFIG = {
        'base_url': 'https://api.gpts.vin/v1',
        'api_key': lambda: os.getenv('YUBOAR_API_KEY'),
        'model': 'gpt-5',
        'timeout': 3600,
        'temperature': 1.0,
        'params': {
            'reasoning_effort': 'high'
            }
        }
    print("测试:")
    llm = LLMClient(CONFIG)
    messages = [{"role": "user", "content": "你好！"}]
    response = llm.get_result(messages, print_to_console=True)
    
