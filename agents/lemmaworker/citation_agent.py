from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from agents.shared_context import Lemma
from utils.logger import Logger


@dataclass
class CitationInput:
    candidate_lemma: Lemma
    verified_context: List[Lemma]


@dataclass
class CitationOutput:
    dependencies: List[int]
    done: bool


class CitationAgent:
    def __init__(self, logger: Logger):
        self.logger = logger
        # 只匹配整数形式的 Lemma 引用
        self.lemma_patterns = [
            re.compile(r'Lemma\s+(\d+)', re.IGNORECASE),  # Lemma 1, Lemma 35, 等
            re.compile(r'Lemma-(\d+)', re.IGNORECASE),    # Lemma-41, Lemma-24, 等
        ]

    def cite(self, input: CitationInput) -> CitationOutput:
        proof = input.candidate_lemma.get('proof', '')
        
        if not proof:
            self.logger.log_print(
                "event=citation_agent_no_proof",
                module="citation_agent",
                level="WARNING"
            )
            return CitationOutput(dependencies=[], done=False)

        # 从证明文本中提取所有 lemma 引用
        found_lemma_ids = set()
        
        for pattern in self.lemma_patterns:
            matches = pattern.findall(proof)
            for match in matches:
                try:
                    lemma_id = int(match)
                    found_lemma_ids.add(lemma_id)
                except (ValueError, TypeError):
                    continue

        # 验证提取的 lemma ID 是否在 verified_context 范围内
        valid_dependencies = []
        max_lemma_id = len(input.verified_context) - 1 if input.verified_context else -1
        
        for lemma_id in found_lemma_ids:
            if 0 <= lemma_id <= max_lemma_id:
                valid_dependencies.append(lemma_id)
            else:
                self.logger.log_print(
                    f"event=citation_agent_invalid_lemma_id lemma_id={lemma_id} max_valid_id={max_lemma_id}",
                    module="citation_agent",
                    level="WARNING"
                )

        # 排序依赖关系
        valid_dependencies.sort()

        self.logger.log_print(
            f"event=citation_agent_extracted dependencies={valid_dependencies}",
            module="citation_agent"
        )

        return CitationOutput(dependencies=valid_dependencies, done=False)


def create_citation_agent(logger: Logger) -> CitationAgent:
    return CitationAgent(logger=logger)
