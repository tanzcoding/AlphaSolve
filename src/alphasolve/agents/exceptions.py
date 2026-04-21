

class LLMFormatException(Exception): ## 对应第一类 LLM 错误, 没有遵循要求的格式, 导致毛也解析不出来, 这类错误频率在5% - 10% 
    pass

class LLMServiceException(Exception): ## 第二类 LLM 错误, 直接崩了, 啥也没返回
    pass

class IterationExaustedExeption(Exception): ## 第三类错误, 迭代爆炸了
    pass

