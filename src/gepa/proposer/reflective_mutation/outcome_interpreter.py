# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

from gepa.core.callbacks import (
    OIEvictionSummaryEvent,
    OutcomeInterpreterCallEvent,
    notify_callbacks,
)
from gepa.proposer.reflective_mutation.base import LanguageModel
from gepa.proposer.reflective_mutation.memory_tree import OutcomeDescription

_logger = logging.getLogger(__name__)

_OI_PROMPT_TEMPLATE = """\
You are analyzing the results of running a system prompt on problems.
For each problem below, describe WHAT happened in the model's reasoning.
Focus on observations, not advice. Do not suggest fixes.

System prompt used:
<prompt>
{candidate_text}
</prompt>

{problem_blocks}

For each problem, respond with exactly this format:

### Problem 1
Question: [brief identifier — first line or key phrase of the problem]
Result: correct/incorrect
Observation: [1-3 sentences describing what the model actually did]
Error type: arithmetic | logic | setup | interpretation | none

### Problem 2
...
"""

_EVICTION_SUMMARY_TEMPLATE = """\
Existing summary (may be empty):
{existing_summary}

New observations to integrate:
{observations}

Produce a compressed summary (max 3 sentences) capturing recurring patterns, \
problem types where the model succeeds/fails, and notable trends.
Output ONLY the summary text."""


class OutcomeInterpreter:
    """Batched LLM call that produces per-question outcome descriptions."""

    def __init__(self, lm: LanguageModel, callbacks: Any = None) -> None:
        self.lm = lm
        self.callbacks = callbacks or []

    def interpret(
        self,
        candidate: dict[str, str],
        reflective_records: list[dict[str, Any]],
        scores: list[float],
        iteration: int,
        node_id: int = -1,
    ) -> list[OutcomeDescription]:
        """Run OI on a batch of evaluation records, return one OutcomeDescription per record."""
        if not reflective_records:
            return []

        candidate_text = "\n---\n".join(f"[{k}]\n{v}" for k, v in candidate.items())

        problem_blocks: list[str] = []
        for idx, record in enumerate(reflective_records):
            block = f"### Problem {idx + 1}\n"
            for key in ("Inputs", "Generated Outputs", "Feedback"):
                val = record.get(key, "")
                block += f"**{key}:**\n{val}\n\n"
            block += f"**Score:** {scores[idx] if idx < len(scores) else 'N/A'}\n"
            problem_blocks.append(block)

        prompt = _OI_PROMPT_TEMPLATE.format(
            candidate_text=candidate_text,
            problem_blocks="\n".join(problem_blocks),
        )

        fallback_used = False
        raw_response = ""
        try:
            raw_response = self.lm(prompt)
            outcomes = self._parse_response(raw_response, reflective_records, scores, iteration)
        except Exception:
            _logger.exception("OI LLM call failed; returning fallback descriptions")
            fallback_used = True
            outcomes = self._fallbacks(reflective_records, scores, iteration)

        notify_callbacks(
            self.callbacks,
            "on_outcome_interpreter_call",
            OutcomeInterpreterCallEvent(
                type="outcome_interpreter_call",
                iteration=iteration,
                node_id=node_id,
                candidate=candidate,
                oi_prompt=prompt,
                oi_raw_response=raw_response,
                outcomes=[dataclasses.asdict(o) for o in outcomes],
                num_records=len(reflective_records),
                fallback_used=fallback_used,
            ),
        )

        return outcomes

    def summarize_for_eviction(
        self,
        existing_summary: str,
        evicted: list[OutcomeDescription],
        *,
        node_id: int = -1,
        iteration: int = -1,
    ) -> str:
        """Compress evicted outcomes into the node's running summary."""
        return self._summarize(
            existing_summary,
            evicted,
            node_id=node_id,
            iteration=iteration,
            summary_type="node",
        )

    def summarize_for_global(
        self,
        existing_global: str,
        evicted: list[OutcomeDescription],
        *,
        node_id: int = -1,
        iteration: int = -1,
    ) -> str:
        """Update the global running summary with evicted outcomes."""
        return self._summarize(
            existing_global,
            evicted,
            node_id=node_id,
            iteration=iteration,
            summary_type="global",
        )

    def _summarize(
        self,
        existing_summary: str,
        outcomes: list[OutcomeDescription],
        *,
        node_id: int = -1,
        iteration: int = -1,
        summary_type: str = "unknown",
    ) -> str:
        observations = "\n".join(
            f"- [{o.result}] {o.observation} (error: {o.error_type or 'none'})" for o in outcomes
        )
        prompt = _EVICTION_SUMMARY_TEMPLATE.format(
            existing_summary=existing_summary or "(none)",
            observations=observations,
        )
        response = ""
        new_summary = existing_summary
        try:
            response = self.lm(prompt).strip()
            new_summary = response
        except Exception:
            _logger.exception("Eviction summary LLM call failed; keeping existing summary")

        notify_callbacks(
            self.callbacks,
            "on_oi_eviction_summary",
            OIEvictionSummaryEvent(
                type="oi_eviction_summary",
                iteration=iteration,
                node_id=node_id,
                summary_type=summary_type,
                prompt=prompt,
                response=response,
                existing_summary=existing_summary,
                evicted_count=len(outcomes),
                new_summary=new_summary,
            ),
        )

        return new_summary

    def _parse_response(
        self,
        raw: str,
        records: list[dict[str, Any]],
        scores: list[float],
        iteration: int,
    ) -> list[OutcomeDescription]:
        """Parse the OI response into OutcomeDescriptions."""
        # Split on "### Problem N"
        blocks = re.split(r"###\s*Problem\s+\d+", raw)
        # First element is text before the first header (usually empty)
        blocks = [b.strip() for b in blocks[1:] if b.strip()]

        results: list[OutcomeDescription] = []
        for idx, record in enumerate(records):
            fallback_question = str(record.get("Inputs", ""))[:80]
            score = scores[idx] if idx < len(scores) else 0.0

            if idx < len(blocks):
                block = blocks[idx]
                question_summary = self._extract_field(block, "Question") or fallback_question
                result_str = self._extract_field(block, "Result") or ("correct" if score > 0.5 else "incorrect")
                observation = self._extract_field(block, "Observation") or "(OI parse failed)"
                error_type = self._extract_field(block, "Error type")
            else:
                question_summary = fallback_question
                result_str = "correct" if score > 0.5 else "incorrect"
                observation = "(OI parse failed — missing block)"
                error_type = None

            results.append(
                OutcomeDescription(
                    question_summary=question_summary,
                    result=result_str,
                    observation=observation,
                    error_type=error_type,
                    score=score,
                    iteration=iteration,
                )
            )
        return results

    @staticmethod
    def _extract_field(block: str, field_name: str) -> str | None:
        """Extract a field value from a parsed block."""
        pattern = rf"^\s*{re.escape(field_name)}\s*:\s*(.+)"
        match = re.search(pattern, block, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _fallbacks(
        records: list[dict[str, Any]], scores: list[float], iteration: int
    ) -> list[OutcomeDescription]:
        """Generate fallback descriptions when the LLM call fails."""
        results: list[OutcomeDescription] = []
        for idx, record in enumerate(records):
            score = scores[idx] if idx < len(scores) else 0.0
            results.append(
                OutcomeDescription(
                    question_summary=str(record.get("Inputs", ""))[:80],
                    result="correct" if score > 0.5 else "incorrect",
                    observation="(OI unavailable)",
                    error_type=None,
                    score=score,
                    iteration=iteration,
                )
            )
        return results
