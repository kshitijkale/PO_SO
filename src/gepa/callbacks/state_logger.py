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
    EvaluationStartEvent,
    IterationEndEvent,
    IterationStartEvent,
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
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _safe_json(obj.model_dump())
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return _safe_json(obj.to_dict())
    if hasattr(obj, "toDict") and callable(obj.toDict):
        return _safe_json(obj.toDict())
    if hasattr(obj, "dict") and callable(obj.dict):
        return _safe_json(obj.dict())
    if hasattr(obj, "__dict__") and isinstance(obj.__dict__, dict):
        return _safe_json(obj.__dict__)
    return str(obj)


class StateLogger:
    """Writes numbered JSON state snapshots per iteration step.

    Args:
        output_dir: Base run directory. Files go into ``output_dir/states/``.
    """

    def __init__(self, output_dir: str) -> None:
        self.states_dir = os.path.join(output_dir, "states")
        os.makedirs(self.states_dir, exist_ok=True)
        self._event_correlation_index = os.path.join(output_dir, "event_correlation_index.jsonl")
        self._current_iter = 0
        # Accumulate state for the current candidate selection
        self._selection_data: dict[str, Any] = {}
        self._eval_count = 0  # track current vs proposed eval
        self._pending_eval_starts: list[dict[str, Any]] = []
        self._current_eval_score: float | None = None

    def _write(self, iteration: int, step: str, data: dict[str, Any]) -> None:
        path = os.path.join(self.states_dir, f"iter_{iteration:03d}_{step}.json")
        with open(path, "w") as f:
            json.dump(_safe_json(data), f, indent=2, default=str)

        meta = data.get("_meta")
        if isinstance(meta, dict) and meta.get("event_id"):
            record = {
                "event_id": str(meta.get("event_id")),
                "event_type": str(meta.get("event_type", "")),
                "callback_method": str(meta.get("callback_method", "")),
                "iteration": meta.get("iteration", iteration),
                "source": "state_logger_snapshot",
                "artifact_path": os.path.relpath(path, os.path.dirname(self.states_dir)),
                "details": {"state_step": step},
            }
            with open(self._event_correlation_index, "a", encoding="utf-8") as idx:
                idx.write(json.dumps(_safe_json(record), default=str) + "\n")

    @staticmethod
    def _with_meta(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        meta = event.get("_meta")
        if isinstance(meta, dict):
            return {**payload, "_meta": meta}
        return payload

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._write(
            0,
            "00_optimization_start",
            self._with_meta(
                event,
                {
                    "seed_candidate": event["seed_candidate"],
                    "trainset_size": event["trainset_size"],
                    "valset_size": event["valset_size"],
                    "config": event["config"],
                },
            ),
        )

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._write(
            self._current_iter,
            "99_optimization_end",
            self._with_meta(
                event,
                {
                    "best_candidate_idx": event["best_candidate_idx"],
                    "total_iterations": event["total_iterations"],
                    "total_metric_calls": event["total_metric_calls"],
                },
            ),
        )

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._current_iter = event["iteration"]
        self._eval_count = 0
        self._pending_eval_starts = []
        self._current_eval_score = None
        state = event["state"]
        self._write(
            event["iteration"],
            "01_iteration_start",
            self._with_meta(
                event,
                {
                    "iteration": event["iteration"],
                    "num_candidates": len(state.program_candidates),
                    "total_evals": state.total_num_evals,
                    "best_scores": state.program_full_scores_val_set,
                    "pareto_front_size": len(state.get_pareto_front_mapping()),
                },
            ),
        )

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._write(
            event["iteration"],
            "12_iteration_end",
            self._with_meta(
                event,
                {
                    "iteration": event["iteration"],
                    "proposal_accepted": event["proposal_accepted"],
                },
            ),
        )

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
        self._write(
            event["iteration"],
            "02_selection_and_minibatch",
            self._with_meta(
                event,
                {
                    **self._selection_data,
                    "minibatch_ids": event["minibatch_ids"],
                    "trainset_size": event["trainset_size"],
                    "minibatch_size": len(event["minibatch_ids"]),
                },
            ),
        )

    # =========================================================================
    # Evaluation
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        self._pending_eval_starts.append(
            {
                "candidate_idx": event["candidate_idx"],
                "inputs": event["inputs"],
                "batch_size": event["batch_size"],
                "capture_traces": event["capture_traces"],
                "parent_ids": list(event["parent_ids"]),
                "is_seed_candidate": event["is_seed_candidate"],
            }
        )

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        self._eval_count += 1
        step = "03_eval_current" if self._eval_count == 1 else "07_eval_proposed"
        aggregate = sum(event["scores"])
        eval_start = self._pending_eval_starts.pop(0) if self._pending_eval_starts else None
        if self._eval_count == 1:
            self._current_eval_score = float(aggregate)
        self._write(
            event["iteration"],
            step,
            self._with_meta(
                event,
                {
                    "candidate_idx": event["candidate_idx"],
                    "evaluation_start": eval_start,
                    "inputs": eval_start["inputs"] if eval_start else None,
                    "scores": event["scores"],
                    "aggregate_score": aggregate,
                    "has_trajectories": event["has_trajectories"],
                    "objective_scores": event["objective_scores"],
                    "outputs": event["outputs"],
                    "trajectories": event["trajectories"],
                    "num_outputs": len(event["outputs"]) if event["outputs"] else 0,
                },
            ),
        )

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        self._write(
            event["iteration"],
            "03_eval_skipped",
            self._with_meta(
                event,
                {
                    "candidate_idx": event["candidate_idx"],
                    "reason": event["reason"],
                    "scores": event["scores"],
                },
            ),
        )

    # =========================================================================
    # Reflection & Proposal
    # =========================================================================

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        self._write(
            event["iteration"],
            "04_reflective_dataset",
            self._with_meta(
                event,
                {
                    "candidate_idx": event["candidate_idx"],
                    "components": event["components"],
                    "dataset": event["dataset"],
                    "dataset_sizes": {k: len(v) for k, v in event["dataset"].items()},
                },
            ),
        )

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        pass  # Captured in proposal trace

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        self._write(
            event["iteration"],
            f"06_proposal_{event['component_name']}",
            self._with_meta(
                event,
                {
                    "component_name": event["component_name"],
                    "prompt_template": event["prompt_template"],
                    "rendered_prompt": event["rendered_prompt"],
                    "raw_response": event["raw_response"],
                    "extracted_instruction": event["extracted_instruction"],
                    "model_id": event["model_id"],
                    "latency_ms": event["latency_ms"],
                    "memory_was_injected": event["memory_was_injected"],
                },
            ),
        )

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        pass  # Captured per-component in proposal_trace

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
        self._write(
            event["iteration"],
            "08_decision",
            self._with_meta(
                event,
                {
                    "accepted": True,
                    "new_candidate_idx": event["new_candidate_idx"],
                    "old_score": old_score,
                    "new_score": new_score,
                    "delta": delta,
                    "threshold": threshold,
                    "parent_ids": list(event["parent_ids"]),
                    "decision_reason": decision_reason,
                },
            ),
        )

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        old_score = float(event["old_score"])
        new_score = float(event["new_score"])
        delta = new_score - old_score
        decision_reason = (
            f"REJECTED | old={old_score:.4f} new={new_score:.4f} delta={delta:+.4f} "
            f"threshold={old_score:.4f} comparator='new_score > threshold' reason={event['reason']}"
        )
        self._write(
            event["iteration"],
            "08_decision",
            self._with_meta(
                event,
                {
                    "accepted": False,
                    "old_score": old_score,
                    "new_score": new_score,
                    "delta": delta,
                    "threshold": old_score,
                    "reason": event["reason"],
                    "decision_reason": decision_reason,
                },
            ),
        )

    # =========================================================================
    # Pareto & Valset
    # =========================================================================

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._write(
            event["iteration"],
            "09_pareto",
            self._with_meta(
                event,
                {
                    "new_front": event["new_front"],
                    "displaced_candidates": event["displaced_candidates"],
                    "front_size": len(event["new_front"]),
                },
            ),
        )

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        self._write(
            event["iteration"],
            "10_valset",
            self._with_meta(
                event,
                {
                    "candidate_idx": event["candidate_idx"],
                    "candidate": event["candidate"],
                    "average_score": event["average_score"],
                    "scores_by_val_id": event["scores_by_val_id"],
                    "num_examples_evaluated": event["num_examples_evaluated"],
                    "total_valset_size": event["total_valset_size"],
                    "parent_ids": list(event["parent_ids"]),
                    "is_best_program": event["is_best_program"],
                    "inputs_by_val_id": event["inputs_by_val_id"],
                    "outputs_by_val_id": event["outputs_by_val_id"],
                },
            ),
        )

    # =========================================================================
    # Merge
    # =========================================================================

    def on_merge_attempted(self, event: MergeAttemptedEvent) -> None:
        self._write(
            event["iteration"],
            "11_merge_attempted",
            self._with_meta(
                event,
                {
                    "parent_ids": list(event["parent_ids"]),
                    "merged_candidate": event["merged_candidate"],
                },
            ),
        )

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        self._write(
            event["iteration"],
            "11_merge_result",
            self._with_meta(
                event,
                {
                    "accepted": True,
                    "new_candidate_idx": event["new_candidate_idx"],
                    "parent_ids": list(event["parent_ids"]),
                },
            ),
        )

    def on_merge_rejected(self, event: MergeRejectedEvent) -> None:
        self._write(
            event["iteration"],
            "11_merge_result",
            self._with_meta(
                event,
                {
                    "accepted": False,
                    "parent_ids": list(event["parent_ids"]),
                    "reason": event["reason"],
                },
            ),
        )

    # =========================================================================
    # Budget
    # =========================================================================

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        # Only write at meaningful points, not every eval increment
        pass
