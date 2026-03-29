# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""Metrics rollup callback for machine-readable observability summaries."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from gepa.core.callbacks import (
    BudgetUpdatedEvent,
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    ErrorEvent,
    EvaluationEndEvent,
    EvaluationSkippedEvent,
    IterationEndEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ProposalTraceEvent,
    ValsetEvaluatedEvent,
)


class MetricsRollupLogger:
    """Aggregate optimization metrics and persist periodic rollups.

    Writes two files:
    - `metrics.jsonl`: append-only event-like snapshots as metrics evolve.
    - `metrics_summary.json`: final aggregate at run end.
    """

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self._metrics_path = os.path.join(output_dir, "metrics.jsonl")
        self._summary_path = os.path.join(output_dir, "metrics_summary.json")
        self._fh = open(self._metrics_path, "a", encoding="utf-8")
        self._start_monotonic = time.monotonic()

        self._state: dict[str, Any] = {
            "iterations": 0,
            "accepted": 0,
            "rejected": 0,
            "errors": 0,
            "metric_calls_used": 0,
            "metric_calls_delta_total": 0,
            "evaluation_runs": 0,
            "evaluation_skipped": 0,
            "evaluation_examples": 0,
            "evaluation_correct": 0,
            "evaluation_wrong": 0,
            "valset_runs": 0,
            "valset_examples": 0,
            "valset_correct": 0,
            "valset_wrong": 0,
            "seed_evaluation_runs": 0,
            "trace_evaluation_runs": 0,
            "cached_evaluation_runs": 0,
            "oi_calls": 0,
            "oi_fallback_calls": 0,
            "oi_outcomes_total": 0,
            "oi_evictions": 0,
            "oi_evicted_examples": 0,
            "memory_entries_added": 0,
            "lessons_generated": 0,
            "proposal_traces": 0,
            "proposal_latency_ms_total": 0.0,
            "proposal_latency_ms_max": 0.0,
        }

    def __del__(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    @staticmethod
    def _ts() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

    def _log(self, event_name: str, event: dict[str, Any] | None = None) -> None:
        payload = {
            "timestamp": self._ts(),
            "elapsed_s": round(time.monotonic() - self._start_monotonic, 3),
            "event": event_name,
            "metrics": self._state,
        }
        if event:
            payload["context"] = event
        self._fh.write(json.dumps(payload, default=str) + "\n")
        self._fh.flush()

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._log(
            "optimization_start",
            {
                "trainset_size": event.get("trainset_size"),
                "valset_size": event.get("valset_size"),
            },
        )

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._state["iterations"] += 1
        self._log("iteration_end", {"iteration": event.get("iteration")})

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        self._state["accepted"] += 1
        self._log(
            "candidate_accepted",
            {
                "iteration": event.get("iteration"),
                "new_candidate_idx": event.get("new_candidate_idx"),
            },
        )

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        self._state["rejected"] += 1
        self._log(
            "candidate_rejected",
            {
                "iteration": event.get("iteration"),
                "reason": event.get("reason", ""),
            },
        )

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        scores = event.get("scores", [])
        total = len(scores)
        correct = sum(1 for score in scores if float(score) >= 1.0)
        wrong = total - correct

        self._state["evaluation_runs"] += 1
        self._state["evaluation_examples"] += total
        self._state["evaluation_correct"] += correct
        self._state["evaluation_wrong"] += wrong

        if bool(event.get("is_seed_candidate", False)):
            self._state["seed_evaluation_runs"] += 1
        if bool(event.get("capture_traces", False)):
            self._state["trace_evaluation_runs"] += 1
        else:
            self._state["cached_evaluation_runs"] += 1

        self._log(
            "evaluation_end",
            {
                "iteration": event.get("iteration"),
                "candidate_idx": event.get("candidate_idx"),
                "correct": correct,
                "wrong": wrong,
                "total": total,
            },
        )

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        self._state["evaluation_skipped"] += 1
        self._log(
            "evaluation_skipped",
            {
                "iteration": event.get("iteration"),
                "candidate_idx": event.get("candidate_idx"),
                "reason": event.get("reason", ""),
            },
        )

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        scores_by_val_id = event.get("scores_by_val_id", {})
        scores = list(scores_by_val_id.values())
        total = len(scores)
        correct = sum(1 for score in scores if float(score) >= 1.0)
        wrong = total - correct

        self._state["valset_runs"] += 1
        self._state["valset_examples"] += total
        self._state["valset_correct"] += correct
        self._state["valset_wrong"] += wrong

        self._log(
            "valset_evaluated",
            {
                "iteration": event.get("iteration"),
                "candidate_idx": event.get("candidate_idx"),
                "correct": correct,
                "wrong": wrong,
                "total": total,
                "average_score": float(event.get("average_score", 0.0)),
            },
        )

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        delta = int(event.get("metric_calls_delta", 0))
        self._state["metric_calls_delta_total"] += delta
        self._state["metric_calls_used"] = int(event.get("metric_calls_used", self._state["metric_calls_used"]))
        self._log(
            "budget_updated",
            {
                "iteration": event.get("iteration"),
                "metric_calls_used": event.get("metric_calls_used"),
                "metric_calls_remaining": event.get("metric_calls_remaining"),
            },
        )

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        latency_ms = float(event.get("latency_ms", 0.0))
        self._state["proposal_traces"] += 1
        self._state["proposal_latency_ms_total"] += latency_ms
        self._state["proposal_latency_ms_max"] = max(self._state["proposal_latency_ms_max"], latency_ms)
        self._log(
            "proposal_trace",
            {
                "iteration": event.get("iteration"),
                "component_name": event.get("component_name"),
                "latency_ms": latency_ms,
            },
        )

    def on_outcome_interpreter_call(self, event: Any) -> None:
        self._state["oi_calls"] += 1
        if bool(event.get("fallback_used", False)):
            self._state["oi_fallback_calls"] += 1
        outcomes = event.get("outcomes", [])
        self._state["oi_outcomes_total"] += len(outcomes)
        self._log(
            "outcome_interpreter_call",
            {
                "iteration": event.get("iteration"),
                "node_id": event.get("node_id"),
                "fallback_used": bool(event.get("fallback_used", False)),
                "num_outcomes": len(outcomes),
            },
        )

    def on_oi_eviction_summary(self, event: Any) -> None:
        evicted_count = int(event.get("evicted_count", 0))
        self._state["oi_evictions"] += 1
        self._state["oi_evicted_examples"] += evicted_count
        self._log(
            "oi_eviction_summary",
            {
                "iteration": event.get("iteration"),
                "node_id": event.get("node_id"),
                "summary_type": event.get("summary_type"),
                "evicted_count": evicted_count,
            },
        )

    def on_memory_entry_added(self, event: Any) -> None:
        self._state["memory_entries_added"] += 1
        self._log(
            "memory_entry_added",
            {
                "iteration": event.get("iteration"),
                "component_name": event.get("component_name"),
            },
        )

    def on_lesson_generated(self, event: Any) -> None:
        self._state["lessons_generated"] += 1
        self._log(
            "lesson_generated",
            {
                "iteration": event.get("iteration"),
                "component_name": event.get("component_name"),
            },
        )

    def on_error(self, event: ErrorEvent) -> None:
        self._state["errors"] += 1
        self._log(
            "error",
            {
                "iteration": event.get("iteration"),
                "exception": str(event.get("exception", "")),
                "will_continue": bool(event.get("will_continue", False)),
            },
        )

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._state["metric_calls_used"] = int(event.get("total_metric_calls", self._state["metric_calls_used"]))
        elapsed_s = time.monotonic() - self._start_monotonic
        summary = {
            "timestamp": self._ts(),
            "elapsed_s": round(elapsed_s, 3),
            "best_candidate_idx": event.get("best_candidate_idx"),
            "total_iterations": event.get("total_iterations"),
            "total_metric_calls": event.get("total_metric_calls"),
            "metrics": self._state,
        }

        with open(self._summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)

        self._log("optimization_end", {"total_iterations": event.get("total_iterations")})
        self._fh.close()
