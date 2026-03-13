# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""VerboseDisplay — concise, informative terminal output during optimization.

Prints a clear summary of each phase in the GEPA loop as events fire.
Designed for readability: shows what matters (scores, decisions, prompts,
lessons) without dumping raw data structures. Full details go to the
ResearchLogger file logs.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from gepa.core.callbacks import (
    BudgetUpdatedEvent,
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    CandidateSelectedEvent,
    EvaluationEndEvent,
    EvaluationSkippedEvent,
    IterationEndEvent,
    IterationStartEvent,
    LessonGeneratedEvent,
    MemoryQueriedEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ProposalEndEvent,
    ProposalTraceEvent,
    ReflectiveDatasetBuiltEvent,
)

# ANSI helpers (degrade gracefully if piped)
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


class VerboseDisplay:
    """Callback that prints a concise, readable summary of every optimization step."""

    def __init__(self, max_prompt_len: int = 300, color: bool = True) -> None:
        self._iteration: int = 0
        self._pre_scores: list[float] | None = None
        self._max_prompt_len: int = max_prompt_len
        self._iter_start: float = 0.0
        self._opt_start: float = 0.0
        self._total_accepted: int = 0
        self._total_iterations: int = 0
        self._best_score: float = 0.0
        self._budget_used: int = 0
        self._budget_remaining: int | None = None
        if color:
            self._b, self._d, self._g, self._r = _BOLD, _DIM, _GREEN, _RED
            self._y, self._c, self._x = _YELLOW, _CYAN, _RESET
        else:
            self._b = self._d = self._g = self._r = self._y = self._c = self._x = ""

    # -- helpers --

    def _trunc(self, text: str, limit: int | None = None) -> str:
        n = limit or self._max_prompt_len
        text = text.strip()
        if len(text) <= n:
            return text
        return text[:n] + f" {self._d}[...+{len(text) - n} chars]{self._x}"

    def _w(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _bar(self, correct: int, total: int, width: int = 20) -> str:
        """Compact score bar: [||||....] 3/5"""
        filled = round(correct / total * width) if total > 0 else 0
        return f"[{'|' * filled}{'.' * (width - filled)}] {correct}/{total}"

    def _score_color(self, score: float) -> str:
        if score >= 0.7:
            return self._g
        elif score >= 0.4:
            return self._y
        return self._r

    # -- event handlers --

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._opt_start = time.monotonic()

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._iteration = event["iteration"]
        self._pre_scores = None
        self._iter_start = time.monotonic()
        elapsed = time.monotonic() - self._opt_start
        m, s = divmod(int(elapsed), 60)
        self._w(
            f"\n{self._b}{'=' * 70}{self._x}\n"
            f"{self._b}  ITERATION {self._iteration}{self._x}"
            f"  {self._d}[{m:02d}m{s:02d}s elapsed]{self._x}\n"
            f"{self._b}{'=' * 70}{self._x}\n"
        )

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        score = event["score"]
        sc = self._score_color(score)
        self._w(
            f"\n{self._b}  PARENT{self._x}  "
            f"candidate #{event['candidate_idx']}  "
            f"score: {sc}{score:.4f}{self._x}\n"
        )
        for comp, text in event["candidate"].items():
            self._w(f"  {self._d}[{comp}]{self._x} {self._trunc(text)}\n")

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        scores = event["scores"]
        if not scores:
            return
        correct = sum(1 for s in scores if s >= 1.0)
        total = len(scores)
        avg = sum(scores) / total

        if event["capture_traces"]:
            # Pre-mutation eval on minibatch
            self._pre_scores = list(scores)
            sc = self._score_color(avg)
            self._w(
                f"\n{self._b}  PRE-MUTATION EVAL{self._x}  "
                f"{self._bar(correct, total)}  "
                f"avg: {sc}{avg:.4f}{self._x}\n"
            )
            # Show per-example: score + answer (from outputs if available)
            outputs = event.get("outputs", [])
            for i, s in enumerate(scores):
                mark = f"{self._g}+{self._x}" if s >= 1.0 else f"{self._r}-{self._x}"
                answer = ""
                if i < len(outputs):
                    out = outputs[i]
                    # Extract answer from side_info dict if possible
                    if isinstance(out, dict):
                        answer = f"  ans={out.get('output', '?')}  correct={out.get('Feedback', '')[:40]}"
                    elif isinstance(out, tuple) and len(out) >= 2:
                        side = out[1] if isinstance(out[1], dict) else (out[2] if len(out) > 2 and isinstance(out[2], dict) else {})
                        answer = f"  ans={side.get('output', '?')}"
                self._w(f"    [{mark}] ex {i}: {s:.0f}{answer}\n")
        else:
            # Post-mutation eval on minibatch
            sc = self._score_color(avg)
            self._w(
                f"\n{self._b}  POST-MUTATION EVAL{self._x}  "
                f"{self._bar(correct, total)}  "
                f"avg: {sc}{avg:.4f}{self._x}"
            )
            if self._pre_scores:
                pre_avg = sum(self._pre_scores) / len(self._pre_scores)
                delta = avg - pre_avg
                dc = self._g if delta > 0 else (self._r if delta < 0 else self._d)
                self._w(f"  {dc}(delta: {delta:+.4f}){self._x}")
            self._w("\n")

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        dataset: dict[str, list[dict[str, Any]]] = event["dataset"]
        for comp, examples in dataset.items():
            n_fail = sum(1 for ex in examples if float(ex.get("score", 1)) < 1.0)
            self._w(
                f"\n{self._b}  FEEDBACK{self._x}  "
                f"{self._d}[{comp}]{self._x}  "
                f"{len(examples)} examples, {self._r}{n_fail} failed{self._x}\n"
            )
            # Show only failed examples' feedback (the useful signal)
            for j, ex in enumerate(examples):
                s = float(ex.get("score", 0))
                mark = f"{self._g}+{self._x}" if s >= 1.0 else f"{self._r}-{self._x}"
                fb = str(ex.get("Feedback") or ex.get("feedback") or ex.get("execution_feedback") or "")
                fb_short = self._trunc(fb, 120) if fb else ""
                out = str(ex.get("output", ""))[:30]
                self._w(f"    [{mark}] ex {j}: ans={out}  {fb_short}\n")

    def on_memory_queried(self, event: MemoryQueriedEvent) -> None:
        n = len(event["entries_returned"])
        if n > 0:
            self._w(
                f"\n{self._b}  MEMORY{self._x}  "
                f"{self._c}{n} entries injected{self._x}  "
                f"{self._d}({event['formatted_text_length']} chars){self._x}\n"
            )
        else:
            self._w(f"\n{self._b}  MEMORY{self._x}  {self._d}(empty){self._x}\n")

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        latency = event["latency_ms"]
        self._w(
            f"\n{self._b}  REFLECTOR{self._x}  "
            f"{self._d}[{event['component_name']}]{self._x}  "
            f"model={event['model_id']}  "
            f"latency={latency:.0f}ms  "
            f"memory={'yes' if event['memory_was_injected'] else 'no'}\n"
        )
        # Show the proposed text (extracted instruction), not the raw chain-of-thought
        instruction = event["extracted_instruction"].strip()
        self._w(f"  {self._d}proposed:{self._x} {self._trunc(instruction)}\n")

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        self._w(f"\n{self._b}  NEW PROMPT{self._x}\n")
        for comp, text in event["new_instructions"].items():
            self._w(f"  {self._d}[{comp}]{self._x} {self._trunc(text)}\n")

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        self._total_accepted += 1
        score = event["new_score"]
        if score > self._best_score:
            self._best_score = score
        self._w(
            f"\n  {self._g}{self._b}>>> ACCEPTED{self._x}  "
            f"candidate #{event['new_candidate_idx']}  "
            f"score: {self._g}{score:.4f}{self._x}\n"
        )

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        self._w(
            f"\n  {self._r}{self._b}>>> REJECTED{self._x}  "
            f"old={event['old_score']:.4f} vs new={event['new_score']:.4f}  "
            f"{self._d}({event['reason']}){self._x}\n"
        )

    def on_lesson_generated(self, event: LessonGeneratedEvent) -> None:
        delta = event["score_after"] - event["score_before"]
        dc = self._g if delta > 0 else (self._r if delta < 0 else self._d)
        status = f"{self._g}accepted{self._x}" if event["accepted"] else f"{self._r}rejected{self._x}"
        self._w(
            f"\n{self._b}  LESSON{self._x}  "
            f"{dc}{delta:+.2f}{self._x}  {status}"
        )
        if event["fallback_used"]:
            self._w(f"  {self._y}(fallback){self._x}")
        self._w("\n")
        if event["intent"]:
            self._w(f"    Intent : {event['intent']}\n")
        if event["lesson"]:
            self._w(f"    Lesson : {event['lesson']}\n")
        cats_ok = event["categories_succeeded"]
        cats_fail = event["categories_failed"]
        if cats_ok:
            self._w(f"    Strong : {', '.join(cats_ok)}\n")
        if cats_fail:
            self._w(f"    Weak   : {', '.join(cats_fail)}\n")

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        self._w(f"\n  {self._y}EVAL SKIPPED{self._x}  {event['reason']}\n")

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        self._budget_used = event["metric_calls_used"]
        self._budget_remaining = event["metric_calls_remaining"]

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._total_iterations += 1
        dt = time.monotonic() - self._iter_start
        accept_rate = self._total_accepted / self._total_iterations * 100

        budget_str = f"{self._budget_used}"
        if self._budget_remaining is not None:
            budget_str += f"/{self._budget_used + self._budget_remaining}"

        status = f"{self._g}accepted{self._x}" if event["proposal_accepted"] else f"{self._r}rejected{self._x}"
        self._w(
            f"\n{self._d}  iter {self._iteration} done in {dt:.1f}s  "
            f"{status}  "
            f"best={self._best_score:.4f}  "
            f"accept={self._total_accepted}/{self._total_iterations} ({accept_rate:.0f}%)  "
            f"budget={budget_str}{self._x}\n"
            f"{self._b}{'=' * 70}{self._x}\n"
        )

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        total_time = time.monotonic() - self._opt_start
        m, s = divmod(int(total_time), 60)
        self._w(
            f"\n{self._b}{'=' * 70}{self._x}\n"
            f"{self._b}  OPTIMIZATION COMPLETE{self._x}  "
            f"{event['total_iterations']} iterations  "
            f"{event['total_metric_calls']} evals  "
            f"{m:02d}m{s:02d}s\n"
            f"  Best: candidate #{event['best_candidate_idx']}  "
            f"accept rate: {self._total_accepted}/{event['total_iterations']}\n"
            f"{self._b}{'=' * 70}{self._x}\n\n"
        )
