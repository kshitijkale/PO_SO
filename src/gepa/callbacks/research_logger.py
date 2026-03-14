# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""ResearchLogger — maximally exhaustive per-iteration logging callback.

Writes structured markdown reports AND machine-readable JSONL for every
event during GEPA optimization. Zero truncation — every LLM prompt,
every LLM response, every evaluation output, every memory entry is
captured in full.

Output files:
    log.jsonl            — one JSON line per event (complete event stream)
    summary.jsonl        — one JSON line per iteration (key metrics)
    pareto_timeline.jsonl — Pareto front after each iteration
    index.md             — cross-iteration index with links and scores
    iterations/          — per-iteration markdown narrative reports
    candidates/          — full text of every accepted candidate
    memory/              — reflection memory snapshots and events
    llm_calls/           — complete LLM interaction records
"""

from __future__ import annotations

import difflib
import json
import os
import time
from typing import Any

from gepa.core.callbacks import (
    BudgetUpdatedEvent,
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    CandidateSelectedEvent,
    ErrorEvent,
    EvaluationEndEvent,
    EvaluationSkippedEvent,
    EvaluationStartEvent,
    IterationEndEvent,
    IterationStartEvent,
    LessonGeneratedEvent,
    MemoryEntryAddedEvent,
    MemoryQueriedEvent,
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
    StateSavedEvent,
    ValsetEvaluatedEvent,
)


def _safe_json(obj: Any) -> Any:
    """Make an object JSON-serializable by converting non-serializable types."""
    if isinstance(obj, dict):
        return {str(k): _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_safe_json(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_safe_json(v) for v in obj)
    if isinstance(obj, float):
        if obj != obj:  # NaN
            return None
        return obj
    if isinstance(obj, int | str | bool | type(None)):
        return obj
    if isinstance(obj, Exception):
        return f"{type(obj).__name__}: {obj}"
    # For GEPAState and other complex objects, skip them
    type_name = type(obj).__name__
    if type_name == "GEPAState":
        return "<GEPAState>"
    return str(obj)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class ResearchLogger:
    """Exhaustive callback logger that writes everything to disk.

    Zero truncation policy: every piece of data that passes through the
    optimization loop is captured verbatim in the iteration reports and
    JSONL logs.

    Args:
        output_dir: Directory for all output files. Created if missing.
    """

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        self._start_time = time.monotonic()
        self._iter_start_time = 0.0

        # Per-iteration buffer for building the markdown report
        self._iter_buf: dict[str, Any] = {}

        # Tracking
        self._total_accepted = 0
        self._total_iterations = 0
        self._best_score = 0.0
        self._best_candidate_idx = 0

        # Cross-iteration index data
        self._iteration_index: list[dict[str, Any]] = []

        # Create directory structure
        for subdir in ["iterations", "candidates", "memory", "memory/prompts_with_memory", "llm_calls"]:
            os.makedirs(os.path.join(output_dir, subdir), exist_ok=True)

        # Open persistent log files
        self._log_jsonl = open(os.path.join(output_dir, "log.jsonl"), "a")
        self._summary_jsonl = open(os.path.join(output_dir, "summary.jsonl"), "a")
        self._pareto_jsonl = open(os.path.join(output_dir, "pareto_timeline.jsonl"), "a")
        self._memory_events_jsonl = open(os.path.join(output_dir, "memory", "memory_events.jsonl"), "a")

    def _log_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Write one JSON line to the event log."""
        record = {"timestamp": _timestamp(), "event_type": event_type, **data}
        self._log_jsonl.write(json.dumps(_safe_json(record), default=str) + "\n")
        self._log_jsonl.flush()

    def _elapsed(self) -> str:
        secs = time.monotonic() - self._start_time
        m, s = divmod(int(secs), 60)
        return f"{m:02d}m {s:02d}s"

    @staticmethod
    def _sum_scores(eval_block: dict[str, Any] | None) -> float | None:
        if not eval_block:
            return None
        scores = eval_block.get("scores")
        if isinstance(scores, list):
            return float(sum(scores))
        return None

    @staticmethod
    def _decision_reason(
        *,
        accepted: bool,
        old_score: float | None,
        new_score: float | None,
        parent_ids: list[int],
        base_reason: str | None = None,
    ) -> dict[str, Any]:
        if old_score is not None and new_score is not None:
            delta: float | None = new_score - old_score
            threshold = old_score
            passes = new_score > threshold
        else:
            delta = None
            threshold = None
            passes = accepted

        status = "ACCEPTED" if accepted else "REJECTED"
        old_s = f"{old_score:.4f}" if old_score is not None else "N/A"
        new_s = f"{new_score:.4f}" if new_score is not None else "N/A"
        delta_s = f"{delta:+.4f}" if delta is not None else "N/A"
        threshold_s = f"{threshold:.4f}" if threshold is not None else "N/A"
        parent_s = parent_ids if parent_ids else ["N/A"]
        line = (
            f"{status} | old={old_s} new={new_s} delta={delta_s} "
            f"threshold={threshold_s} parent_ids={parent_s} comparator='new_score > threshold'"
        )
        if base_reason:
            line += f" | reason={base_reason}"

        return {
            "status": status,
            "old_score": old_score,
            "new_score": new_score,
            "delta": delta,
            "threshold": threshold,
            "parent_ids": parent_ids,
            "passes_threshold": passes,
            "base_reason": base_reason,
            "line": line,
        }

    # =========================================================================
    # Optimization Lifecycle
    # =========================================================================

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._start_time = time.monotonic()
        self._log_event("optimization_start", dict(event))

        # Write config to a separate file for reference
        config_path = os.path.join(self.output_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump(_safe_json(dict(event)), f, indent=2, default=str)

        # Save seed candidate
        self._save_candidate(0, event["seed_candidate"], iteration=0, operation="seed", parent_idxs=[])

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        data = {
            "best_candidate_idx": event["best_candidate_idx"],
            "total_iterations": event["total_iterations"],
            "total_metric_calls": event["total_metric_calls"],
            "elapsed": self._elapsed(),
        }
        self._log_event("optimization_end", data)

        # Write cross-iteration index
        self._write_index()

        # Close file handles
        for fh in [self._log_jsonl, self._summary_jsonl, self._pareto_jsonl, self._memory_events_jsonl]:
            fh.close()

    # =========================================================================
    # Iteration Lifecycle
    # =========================================================================

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._iter_start_time = time.monotonic()
        self._iter_buf = {
            "iteration": event["iteration"],
            "timestamp": _timestamp(),
            "num_candidates": len(event["state"].program_candidates),
            "total_evals": event["state"].total_num_evals,
        }
        self._log_event(
            "iteration_start",
            {
                "iteration": event["iteration"],
                "num_candidates": len(event["state"].program_candidates),
                "total_evals": event["state"].total_num_evals,
            },
        )

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._total_iterations += 1
        iteration = event["iteration"]
        accepted = event["proposal_accepted"]
        if accepted:
            self._total_accepted += 1

        iter_elapsed = time.monotonic() - self._iter_start_time
        self._iter_buf["accepted"] = accepted
        self._iter_buf["iter_elapsed_s"] = round(iter_elapsed, 2)

        self._log_event(
            "iteration_end",
            {"iteration": iteration, "accepted": accepted, "iter_elapsed_s": round(iter_elapsed, 2)},
        )

        # Write summary JSONL line
        state = event["state"]
        best_scores = state.program_full_scores_val_set
        best_idx = max(range(len(best_scores)), key=lambda i: best_scores[i]) if best_scores else 0
        self._best_score = best_scores[best_idx] if best_scores else 0.0
        self._best_candidate_idx = best_idx

        summary = {
            "iteration": iteration,
            "timestamp": _timestamp(),
            "accepted": accepted,
            "best_score": self._best_score,
            "best_candidate_idx": best_idx,
            "num_candidates": len(state.program_candidates),
            "total_evals": state.total_num_evals,
            "acceptance_rate": self._total_accepted / self._total_iterations if self._total_iterations > 0 else 0.0,
            "iter_elapsed_s": round(iter_elapsed, 2),
            "total_elapsed": self._elapsed(),
        }
        self._summary_jsonl.write(json.dumps(summary) + "\n")
        self._summary_jsonl.flush()

        # Track for index
        self._iteration_index.append({
            "iteration": iteration,
            "accepted": accepted,
            "best_score": self._best_score,
            "iter_elapsed_s": round(iter_elapsed, 2),
            "decision": self._iter_buf.get("decision", {}),
        })

        # Write iteration markdown report
        self._write_iteration_report(iteration)

    # =========================================================================
    # Candidate Selection and Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        self._iter_buf["selected_candidate_idx"] = event["candidate_idx"]
        self._iter_buf["selected_candidate_score"] = event["score"]
        self._iter_buf["selected_candidate"] = event["candidate"]
        self._log_event("candidate_selected", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "score": event["score"],
            "candidate": event["candidate"],
        })

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        self._iter_buf["minibatch_ids"] = event["minibatch_ids"]
        self._iter_buf["trainset_size"] = event["trainset_size"]
        self._log_event("minibatch_sampled", _safe_json(dict(event)))

    # =========================================================================
    # Evaluation Events
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        # Store inputs for the iteration report
        key = "eval_inputs_current" if event["capture_traces"] else "eval_inputs_proposed"
        self._iter_buf[key] = _safe_json(event["inputs"])

        self._log_event("evaluation_start", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "batch_size": event["batch_size"],
            "capture_traces": event["capture_traces"],
            "is_seed_candidate": event["is_seed_candidate"],
            "inputs": _safe_json(event["inputs"]),
        })

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        key = "eval_current" if event["capture_traces"] else "eval_proposed"
        self._iter_buf[key] = {
            "candidate_idx": event["candidate_idx"],
            "scores": event["scores"],
            "has_trajectories": event["has_trajectories"],
            "objective_scores": _safe_json(event["objective_scores"]),
            "outputs": _safe_json(event["outputs"]),
            "trajectories": _safe_json(event["trajectories"]),
        }
        self._log_event("evaluation_end", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "scores": event["scores"],
            "has_trajectories": event["has_trajectories"],
            "capture_traces": event.get("capture_traces"),
            "outputs": _safe_json(event["outputs"]),
            "trajectories": _safe_json(event["trajectories"]),
            "objective_scores": _safe_json(event["objective_scores"]),
        })

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        self._iter_buf["evaluation_skipped"] = {"reason": event["reason"], "scores": event["scores"]}
        self._log_event("evaluation_skipped", _safe_json(dict(event)))

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        self._iter_buf["valset_eval"] = {
            "candidate_idx": event["candidate_idx"],
            "average_score": event["average_score"],
            "num_examples": event["num_examples_evaluated"],
            "total_valset_size": event["total_valset_size"],
            "is_best_program": event["is_best_program"],
            "scores_by_val_id": _safe_json(event["scores_by_val_id"]),
            "outputs_by_val_id": _safe_json(event.get("outputs_by_val_id")),
        }
        self._log_event("valset_evaluated", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "average_score": event["average_score"],
            "num_examples": event["num_examples_evaluated"],
            "is_best_program": event["is_best_program"],
            "scores_by_val_id": _safe_json(event["scores_by_val_id"]),
            "outputs_by_val_id": _safe_json(event.get("outputs_by_val_id")),
        })

        # Persist discovered candidates to stable files so downstream analysis
        # can inspect all accepted programs without parsing iteration markdown.
        candidate_idx = int(event["candidate_idx"])
        if candidate_idx > 0:
            parent_ids = list(event.get("parent_ids", []))
            operation = "merge" if len(parent_ids) > 1 else "mutation"
            candidate_path = os.path.join(self.output_dir, "candidates", f"candidate_{candidate_idx:03d}.json")
            if not os.path.exists(candidate_path):
                self._save_candidate(
                    candidate_idx,
                    event["candidate"],
                    iteration=event["iteration"],
                    operation=operation,
                    parent_idxs=parent_ids,
                )

    # =========================================================================
    # Reflection Events
    # =========================================================================

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        self._iter_buf["reflective_dataset"] = {
            "candidate_idx": event["candidate_idx"],
            "components": event["components"],
            "dataset": _safe_json(event["dataset"]),
        }
        self._log_event("reflective_dataset_built", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "components": event["components"],
            "dataset": _safe_json(event["dataset"]),
        })

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        self._iter_buf["proposal_parent"] = event["parent_candidate"]
        self._iter_buf["proposal_components"] = event["components"]
        self._log_event("proposal_start", {
            "iteration": event["iteration"],
            "components": event["components"],
            "parent_candidate": event["parent_candidate"],
        })

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        self._iter_buf["new_instructions"] = event["new_instructions"]
        self._log_event("proposal_end", {
            "iteration": event["iteration"],
            "new_instructions": event["new_instructions"],
        })

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        self._iter_buf.setdefault("proposal_traces", {})[event["component_name"]] = {
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
        }
        # Log FULL prompt and response to JSONL (no truncation)
        self._log_event("proposal_trace", {
            "iteration": event["iteration"],
            "component_name": event["component_name"],
            "model_id": event["model_id"],
            "latency_ms": round(event["latency_ms"], 1),
            "memory_was_injected": event["memory_was_injected"],
            "memory_selected_entry_ids": event["memory_selected_entry_ids"],
            "memory_selected_intents": event["memory_selected_intents"],
            "memory_selected_categories": event["memory_selected_categories"],
            "memory_reused_intents": event["memory_reused_intents"],
            "memory_reused_categories": event["memory_reused_categories"],
            "memory_reuse_detected": event["memory_reuse_detected"],
            "prompt_template": event["prompt_template"],
            "rendered_prompt": event["rendered_prompt"],
            "raw_response": event["raw_response"],
            "extracted_instruction": event["extracted_instruction"],
        })

        # Write LLM call files
        iteration = event["iteration"]
        comp = event["component_name"]
        base = os.path.join(self.output_dir, "llm_calls")

        with open(os.path.join(base, f"iter_{iteration:03d}_{comp}.json"), "w") as f:
            json.dump(_safe_json(dict(event)), f, indent=2, default=str)
        with open(os.path.join(base, f"iter_{iteration:03d}_{comp}_prompt.txt"), "w") as f:
            f.write(event["rendered_prompt"])
        with open(os.path.join(base, f"iter_{iteration:03d}_{comp}_response.txt"), "w") as f:
            f.write(event["raw_response"])

    # =========================================================================
    # Acceptance/Rejection Events
    # =========================================================================

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        old_score = self._sum_scores(self._iter_buf.get("eval_current"))
        new_score = float(event["new_score"])
        parent_ids = list(event["parent_ids"])
        decision_reason = self._decision_reason(
            accepted=True,
            old_score=old_score,
            new_score=new_score,
            parent_ids=parent_ids,
        )

        self._iter_buf["decision"] = {
            "accepted": True,
            "new_candidate_idx": event["new_candidate_idx"],
            "new_score": new_score,
            "old_score": old_score,
            "delta": decision_reason["delta"],
            "threshold": decision_reason["threshold"],
            "parent_ids": parent_ids,
            "decision_reason": decision_reason["line"],
        }
        self._iter_buf["decision_reason"] = decision_reason
        payload = _safe_json(dict(event))
        payload["decision_reason"] = decision_reason
        self._log_event("candidate_accepted", payload)

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        old_score = float(event["old_score"])
        new_score = float(event["new_score"])
        parent_ids = [self._iter_buf["selected_candidate_idx"]] if "selected_candidate_idx" in self._iter_buf else []
        decision_reason = self._decision_reason(
            accepted=False,
            old_score=old_score,
            new_score=new_score,
            parent_ids=parent_ids,
            base_reason=event["reason"],
        )

        self._iter_buf["decision"] = {
            "accepted": False,
            "old_score": old_score,
            "new_score": new_score,
            "delta": decision_reason["delta"],
            "threshold": decision_reason["threshold"],
            "parent_ids": parent_ids,
            "reason": event["reason"],
            "decision_reason": decision_reason["line"],
        }
        self._iter_buf["decision_reason"] = decision_reason
        payload = _safe_json(dict(event))
        payload["parent_ids"] = parent_ids
        payload["decision_reason"] = decision_reason
        self._log_event("candidate_rejected", payload)

    # =========================================================================
    # Merge Events
    # =========================================================================

    def on_merge_attempted(self, event: MergeAttemptedEvent) -> None:
        self._iter_buf["merge"] = {"attempted": True, "parent_ids": list(event["parent_ids"])}
        self._log_event("merge_attempted", _safe_json(dict(event)))

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        self._iter_buf.setdefault("merge", {})["accepted"] = True
        self._iter_buf["merge"]["new_candidate_idx"] = event["new_candidate_idx"]
        self._log_event("merge_accepted", _safe_json(dict(event)))

    def on_merge_rejected(self, event: MergeRejectedEvent) -> None:
        self._iter_buf.setdefault("merge", {})["accepted"] = False
        self._iter_buf["merge"]["reason"] = event["reason"]
        self._log_event("merge_rejected", _safe_json(dict(event)))

    # =========================================================================
    # State Events
    # =========================================================================

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._iter_buf["pareto_update"] = {
            "new_front": event["new_front"],
            "displaced": event["displaced_candidates"],
        }
        self._log_event("pareto_front_updated", _safe_json(dict(event)))

        # Write pareto timeline
        record = {
            "iteration": event["iteration"],
            "front_members": event["new_front"],
            "displaced": event["displaced_candidates"],
            "front_size": len(event["new_front"]),
        }
        self._pareto_jsonl.write(json.dumps(record) + "\n")
        self._pareto_jsonl.flush()

    def on_state_saved(self, event: StateSavedEvent) -> None:
        self._log_event("state_saved", _safe_json(dict(event)))

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        self._iter_buf["budget"] = {
            "metric_calls_used": event["metric_calls_used"],
            "metric_calls_delta": event["metric_calls_delta"],
            "metric_calls_remaining": event["metric_calls_remaining"],
        }
        self._log_event("budget_updated", _safe_json(dict(event)))

    def on_error(self, event: ErrorEvent) -> None:
        self._iter_buf["error"] = {"exception": str(event["exception"]), "will_continue": event["will_continue"]}
        self._log_event("error", {
            "iteration": event["iteration"],
            "exception": str(event["exception"]),
            "will_continue": event["will_continue"],
        })

    # =========================================================================
    # Memory Events
    # =========================================================================

    def on_memory_entry_added(self, event: MemoryEntryAddedEvent) -> None:
        self._iter_buf.setdefault("memory_updates", []).append({
            "component": event["component_name"],
            "entry": event["entry"],
            "evicted": event["evicted_entry"],
            "size_after": event["memory_size_after"],
            "utilization": event["memory_utilization"],
        })
        self._log_event("memory_entry_added", _safe_json(dict(event)))

        # Write to dedicated memory events log
        record = {
            "timestamp": _timestamp(),
            "event": "add",
            "iteration": event["iteration"],
            "component": event["component_name"],
            "entry_id": event["entry"].get("entry_id"),
            "entry": _safe_json(event["entry"]),
            "accepted": event["entry"].get("accepted"),
            "score_delta": (event["entry"].get("score_after", 0) or 0) - (event["entry"].get("score_before", 0) or 0),
            "evicted": _safe_json(event["evicted_entry"]) if event["evicted_entry"] else None,
            "size_after": event["memory_size_after"],
        }
        self._memory_events_jsonl.write(json.dumps(record) + "\n")
        self._memory_events_jsonl.flush()

    def on_memory_queried(self, event: MemoryQueriedEvent) -> None:
        self._iter_buf.setdefault("memory_queries", []).append({
            "component": event["component_name"],
            "selected_entry_ids": event["selected_entry_ids"],
            "entries_returned": event["entries_returned"],
            "formatted_text": event["formatted_text"],
            "formatted_text_length": event["formatted_text_length"],
        })
        # Log full query including all returned entries and formatted text
        self._log_event("memory_queried", {
            "iteration": event["iteration"],
            "component": event["component_name"],
            "query_n": event["query_n"],
            "selected_entry_ids": event["selected_entry_ids"],
            "entries_returned": _safe_json(event["entries_returned"]),
            "formatted_text": event["formatted_text"],
            "formatted_text_length": event["formatted_text_length"],
        })

        # Write memory-injected prompt to file
        if event["formatted_text"]:
            path = os.path.join(
                self.output_dir,
                "memory",
                "prompts_with_memory",
                f"iter_{event['iteration']:03d}_{event['component_name']}.txt",
            )
            with open(path, "w") as f:
                f.write(event["formatted_text"])

        # Write to memory events log
        record = {
            "timestamp": _timestamp(),
            "event": "query",
            "iteration": event["iteration"],
            "component": event["component_name"],
            "query_n": event["query_n"],
            "selected_entry_ids": event["selected_entry_ids"],
            "entries_returned": _safe_json(event["entries_returned"]),
            "formatted_text": event["formatted_text"],
            # Keep both keys for backward compatibility with older analysis scripts.
            "formatted_length": event["formatted_text_length"],
            "formatted_text_length": event["formatted_text_length"],
        }
        self._memory_events_jsonl.write(json.dumps(record) + "\n")
        self._memory_events_jsonl.flush()

    def on_memory_state_snapshot(self, event: MemoryStateSnapshotEvent) -> None:
        self._iter_buf.setdefault("memory_snapshots", []).append({
            "phase": event["phase"],
            "total_entries": event["total_entries"],
            "max_entries": event["max_entries"],
            "entries_by_component": event["entries_by_component"],
            "accepted_ratio": event["accepted_ratio"],
            "rejected_ratio": event["rejected_ratio"],
            "all_entries": event["all_entries"],
        })
        self._log_event("memory_state_snapshot", {
            "iteration": event["iteration"],
            "phase": event["phase"],
            "total_entries": event["total_entries"],
            "max_entries": event["max_entries"],
            "accepted_ratio": round(event["accepted_ratio"], 3),
            "rejected_ratio": round(event["rejected_ratio"], 3),
            "entries_by_component": event["entries_by_component"],
            "all_entries": _safe_json(event["all_entries"]),
        })

        # Write full snapshot to file
        snapshot_path = os.path.join(
            self.output_dir, "memory", f"memory_state_iter_{event['iteration']:03d}_{event['phase']}.json"
        )
        with open(snapshot_path, "w") as f:
            json.dump(_safe_json(dict(event)), f, indent=2, default=str)

    def on_lesson_generated(self, event: LessonGeneratedEvent) -> None:
        self._iter_buf.setdefault("lessons", []).append({
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
        self._log_event("lesson_generated", _safe_json(dict(event)))
        # Write to dedicated lesson events log
        lesson_path = os.path.join(self.output_dir, "memory", "lesson_events.jsonl")
        with open(lesson_path, "a") as f:
            f.write(json.dumps(_safe_json(dict(event))) + "\n")

    # =========================================================================
    # Helper: Save candidate
    # =========================================================================

    def _save_candidate(
        self,
        idx: int,
        candidate: dict[str, str],
        iteration: int,
        operation: str,
        parent_idxs: list[int],
    ) -> None:
        record = {
            "index": idx,
            "iteration_created": iteration,
            "operation": operation,
            "parent_indices": parent_idxs,
            "components": candidate,
        }
        path = os.path.join(self.output_dir, "candidates", f"candidate_{idx:03d}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)

    # =========================================================================
    # Helper: Write cross-iteration index
    # =========================================================================

    def _write_index(self) -> None:
        """Write index.md with links to all iteration reports and a score timeline."""
        lines: list[str] = []
        lines.append("# Optimization Run Index")
        lines.append("")
        lines.append(f"**Total iterations:** {self._total_iterations}")
        lines.append(f"**Accepted:** {self._total_accepted}/{self._total_iterations} ({self._total_accepted / self._total_iterations * 100:.1f}%)" if self._total_iterations > 0 else "")
        lines.append(f"**Best score:** {self._best_score:.4f} (candidate #{self._best_candidate_idx})")
        lines.append(f"**Total elapsed:** {self._elapsed()}")
        lines.append("")

        # Score timeline table
        lines.append("## Iteration Timeline")
        lines.append("")
        lines.append("| Iter | Decision | Best Score | Time | Details |")
        lines.append("|------|----------|------------|------|---------|")
        for entry in self._iteration_index:
            it = entry["iteration"]
            dec = entry.get("decision", {})
            accepted = dec.get("accepted", False)
            status = "ACCEPTED" if accepted else "REJECTED"
            score = entry.get("best_score", 0)
            elapsed = entry.get("iter_elapsed_s", 0)
            link = f"[iteration_{it:03d}.md](iterations/iteration_{it:03d}.md)"
            lines.append(f"| {it} | {status} | {score:.4f} | {elapsed:.1f}s | {link} |")
        lines.append("")

        path = os.path.join(self.output_dir, "index.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))

    # =========================================================================
    # Helper: Write per-iteration markdown report
    # =========================================================================

    def _write_iteration_report(self, iteration: int) -> None:
        buf = self._iter_buf
        lines: list[str] = []

        # =================================================================
        # Header
        # =================================================================
        lines.append(f"# Iteration {iteration}")
        lines.append("")
        lines.append(f"**Timestamp:** {buf.get('timestamp', 'N/A')}")
        lines.append(f"**Elapsed:** {self._elapsed()}")
        lines.append(f"**Iteration time:** {buf.get('iter_elapsed_s', 'N/A')}s")
        lines.append(f"**Total evals so far:** {buf.get('total_evals', 'N/A')}")
        lines.append(f"**Candidates in population:** {buf.get('num_candidates', 'N/A')}")
        lines.append(f"**Best score:** {self._best_score:.4f} (candidate #{self._best_candidate_idx})")
        if self._total_iterations > 0:
            lines.append(f"**Acceptance rate:** {self._total_accepted}/{self._total_iterations} ({self._total_accepted / self._total_iterations * 100:.1f}%)")
        lines.append("")

        # =================================================================
        # Research Verdict (high-signal summary for rapid review)
        # =================================================================
        lines.append("## Research Verdict")
        lines.append("")

        decision = buf.get("decision", {})
        decision_reason = buf.get("decision_reason", {})
        if decision:
            status = "ACCEPTED" if decision.get("accepted") else "REJECTED"
            lines.append(f"- **Decision:** {status}")
            if decision_reason:
                lines.append(f"- **Decision reason:** `{decision_reason.get('line', 'N/A')}`")
        else:
            lines.append("- **Decision:** N/A")

        if "proposal_parent" in buf and "new_instructions" in buf:
            lines.append("- **What changed:**")
            for comp_name, new_text in buf["new_instructions"].items():
                old_text = buf["proposal_parent"].get(comp_name, "")
                delta_chars = len(new_text) - len(old_text)
                changed = old_text != new_text
                status = "changed" if changed else "unchanged"
                lines.append(
                    f"  - `{comp_name}`: {status}, chars {len(old_text)} -> {len(new_text)} ({delta_chars:+d})"
                )
        else:
            lines.append("- **What changed:** N/A")

        lessons = buf.get("lessons", [])
        if lessons:
            lines.append("- **Why the model says it changed:**")
            for lesson in lessons:
                intent = lesson.get("intent") or "(no intent)"
                text = lesson.get("lesson") or "(fallback: no V2 lesson text)"
                lines.append(f"  - `{lesson['component_name']}` intent: {intent}")
                lines.append(f"  - `{lesson['component_name']}` lesson: {text}")
        else:
            lines.append("- **Why the model says it changed:** N/A")

        old_sum = self._sum_scores(buf.get("eval_current"))
        new_sum = self._sum_scores(buf.get("eval_proposed"))
        if old_sum is not None and new_sum is not None:
            delta = new_sum - old_sum
            lines.append(f"- **Did score improve:** {new_sum > old_sum} ({old_sum:.4f} -> {new_sum:.4f}, {delta:+.4f})")
        else:
            lines.append("- **Did score improve:** N/A")

        moved_summary: list[str] = []
        curr_scores = buf.get("eval_current", {}).get("scores", [])
        prop_scores = buf.get("eval_proposed", {}).get("scores", [])
        reflective_dataset = buf.get("reflective_dataset", {}).get("dataset", {})
        first_component_records = next(iter(reflective_dataset.values()), [])
        if (
            isinstance(curr_scores, list)
            and isinstance(prop_scores, list)
            and curr_scores
            and len(curr_scores) == len(prop_scores)
        ):
            improved: list[str] = []
            regressed: list[str] = []
            still_failing: list[str] = []
            for idx, (old_s, new_s) in enumerate(zip(curr_scores, prop_scores, strict=False)):
                feedback = f"Example {idx}"
                if idx < len(first_component_records):
                    rec = first_component_records[idx]
                    feedback = str(rec.get("Feedback") or rec.get("feedback") or feedback)
                old_fail = isinstance(old_s, int | float) and old_s < 1.0
                new_fail = isinstance(new_s, int | float) and new_s < 1.0
                if old_fail and not new_fail:
                    improved.append(feedback)
                elif (not old_fail) and new_fail:
                    regressed.append(feedback)
                elif old_fail and new_fail:
                    still_failing.append(feedback)

            moved_summary.append(f"improved={len(improved)}")
            moved_summary.append(f"regressed={len(regressed)}")
            moved_summary.append(f"still_failing={len(still_failing)}")
            if improved:
                moved_summary.append(f"improved_examples={improved[:2]}")
            if regressed:
                moved_summary.append(f"regressed_examples={regressed[:2]}")
            if still_failing:
                moved_summary.append(f"still_failing_examples={still_failing[:2]}")

        if moved_summary:
            lines.append(f"- **Failure modes moved:** {', '.join(moved_summary)}")
        else:
            lines.append("- **Failure modes moved:** N/A")
        lines.append("")

        # =================================================================
        # Table of Contents
        # =================================================================
        lines.append("## Table of Contents")
        lines.append("")
        toc_sections = [
            ("research-verdict", "Research Verdict"),
            ("candidate-selection", "Candidate Selection"),
            ("minibatch", "Minibatch"),
            ("current-candidate-evaluation-pre-mutation", "Current Candidate Evaluation (pre-mutation)"),
            ("reflective-dataset", "Reflective Dataset"),
            ("reflection-memory", "Reflection Memory"),
            ("llm-reflection-calls", "LLM Reflection Calls"),
            ("full-candidate-texts", "Full Candidate Texts"),
            ("beforeafter-diff", "Before/After Diff"),
            ("proposed-candidate-evaluation-post-mutation", "Proposed Candidate Evaluation (post-mutation)"),
            ("acceptance-decision", "Acceptance Decision"),
            ("lesson-generated", "Lesson Generated"),
            ("pareto-front-update", "Pareto Front Update"),
            ("validation-set-evaluation", "Validation Set Evaluation"),
            ("merge", "Merge"),
            ("budget", "Budget"),
        ]
        for anchor, title in toc_sections:
            lines.append(f"- [{title}](#{anchor})")
        lines.append("")

        # =================================================================
        # Candidate Selection — full text of selected candidate
        # =================================================================
        if "selected_candidate_idx" in buf:
            lines.append("## Candidate Selection")
            lines.append("")
            lines.append(f"- **Selected:** candidate #{buf['selected_candidate_idx']} (score: {buf.get('selected_candidate_score', 'N/A'):.4f})")
            if "selected_candidate" in buf:
                for comp_name, comp_text in buf["selected_candidate"].items():
                    lines.append(f"- **Component `{comp_name}`:** {len(comp_text)} chars")
                lines.append("")
                lines.append("### Full Candidate Text")
                lines.append("")
                for comp_name, comp_text in buf["selected_candidate"].items():
                    lines.append(f"#### `{comp_name}`")
                    lines.append("")
                    lines.append("```")
                    lines.append(comp_text)
                    lines.append("```")
                    lines.append("")

        # =================================================================
        # Minibatch
        # =================================================================
        if "minibatch_ids" in buf:
            lines.append("## Minibatch")
            lines.append("")
            lines.append(f"- **IDs:** {buf['minibatch_ids']}")
            lines.append(f"- **Size:** {len(buf['minibatch_ids'])} / {buf.get('trainset_size', 'N/A')} total")
            lines.append("")

        # =================================================================
        # Current evaluation (pre-mutation) — full outputs
        # =================================================================
        if "eval_current" in buf:
            ev = buf["eval_current"]
            lines.append("## Current Candidate Evaluation (pre-mutation)")
            lines.append("")
            scores = ev.get("scores", [])
            lines.append(f"- **Aggregate score:** {sum(scores):.4f}")
            lines.append(f"- **Per-example scores:** {scores}")
            if scores:
                failed = sum(1 for s in scores if s < 1.0)
                lines.append(f"- **Failed examples:** {failed}/{len(scores)}")
            lines.append("")

            # Full per-example outputs
            outputs = ev.get("outputs", [])
            if outputs:
                lines.append("### Per-Example Outputs")
                lines.append("")
                for idx, out in enumerate(outputs):
                    score = scores[idx] if idx < len(scores) else "N/A"
                    mark = "PASS" if isinstance(score, int | float) and score >= 1.0 else "FAIL"
                    lines.append(f"#### Example {idx} [{mark}] (score: {score})")
                    lines.append("")
                    lines.append("```json")
                    lines.append(json.dumps(_safe_json(out), indent=2, default=str))
                    lines.append("```")
                    lines.append("")

            # Full trajectories
            trajectories = ev.get("trajectories")
            if trajectories:
                lines.append("### Trajectories")
                lines.append("")
                for idx, traj in enumerate(trajectories):
                    lines.append(f"#### Trajectory {idx}")
                    lines.append("")
                    lines.append("```json")
                    lines.append(json.dumps(_safe_json(traj), indent=2, default=str))
                    lines.append("```")
                    lines.append("")

            # Objective scores
            obj_scores = ev.get("objective_scores")
            if obj_scores:
                lines.append("### Objective Scores")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(_safe_json(obj_scores), indent=2, default=str))
                lines.append("```")
                lines.append("")

        # Evaluation skipped
        if "evaluation_skipped" in buf:
            lines.append("## Evaluation Skipped")
            lines.append("")
            lines.append(f"- **Reason:** {buf['evaluation_skipped']['reason']}")
            lines.append("")

        # =================================================================
        # Reflective Dataset — FULL records, no truncation
        # =================================================================
        if "reflective_dataset" in buf:
            rd = buf["reflective_dataset"]
            lines.append("## Reflective Dataset")
            lines.append("")
            lines.append(f"- **Components to update:** {rd['components']}")
            for comp, records in rd.get("dataset", {}).items():
                lines.append(f"\n### Component: `{comp}` ({len(records)} records)")
                lines.append("")
                for idx, record in enumerate(records):
                    lines.append(f"#### Record {idx + 1}")
                    lines.append("")
                    for key, val in record.items():
                        val_str = str(val)
                        if len(val_str) > 500:
                            # For very long values, use a fenced block
                            lines.append(f"**{key}:**")
                            lines.append("")
                            lines.append("```")
                            lines.append(val_str)
                            lines.append("```")
                        else:
                            lines.append(f"- **{key}:** {val_str}")
                    lines.append("")

        # =================================================================
        # Memory State — show ALL entries, mark which were SELECTED
        # =================================================================
        memory_snapshots = buf.get("memory_snapshots", [])
        memory_queries = buf.get("memory_queries", [])
        memory_updates = buf.get("memory_updates", [])

        if memory_snapshots or memory_queries or memory_updates:
            lines.append("## Reflection Memory")
            lines.append("")

            # Build set of selected entry IDs for highlighting
            selected_entry_ids: set[str] = set()
            for mq in memory_queries:
                selected_entry_ids.update(str(eid) for eid in mq.get("selected_entry_ids", []))

            # Show before-proposal snapshot with ALL entries
            before_snapshots = [s for s in memory_snapshots if s["phase"] == "before_proposal"]
            if before_snapshots:
                snap = before_snapshots[0]
                util_pct = snap["total_entries"] / snap["max_entries"] * 100 if snap["max_entries"] > 0 else 0
                lines.append(f"### Memory State Before Proposal ({snap['total_entries']}/{snap['max_entries']} entries, {util_pct:.0f}% utilization)")
                lines.append("")
                lines.append(f"- **Accepted ratio:** {snap['accepted_ratio']:.1%}")
                lines.append(f"- **Rejected ratio:** {snap['rejected_ratio']:.1%}")
                lines.append(f"- **Entries by component:** {snap['entries_by_component']}")
                lines.append("")

                if snap["all_entries"]:
                    lines.append("| # | Selected? | Entry ID | Iter | Component | Status | Score | Intent | Lesson | Categories Succeeded | Categories Failed | Change Summary |")
                    lines.append("|---|-----------|----------|------|-----------|--------|-------|--------|--------|---------------------|-------------------|----------------|")
                    for i, entry in enumerate(snap["all_entries"], 1):
                        status = "ACCEPTED" if entry.get("accepted") else "REJECTED"
                        entry_id = str(entry.get("entry_id") or "")
                        was_selected = bool(entry_id and entry_id in selected_entry_ids)
                        selected_mark = "**YES**" if was_selected else ""
                        score_before = entry.get("score_before", 0)
                        score_after = entry.get("score_after", 0)
                        intent = entry.get("intent", "")
                        lesson = entry.get("lesson", "")
                        cats_ok = ", ".join(entry.get("categories_succeeded", []))
                        cats_fail = ", ".join(entry.get("categories_failed", []))
                        change = entry.get("change_summary", "")
                        lines.append(
                            f"| {i} | {selected_mark} | {entry_id or 'N/A'} | {entry.get('iteration', '?')} | {entry.get('component_name', '?')} "
                            f"| {status} | {score_before:.2f}->{score_after:.2f} | {intent} | {lesson} | {cats_ok} | {cats_fail} | {change} |"
                        )
                    lines.append("")

                    # Detailed view of each entry (full failure_modes, etc.)
                    lines.append("#### Full Memory Entry Details")
                    lines.append("")
                    for i, entry in enumerate(snap["all_entries"], 1):
                        entry_id = str(entry.get("entry_id") or "")
                        was_selected = bool(entry_id and entry_id in selected_entry_ids)
                        tag = " **(SELECTED FOR INJECTION)**" if was_selected else ""
                        lines.append("<details>")
                        lines.append(
                            f"<summary>Entry {i} — {entry_id or 'N/A'}, Iter {entry.get('iteration', '?')}, {entry.get('component_name', '?')}{tag}</summary>"
                        )
                        lines.append("")
                        lines.append("```json")
                        lines.append(json.dumps(_safe_json(entry), indent=2, default=str))
                        lines.append("```")
                        lines.append("")
                        lines.append("</details>")
                        lines.append("")
                else:
                    lines.append("*Memory is empty*")
                    lines.append("")

            # Show the exact text injected into prompts
            for mq in memory_queries:
                lines.append(f"### Memory Injected into Prompt for `{mq['component']}`")
                lines.append("")
                n_returned = len(mq.get("entries_returned", []))
                lines.append(f"**Entries selected:** {n_returned}")
                if mq.get("selected_entry_ids"):
                    lines.append(f"**Selected entry IDs:** {mq['selected_entry_ids']}")
                lines.append(f"**Text length:** {mq['formatted_text_length']} chars")
                lines.append("")
                if mq["formatted_text"]:
                    lines.append("Full injected text:")
                    lines.append("")
                    lines.append("```")
                    lines.append(mq["formatted_text"])
                    lines.append("```")
                    lines.append("")

                    # Also list exactly which entries were selected
                    if mq.get("entries_returned"):
                        lines.append("**Selected entries (in order):**")
                        lines.append("")
                        for idx, entry in enumerate(mq["entries_returned"], 1):
                            status = "ACCEPTED" if entry.get("accepted") else "REJECTED"
                            entry_id = entry.get("entry_id", "N/A")
                            lines.append(
                                f"{idx}. {entry_id} | Iter {entry.get('iteration', '?')} [{status}] "
                                f"— {entry.get('intent') or entry.get('change_summary', '')}"
                            )
                        lines.append("")
                else:
                    lines.append("*No memory entries matched this component*")
                    lines.append("")

            # Show updates made this iteration
            if memory_updates:
                lines.append("### Memory Updates This Iteration")
                lines.append("")
                for mu in memory_updates:
                    entry = mu["entry"]
                    status = "ACCEPTED" if entry.get("accepted") else "REJECTED"
                    lines.append(f"#### New Entry: `{mu['component']}` [{status}]")
                    lines.append("")
                    lines.append(f"- **Entry ID:** {entry.get('entry_id', 'N/A')}")
                    lines.append(f"- **Iteration:** {entry.get('iteration')}")
                    lines.append(f"- **Score:** {entry.get('score_before', 0):.2f} -> {entry.get('score_after', 0):.2f}")
                    lines.append(f"- **Intent:** {entry.get('intent', '')}")
                    lines.append(f"- **Lesson:** {entry.get('lesson', '')}")
                    lines.append(f"- **Categories succeeded:** {entry.get('categories_succeeded', [])}")
                    lines.append(f"- **Categories failed:** {entry.get('categories_failed', [])}")
                    lines.append(f"- **Referenced memory IDs:** {entry.get('referenced_memory_entry_ids', [])}")
                    lines.append(f"- **Reused memory intents:** {entry.get('reused_memory_intents', [])}")
                    lines.append(f"- **Reused memory categories:** {entry.get('reused_memory_categories', [])}")
                    lines.append(f"- **Change summary:** {entry.get('change_summary', '')}")
                    lines.append("- **Failure modes:**")
                    for fm in entry.get("failure_modes", []):
                        lines.append(f"  - {fm}")
                    lines.append(f"- **Memory utilization:** {mu['size_after']} entries ({mu['utilization']:.0%})")
                    if mu["evicted"]:
                        evicted = mu["evicted"]
                        lines.append(f"- **Evicted entry:** iter={evicted.get('iteration')}, component={evicted.get('component_name')}")
                        lines.append("")
                        lines.append("  <details>")
                        lines.append("  <summary>Evicted entry details</summary>")
                        lines.append("")
                        lines.append("  ```json")
                        lines.append("  " + json.dumps(_safe_json(evicted), indent=2, default=str).replace("\n", "\n  "))
                        lines.append("  ```")
                        lines.append("")
                        lines.append("  </details>")
                    lines.append("")

            # Show after-proposal snapshot
            after_snapshots = [s for s in memory_snapshots if s["phase"] == "after_proposal"]
            if after_snapshots:
                snap = after_snapshots[0]
                util_pct = snap["total_entries"] / snap["max_entries"] * 100 if snap["max_entries"] > 0 else 0
                lines.append(f"### Memory State After Proposal ({snap['total_entries']}/{snap['max_entries']} entries, {util_pct:.0f}% utilization)")
                lines.append("")
                lines.append(f"- **Accepted ratio:** {snap['accepted_ratio']:.1%}")
                lines.append(f"- **Rejected ratio:** {snap['rejected_ratio']:.1%}")
                lines.append("")

        # =================================================================
        # LLM Proposal — full prompts and responses, zero truncation
        # =================================================================
        if "proposal_traces" in buf:
            lines.append("## LLM Reflection Calls")
            lines.append("")
            for comp_name, trace in buf["proposal_traces"].items():
                lines.append(f"### Component: `{comp_name}`")
                lines.append("")
                lines.append(f"- **Model:** {trace['model_id']}")
                lines.append(f"- **Latency:** {trace['latency_ms']:.0f}ms")
                lines.append(f"- **Memory injected:** {trace['memory_was_injected']}")
                lines.append(f"- **Memory selected entry IDs:** {trace.get('memory_selected_entry_ids', [])}")
                lines.append(f"- **Memory reused intents:** {trace.get('memory_reused_intents', [])}")
                lines.append(f"- **Memory reused categories:** {trace.get('memory_reused_categories', [])}")
                lines.append(f"- **Memory reuse detected:** {trace.get('memory_reuse_detected', False)}")
                lines.append(f"- **Prompt length:** {len(trace['rendered_prompt'])} chars")
                lines.append(f"- **Response length:** {len(trace['raw_response'])} chars")
                lines.append("")

                lines.append("#### Prompt Template")
                lines.append("")
                lines.append("```")
                lines.append(trace["prompt_template"])
                lines.append("```")
                lines.append("")

                lines.append("#### Full Rendered Prompt (sent to LLM)")
                lines.append("")
                lines.append("````")
                lines.append(trace["rendered_prompt"])
                lines.append("````")
                lines.append("")

                lines.append("#### Full Raw LLM Response")
                lines.append("")
                lines.append("````")
                lines.append(trace["raw_response"])
                lines.append("````")
                lines.append("")

                lines.append("#### Extracted Instruction")
                lines.append("")
                lines.append("```")
                lines.append(trace["extracted_instruction"])
                lines.append("```")
                lines.append("")

        # =================================================================
        # Full Candidate Texts — before AND after
        # =================================================================
        if "proposal_parent" in buf and "new_instructions" in buf:
            lines.append("## Full Candidate Texts")
            lines.append("")
            for comp_name in buf["new_instructions"]:
                old_text = buf["proposal_parent"].get(comp_name, "")
                new_text = buf["new_instructions"][comp_name]

                lines.append(f"### Component: `{comp_name}`")
                lines.append("")
                lines.append("#### Before (current)")
                lines.append("")
                lines.append("```")
                lines.append(old_text)
                lines.append("```")
                lines.append("")
                lines.append("#### After (proposed)")
                lines.append("")
                lines.append("```")
                lines.append(new_text)
                lines.append("```")
                lines.append("")

        # Before/After Diff
        if "proposal_parent" in buf and "new_instructions" in buf:
            lines.append("## Before/After Diff")
            lines.append("")
            for comp_name, new_text in buf["new_instructions"].items():
                old_text = buf["proposal_parent"].get(comp_name, "")
                lines.append(f"### Component: `{comp_name}`")
                lines.append("")
                diff = difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"{comp_name} (before)",
                    tofile=f"{comp_name} (after)",
                    lineterm="",
                )
                diff_text = "\n".join(diff)
                if diff_text:
                    lines.append("```diff")
                    lines.append(diff_text)
                    lines.append("```")
                else:
                    lines.append("*No changes*")
                lines.append("")

        # =================================================================
        # Proposed candidate evaluation — full outputs
        # =================================================================
        if "eval_proposed" in buf:
            ev = buf["eval_proposed"]
            lines.append("## Proposed Candidate Evaluation (post-mutation)")
            lines.append("")
            scores = ev.get("scores", [])
            lines.append(f"- **Aggregate score:** {sum(scores):.4f}")
            lines.append(f"- **Per-example scores:** {scores}")
            lines.append("")

            # Score comparison
            if "eval_current" in buf:
                old_scores = buf["eval_current"].get("scores", [])
                if old_scores and scores and len(old_scores) == len(scores):
                    lines.append("### Per-Example Score Comparison")
                    lines.append("")
                    lines.append("| Example | Old | New | Delta |")
                    lines.append("|---------|-----|-----|-------|")
                    for j, (old_s, new_s) in enumerate(zip(old_scores, scores, strict=False)):
                        delta = new_s - old_s
                        sign = "+" if delta > 0 else ""
                        lines.append(f"| {j} | {old_s:.4f} | {new_s:.4f} | {sign}{delta:.4f} |")
                    lines.append("")

            # Full per-example outputs
            outputs = ev.get("outputs", [])
            if outputs:
                lines.append("### Per-Example Outputs (post-mutation)")
                lines.append("")
                for idx, out in enumerate(outputs):
                    score = scores[idx] if idx < len(scores) else "N/A"
                    mark = "PASS" if isinstance(score, int | float) and score >= 1.0 else "FAIL"
                    lines.append(f"#### Example {idx} [{mark}] (score: {score})")
                    lines.append("")
                    lines.append("```json")
                    lines.append(json.dumps(_safe_json(out), indent=2, default=str))
                    lines.append("```")
                    lines.append("")

        # =================================================================
        # Decision
        # =================================================================
        if "decision" in buf:
            dec = buf["decision"]
            lines.append("## Acceptance Decision")
            lines.append("")
            if dec["accepted"]:
                lines.append(f"**ACCEPTED** — new candidate #{dec.get('new_candidate_idx', '?')} (score: {dec.get('new_score', 'N/A')})")
            else:
                lines.append(f"**REJECTED** — old: {dec.get('old_score', 'N/A')}, new: {dec.get('new_score', 'N/A')}, reason: {dec.get('reason', 'N/A')}")
            if dec.get("decision_reason"):
                lines.append(f"- **Decision reason:** `{dec['decision_reason']}`")
            if dec.get("parent_ids") is not None:
                lines.append(f"- **Parent IDs:** {dec.get('parent_ids', [])}")
            if dec.get("threshold") is not None:
                lines.append(f"- **Threshold:** {dec.get('threshold'):.4f}")
            if dec.get("delta") is not None:
                lines.append(f"- **Delta:** {dec.get('delta'):+.4f}")
            lines.append("")

        # =================================================================
        # Lesson Generated
        # =================================================================
        lessons = buf.get("lessons", [])
        if lessons:
            lines.append("## Lesson Generated")
            lines.append("")
            for ls in lessons:
                delta = ls["score_after"] - ls["score_before"]
                status = "ACCEPTED" if ls["accepted"] else "REJECTED"
                fallback = " (V1 fallback)" if ls["fallback_used"] else " (V2 LLM)"
                lines.append(f"### Component: `{ls['component_name']}` [{status}, delta: {delta:+.2f}]{fallback}")
                lines.append("")
                lines.append(f"- **Score:** {ls['score_before']:.2f} -> {ls['score_after']:.2f}")
                lines.append(f"- **Latency:** {ls['latency_ms']:.0f}ms")
                if ls["intent"]:
                    lines.append(f"- **Intent:** {ls['intent']}")
                if ls["lesson"]:
                    lines.append(f"- **Lesson:** {ls['lesson']}")
                if ls["categories_succeeded"]:
                    lines.append(f"- **Categories succeeded:** {', '.join(ls['categories_succeeded'])}")
                if ls["categories_failed"]:
                    lines.append(f"- **Categories failed:** {', '.join(ls['categories_failed'])}")
                lines.append(f"- **Referenced memory entry IDs:** {ls.get('memory_selected_entry_ids', [])}")
                lines.append(f"- **Reused memory intents:** {ls.get('memory_reused_intents', [])}")
                lines.append(f"- **Reused memory categories:** {ls.get('memory_reused_categories', [])}")
                lines.append(f"- **Memory reuse detected:** {ls.get('memory_reuse_detected', False)}")
                lines.append("")

        # =================================================================
        # Pareto front
        # =================================================================
        if "pareto_update" in buf:
            pu = buf["pareto_update"]
            lines.append("## Pareto Front Update")
            lines.append("")
            lines.append(f"- **Front members:** {pu['new_front']}")
            lines.append(f"- **Front size:** {len(pu['new_front'])}")
            if pu["displaced"]:
                lines.append(f"- **Displaced:** {pu['displaced']}")
            lines.append("")

        # =================================================================
        # Valset evaluation — full per-example scores
        # =================================================================
        if "valset_eval" in buf:
            ve = buf["valset_eval"]
            lines.append("## Validation Set Evaluation")
            lines.append("")
            lines.append(f"- **Candidate:** #{ve['candidate_idx']}")
            lines.append(f"- **Average score:** {ve['average_score']:.4f}")
            lines.append(f"- **Examples evaluated:** {ve['num_examples']}/{ve['total_valset_size']}")
            lines.append(f"- **Is best program:** {ve['is_best_program']}")
            lines.append("")

            # Per-example valset scores
            scores_by_id = ve.get("scores_by_val_id")
            if scores_by_id:
                lines.append("### Per-Example Validation Scores")
                lines.append("")
                lines.append("| Example ID | Score |")
                lines.append("|------------|-------|")
                for eid, score in scores_by_id.items():
                    mark = "PASS" if isinstance(score, int | float) and score >= 1.0 else "FAIL"
                    lines.append(f"| {eid} | {score} [{mark}] |")
                lines.append("")

            # Per-example valset outputs
            outputs_by_id = ve.get("outputs_by_val_id")
            if outputs_by_id:
                lines.append("### Per-Example Validation Outputs")
                lines.append("")
                for eid, out in outputs_by_id.items():
                    score = scores_by_id.get(str(eid), "?") if scores_by_id else "?"
                    lines.append("<details>")
                    lines.append(f"<summary>Example {eid} (score: {score})</summary>")
                    lines.append("")
                    lines.append("```json")
                    lines.append(json.dumps(_safe_json(out), indent=2, default=str))
                    lines.append("```")
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")

        # =================================================================
        # Merge
        # =================================================================
        if "merge" in buf:
            m = buf["merge"]
            lines.append("## Merge")
            lines.append("")
            lines.append(f"- **Parents:** {m.get('parent_ids', 'N/A')}")
            if m.get("accepted"):
                lines.append(f"- **Result:** ACCEPTED (new candidate #{m.get('new_candidate_idx', '?')})")
            else:
                lines.append(f"- **Result:** REJECTED (reason: {m.get('reason', 'N/A')})")
            lines.append("")

        # =================================================================
        # Budget
        # =================================================================
        if "budget" in buf:
            b = buf["budget"]
            lines.append("## Budget")
            lines.append("")
            lines.append(f"- **Metric calls used:** {b['metric_calls_used']}")
            lines.append(f"- **Delta this iteration:** {b['metric_calls_delta']}")
            lines.append(f"- **Remaining:** {b['metric_calls_remaining']}")
            lines.append("")

        # =================================================================
        # Error
        # =================================================================
        if "error" in buf:
            lines.append("## Error")
            lines.append("")
            lines.append(f"- **Exception:** {buf['error']['exception']}")
            lines.append(f"- **Will continue:** {buf['error']['will_continue']}")
            lines.append("")

        # =================================================================
        # Navigation footer
        # =================================================================
        lines.append("---")
        lines.append("")
        if iteration > 1:
            lines.append(f"[< Previous iteration](iteration_{iteration - 1:03d}.md)")
        lines.append("[Index](../index.md)")
        lines.append(f"[Next iteration >](iteration_{iteration + 1:03d}.md)")
        lines.append("")

        # Write file
        path = os.path.join(self.output_dir, "iterations", f"iteration_{iteration:03d}.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))
