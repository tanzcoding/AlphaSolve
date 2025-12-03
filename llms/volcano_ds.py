import os
from openai import OpenAI
from config.agent_config import LLMConfig

client = OpenAI(
    base_url = LLMConfig.VOLCANO_DS_URL,
    api_key = LLMConfig.VOLCANO_DS_ARK_API_KEY,
)


def get_result(system_prompt, user_prompt): ## 第一种场景, 直接问结果

    messages = [ ]
    if system_prompt:
        system_prompt_map = {'role': 'system', 'content': system_prompt}
        messages.append(system_prompt_map)
    if user_prompt:
        user_prompt_map = {'role': 'user', 'content': user_prompt}
        messages.append(user_prompt_map)
    
    completion = client.chat.completions.create(
        model = LLMConfig.VOLCANO_DS_ENDPOINT,
        messages = messages,
        timeout = LLMConfig.VOLCANO_DS_TIMEOUT,
    )
 
    message = completion.choices[0].message

    return message.content, message.reasoning_content
    



def get_result_2(system_prompt, user_prompt, assistant_resp, user_prompt_next): ## 第一种场景, 追加一条结果, 多用在verifier/refiner上

    messages = [ ]
  
    if system_prompt:
        system_prompt_map = {'role': 'system', 'content': system_prompt}
        messages.append(system_prompt_map)
    if user_prompt:
        user_prompt_map = {'role': 'user', 'content': user_prompt}
        messages.append(user_prompt_map)
    if assistant_resp:
        assistant_resp_map = {'role': 'assistant', 'content': assistant_resp}
        messages.append(assistant_resp_map)
    if user_prompt_next:
        user_prompt_next_map = {'role': 'user', 'content': user_prompt_next}
        messages.append(user_prompt_next_map)
  
  
    completion = client.chat.completions.create(
        model = LLMConfig.VOLCANO_DS_ENDPOINT, 
        messages = messages,
        timeout = LLMConfig.VOLCANO_DS_TIMEOUT,
    )
 
    message = completion.choices[0].message

    return message.content, message.reasoning_content
       
    
