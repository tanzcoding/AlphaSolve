from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphasolve.agents.shared_context import Lemma
from alphasolve.agents.lemmaworker import (
    LemmaWorkerContext,
    GenerateInput,
    VerifyInput,
    ReviseInput,
    CitationInput,
    create_generator_component,
    create_verifier_component,
    create_reviser_component,
    create_citation_agent,
)
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.utils.logger import Logger


@dataclass
class LemmaWorkerResult:
    lemma: Lemma
    status: Literal["verified", "rejected"]
    is_theorem: bool
    dependencies: list[int]


class LemmaWorker:
    def __init__(self, *, logger: Logger, tool_executor=None, print_to_console: bool = False):
        self.logger = logger
        self.tool_executor = tool_executor
        self.print_to_console = print_to_console

        self.generator = create_generator_component(
            prompt_file_path=AlphaSolveConfig.GENERATOR_PROMPT_PATH,
            logger=self.logger,
            tool_executor=self.tool_executor,
        )
        self.citation_agent = create_citation_agent(logger=self.logger)
        self.verifier = create_verifier_component(
            prompt_file_path=AlphaSolveConfig.VERIFIER_PROMPT_PATH,
            logger=self.logger,
            tool_executor=self.tool_executor,
        )
        self.reviser = create_reviser_component(
            prompt_file_path=AlphaSolveConfig.REVISER_PROMPT_PATH,
            logger=self.logger,
            tool_executor=self.tool_executor,
        )

    def run(self, ctx: LemmaWorkerContext) -> LemmaWorkerResult:
        self.logger.log_print(
            f"event=lemma_worker_start worker_id={ctx.worker_id} verified_ctx_size={len(ctx.verified_snapshot)} print_to_console={self.print_to_console}",
            module="lemma_worker",
        )

        gen_out = self.generator.generate(
            GenerateInput(
                problem=ctx.problem,
                hint=ctx.hint,
                verified_context=ctx.verified_snapshot,
                remaining_lemma_quota=ctx.remaining_capacity,
            )
        )

        if not gen_out.lemma:
            rejected = {
                "statement": "",
                "proof": "",
                "dependencies": [],
                "status": "rejected",
                "review": "generator_failed_to_produce_valid_lemma",
                "cot": None,
                "is_theorem": False,
                "history_messages": [],
                "verify_round": 0,
            }
            return LemmaWorkerResult(lemma=rejected, status="rejected", is_theorem=False, dependencies=[])

        lemma = gen_out.lemma

        citation_out = self.citation_agent.cite(
            CitationInput(
                candidate_lemma=lemma,
                verified_context=ctx.verified_snapshot,
            )
        )
        lemma["dependencies"] = citation_out.dependencies

        while True:
            verify_out = self.verifier.verify(
                VerifyInput(
                    problem=ctx.problem,
                    verified_context=ctx.verified_snapshot,
                    candidate_lemma=lemma,
                )
            )

            lemma["verify_round"] = lemma.get("verify_round", 0) + 1
            lemma["review"] = verify_out.review
            lemma["cot"] = verify_out.cot

            if verify_out.valid:
                lemma["status"] = "verified"
                lemma["is_theorem"] = False
                self.logger.log_print(
                    f"event=lemma_worker_done worker_id={ctx.worker_id} status=verified is_theorem=False",
                    module="lemma_worker",
                )
                return LemmaWorkerResult(
                    lemma=lemma,
                    status="verified",
                    is_theorem=False,
                    dependencies=list(lemma.get("dependencies", [])),
                )

            if lemma.get("verify_round", 0) >= AlphaSolveConfig.MAX_VERIFY_AND_REFINE_ROUND:
                lemma["status"] = "rejected"
                self.logger.log_print(
                    f"event=lemma_worker_done worker_id={ctx.worker_id} status=rejected reason=verify_refine_exhausted",
                    module="lemma_worker",
                    level="WARNING",
                )
                return LemmaWorkerResult(
                    lemma=lemma,
                    status="rejected",
                    is_theorem=False,
                    dependencies=list(lemma.get("dependencies", [])),
                )

            revise_out = self.reviser.revise(
                ReviseInput(
                    problem=ctx.problem,
                    verified_context=ctx.verified_snapshot,
                    candidate_lemma=lemma,
                )
            )

            if revise_out.new_statement and len(revise_out.new_statement) > 5:
                lemma["statement"] = revise_out.new_statement
            if revise_out.new_proof and len(revise_out.new_proof) > 5:
                lemma["proof"] = revise_out.new_proof
                citation_out = self.citation_agent.cite(
                    CitationInput(
                        candidate_lemma=lemma,
                        verified_context=ctx.verified_snapshot,
                    )
                )
                lemma["dependencies"] = citation_out.dependencies
