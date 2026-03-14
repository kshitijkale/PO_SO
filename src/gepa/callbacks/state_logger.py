# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""StateLogger — dumps complete intermediate state snapshots per iteration step.

Writes numbered JSON files into ``states/`` so a researcher can reconstruct
the exact data flow at every step within an iteration.
"""

from __future__ import annotations

import json
import os
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
    MemoryStateSnapshotEvent,
    MergeAcceptedEvent,
    MergeAttemptedEvent,
    MergeRejectedEvent,
    MinibatchSampledEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ParetoFrontUpdatedEvent,
    ProposalEndEvent,
    ProposalStartEvent,
    ProposalTraceEvent,
    ReflectiveDatasetBuiltEvent,
    ValsetEvaluatedEvent,
)


def _safe_json(obj: Any) -> Any:
    """Make an object JSON-serializable."""
    if isinstance(obj, dict):
        return {str(k): _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_safe_json(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_safe_json(v) for v in obj)
    if isinstance(obj, float):
        if obj != obj:
            return None
        return obj
    if isinstance(obj, int | str | bool | type(None)):
        return obj
    if isinstance(obj, Exception):
        return f"{type(obj).__name__}: {obj}"
    type_name = type(obj).__name__
    if type_name == "GEPAState":
        return "<GEPAState>"
    return str(obj)


class StateLogger:
    """Writes numbered JSON state snapshots per iteration step.

    Args:
        output_dir: Base run directory. Files go into ``output_dir/states/``.
    """

    def __init__(self, output_dir: str) -> None:
        self.states_dir = os.path.join(output_dir, "states")
        os.makedirs(self.states_dir, exist_ok=True)
        self._current_iter = 0
        # Accumulate state for the current candidate selection
        self._selection_data: dict[str, Any] = {}
        self._eval_count = 0  # track current vs proposed eval
        self._current_eval_score: float | None = None

    def _write(self, iteration: int, step: str, data: dict[str, Any]) -> None:
        path = os.path.join(self.states_dir, f"iter_{iteration:03d}_{step}.json")
        with open(path, "w") as f:
            json.dump(_safe_json(data), f, indent=2, default=str)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._write(0, "00_optimization_start", {
            "seed_candidate": event["seed_candidate"],
            "trainset_size": event["trainset_size"],
            "valset_size": event["valset_size"],
            "config": event["config"],
        })

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._write(self._current_iter, "99_optimization_end", {
            "best_candidate_idx": event["best_candidate_idx"],
            "total_iterations": event["total_iterations"],
            "total_metric_calls": event["total_metric_calls"],
        })

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._current_iter = event["iteration"]
        self._eval_count = 0
        self._current_eval_score = None
        state = event["state"]
        self._write(event["iteration"], "01_iteration_start", {
            "iteration": event["iteration"],
            "num_candidates": len(state.program_candidates),
            "total_evals": state.total_num_evals,
            "best_scores": state.program_full_scores_val_set,
            "pareto_front_size": len(state.get_pareto_front_mapping()),
        })

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._write(event["iteration"], "12_iteration_end", {
            "iteration": event["iteration"],
            "proposal_accepted": event["proposal_accepted"],
        })

    # =========================================================================
    # Selection & Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        self._selection_data = {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "candidate": event["candidate"],
            "score": event["score"],
        }

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        self._write(event["iteration"], "02_selection_and_minibatch", {
            **self._selection_data,
            "minibatch_ids": event["minibatch_ids"],
            "trainset_size": event["trainset_size"],
            "minibatch_size": len(event["minibatch_ids"]),
        })

    # =========================================================================
    # Evaluation
    # =========================================================================

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        self._eval_count += 1
        step = "03_eval_current" if self._eval_count == 1 else "07_eval_proposed"
        aggregate = sum(event["scores"])
        if self._eval_count == 1:
            self._current_eval_score = float(aggregate)
        self._write(event["iteration"], step, {
            "candidate_idx": event["candidate_idx"],
            "scores": event["scores"],
            "aggregate_score": aggregate,
            "has_trajectories": event["has_trajectories"],
            "objective_scores": event["objective_scores"],
            "num_outputs": len(event["outputs"]) if event["outputs"] else 0,
        })

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        self._write(event["iteration"], "03_eval_skipped", {
            "candidate_idx": event["candidate_idx"],
            "reason": event["reason"],
            "scores": event["scores"],
        })

    # =========================================================================
    # Reflection & Proposal
    # =========================================================================

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        self._write(event["iteration"], "04_reflective_dataset", {
            "candidate_idx": event["candidate_idx"],
            "components": event["components"],
            "dataset": event["dataset"],
            "dataset_sizes": {k: len(v) for k, v in event["dataset"].items()},
        })

    def on_memory_state_snapshot(self, event: MemoryStateSnapshotEvent) -> None:
        step = "05_memory_before" if "before" in event["phase"] else "05_memory_after"
        self._write(event["iteration"], step, dict(event))

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        pass  # Captured in proposal trace

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        self._write(event["iteration"], f"06_proposal_{event['component_name']}", {
            "component_name": event["component_name"],
            "prompt_template": event["prompt_template"],
            "rendered_prompt": event["rendered_prompt"],
            "raw_response": event["raw_response"],
            "extracted_instruction": event["extracted_instruction"],
            "model_id": event["model_id"],
            "latency_ms": event["latency_ms"],
            "memory_was_injected": event["memory_was_injected"],
            "memory_selected_entry_ids": event["memory_selected_entry_ids"],
            "memory_selected_intents": event["memory_selected_intents"],
            "memory_selected_categories": event["memory_selected_categories"],
            "memory_reused_intents": event["memory_reused_intents"],
            "memory_reused_categories": event["memory_reused_categories"],
            "memory_reuse_detected": event["memory_reuse_detected"],
        })

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        pass  # Captured per-component in proposal_trace

    def on_lesson_generated(self, event: LessonGeneratedEvent) -> None:
        self._write(event["iteration"], f"07b_lesson_{event['component_name']}", {
            "component_name": event["component_name"],
            "intent": event["intent"],
            "lesson": event["lesson"],
            "categories_succeeded": event["categories_succeeded"],
            "categories_failed": event["categories_failed"],
            "score_before": event["score_before"],
            "score_after": event["score_after"],
            "accepted": event["accepted"],
            "latency_ms": event["latency_ms"],
            "fallback_used": event["fallback_used"],
            "memory_selected_entry_ids": event["memory_selected_entry_ids"],
            "memory_reused_intents": event["memory_reused_intents"],
            "memory_reused_categories": event["memory_reused_categories"],
            "memory_reuse_detected": event["memory_reuse_detected"],
        })

    # =========================================================================
    # Decision
    # =========================================================================

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        old_score = self._current_eval_score
        new_score = float(event["new_score"])
        delta = (new_score - old_score) if old_score is not None else None
        threshold = old_score
        decision_reason = (
            f"ACCEPTED | old={old_score:.4f} new={new_score:.4f} delta={delta:+.4f} "
            f"threshold={threshold:.4f} parent_ids={list(event['parent_ids'])} comparator='new_score > threshold'"
            if old_score is not None
            else (
                f"ACCEPTED | old=N/A new={new_score:.4f} delta=N/A "
                f"threshold=N/A parent_ids={list(event['parent_ids'])} comparator='new_score > threshold'"
            )
        )
        self._write(event["iteration"], "08_decision", {
            "accepted": True,
            "new_candidate_idx": event["new_candidate_idx"],
            "old_score": old_score,
            "new_score": new_score,
            "delta": delta,
            "threshold": threshold,
            "parent_ids": list(event["parent_ids"]),
            "decision_reason": decision_reason,
        })

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        old_score = float(event["old_score"])
        new_score = float(event["new_score"])
        delta = new_score - old_score
        decision_reason = (
            f"REJECTED | old={old_score:.4f} new={new_score:.4f} delta={delta:+.4f} "
            f"threshold={old_score:.4f} comparator='new_score > threshold' reason={event['reason']}"
        )
        self._write(event["iteration"], "08_decision", {
            "accepted": False,
            "old_score": old_score,
            "new_score": new_score,
            "delta": delta,
            "threshold": old_score,
            "reason": event["reason"],
            "decision_reason": decision_reason,
        })

    # =========================================================================
    # Pareto & Valset
    # =========================================================================

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._write(event["iteration"], "09_pareto", {
            "new_front": event["new_front"],
            "displaced_candidates": event["displaced_candidates"],
            "front_size": len(event["new_front"]),
        })

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        self._write(event["iteration"], "10_valset", {
            "candidate_idx": event["candidate_idx"],
            "average_score": event["average_score"],
            "scores_by_val_id": event["scores_by_val_id"],
            "num_examples_evaluated": event["num_examples_evaluated"],
            "total_valset_size": event["total_valset_size"],
            "is_best_program": event["is_best_program"],
        })

    # =========================================================================
    # Merge
    # =========================================================================

    def on_merge_attempted(self, event: MergeAttemptedEvent) -> None:
        self._write(event["iteration"], "11_merge_attempted", {
            "parent_ids": list(event["parent_ids"]),
            "merged_candidate": event["merged_candidate"],
        })

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        self._write(event["iteration"], "11_merge_result", {
            "accepted": True,
            "new_candidate_idx": event["new_candidate_idx"],
            "parent_ids": list(event["parent_ids"]),
        })

    def on_merge_rejected(self, event: MergeRejectedEvent) -> None:
        self._write(event["iteration"], "11_merge_result", {
            "accepted": False,
            "parent_ids": list(event["parent_ids"]),
            "reason": event["reason"],
        })

    # =========================================================================
    # Budget
    # =========================================================================

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        # Only write at meaningful points, not every eval increment
        pass
