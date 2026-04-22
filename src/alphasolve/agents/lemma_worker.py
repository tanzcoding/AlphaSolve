from __future__ import annotations

import asyncio
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
    def __init__(self, *, logger: Logger, execution_gateway=None, print_to_console: bool = False):
        self.logger = logger
        self.execution_gateway = execution_gateway
        self.print_to_console = print_to_console

        self.generator = create_generator_component(
            prompt_file_path=AlphaSolveConfig.GENERATOR_PROMPT_PATH,
            logger=self.logger,
            execution_gateway=self.execution_gateway,
        )
        self.citation_agent = create_citation_agent(logger=self.logger)
        self.verifier = create_verifier_component(
            prompt_file_path=AlphaSolveConfig.VERIFIER_PROMPT_PATH,
            logger=self.logger,
            execution_gateway=self.execution_gateway,
        )
        self.reviser = create_reviser_component(
            prompt_file_path=AlphaSolveConfig.REVISER_PROMPT_PATH,
            logger=self.logger,
            execution_gateway=self.execution_gateway,
        )

    def _render_phase(self, ctx: LemmaWorkerContext, phase: str, *, status: str = "running") -> None:
        renderer = getattr(self.logger, "console_renderer", None)
        if renderer is not None and self.print_to_console:
            renderer.update_phase(ctx.worker_id, phase, status=status)

    def run(self, ctx: LemmaWorkerContext) -> LemmaWorkerResult:
        self.logger.log_print(
            f"event=lemma_worker_start worker_id={ctx.worker_id} verified_ctx_size={len(ctx.verified_snapshot)} print_to_console={self.print_to_console}",
            module="lemma_worker",
        )

        self._render_phase(ctx, "generator")
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
            self._render_phase(ctx, "done", status="rejected")
            return LemmaWorkerResult(lemma=rejected, status="rejected", is_theorem=False, dependencies=[])

        lemma = gen_out.lemma

        self._render_phase(ctx, "citation")
        citation_out = self.citation_agent.cite(
            CitationInput(
                candidate_lemma=lemma,
                verified_context=ctx.verified_snapshot,
            )
        )
        lemma["dependencies"] = citation_out.dependencies

        while True:
            self._render_phase(ctx, f"verifier round {lemma.get('verify_round', 0) + 1}")
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
                self._render_phase(ctx, "done", status="verified")
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
                self._render_phase(ctx, "done", status="rejected")
                return LemmaWorkerResult(
                    lemma=lemma,
                    status="rejected",
                    is_theorem=False,
                    dependencies=list(lemma.get("dependencies", [])),
                )

            self._render_phase(ctx, f"reviser round {lemma.get('verify_round', 0)}")
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

    async def run_async(self, ctx: LemmaWorkerContext) -> LemmaWorkerResult:
        return await asyncio.to_thread(self.run, ctx)
