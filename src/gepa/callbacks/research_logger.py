# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""ResearchLogger — exhaustive per-iteration logging callback.

Writes structured markdown reports AND machine-readable JSONL for every
event during GEPA optimization. Designed for researchers who need full
transparency into every decision, every LLM call, every memory operation.

Output files:
    log.jsonl            — one JSON line per event (complete event stream)
    summary.jsonl        — one JSON line per iteration (key metrics)
    pareto_timeline.jsonl — Pareto front after each iteration
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
        })

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        self._iter_buf["minibatch_ids"] = event["minibatch_ids"]
        self._iter_buf["trainset_size"] = event["trainset_size"]
        self._log_event("minibatch_sampled", _safe_json(dict(event)))

    # =========================================================================
    # Evaluation Events
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        self._log_event("evaluation_start", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "batch_size": event["batch_size"],
            "capture_traces": event["capture_traces"],
            "is_seed_candidate": event["is_seed_candidate"],
        })

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        key = "eval_current" if event["capture_traces"] else "eval_proposed"
        self._iter_buf[key] = {
            "candidate_idx": event["candidate_idx"],
            "scores": event["scores"],
            "has_trajectories": event["has_trajectories"],
            "objective_scores": _safe_json(event["objective_scores"]),
        }
        self._log_event("evaluation_end", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "scores": event["scores"],
            "has_trajectories": event["has_trajectories"],
            "capture_traces": event.get("capture_traces"),
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
        }
        self._log_event("valset_evaluated", {
            "iteration": event["iteration"],
            "candidate_idx": event["candidate_idx"],
            "average_score": event["average_score"],
            "num_examples": event["num_examples_evaluated"],
            "is_best_program": event["is_best_program"],
        })

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
            "dataset_sizes": {k: len(v) for k, v in event["dataset"].items()},
        })

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        self._iter_buf["proposal_parent"] = event["parent_candidate"]
        self._iter_buf["proposal_components"] = event["components"]
        self._log_event("proposal_start", {
            "iteration": event["iteration"],
            "components": event["components"],
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
        }
        self._log_event("proposal_trace", {
            "iteration": event["iteration"],
            "component_name": event["component_name"],
            "model_id": event["model_id"],
            "latency_ms": round(event["latency_ms"], 1),
            "memory_was_injected": event["memory_was_injected"],
            "prompt_length": len(event["rendered_prompt"]),
            "response_length": len(event["raw_response"]),
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
        self._iter_buf["decision"] = {
            "accepted": True,
            "new_candidate_idx": event["new_candidate_idx"],
            "new_score": event["new_score"],
        }
        self._log_event("candidate_accepted", _safe_json(dict(event)))

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        self._iter_buf["decision"] = {
            "accepted": False,
            "old_score": event["old_score"],
            "new_score": event["new_score"],
            "reason": event["reason"],
        }
        self._log_event("candidate_rejected", _safe_json(dict(event)))

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
        # Don't log every budget update to avoid noise — they fire per-eval
        # The iteration-end summary captures the final budget state

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
            "accepted": event["entry"].get("accepted"),
            "score_delta": (event["entry"].get("score_after", 0) or 0) - (event["entry"].get("score_before", 0) or 0),
            "evicted": event["evicted_entry"] is not None,
            "size_after": event["memory_size_after"],
        }
        self._memory_events_jsonl.write(json.dumps(record) + "\n")
        self._memory_events_jsonl.flush()

    def on_memory_queried(self, event: MemoryQueriedEvent) -> None:
        self._iter_buf.setdefault("memory_queries", []).append({
            "component": event["component_name"],
            "entries_returned": len(event["entries_returned"]),
            "formatted_text": event["formatted_text"],
            "formatted_text_length": event["formatted_text_length"],
        })
        self._log_event("memory_queried", {
            "iteration": event["iteration"],
            "component": event["component_name"],
            "entries_returned": len(event["entries_returned"]),
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
            "entries_returned": len(event["entries_returned"]),
            "formatted_length": event["formatted_text_length"],
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
        })

        # Write full snapshot to file
        snapshot_path = os.path.join(
            self.output_dir, "memory", f"memory_state_iter_{event['iteration']:03d}_{event['phase']}.json"
        )
        with open(snapshot_path, "w") as f:
            json.dump(_safe_json(dict(event)), f, indent=2, default=str)

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
    # Helper: Write per-iteration markdown report
    # =========================================================================

    def _write_iteration_report(self, iteration: int) -> None:
        buf = self._iter_buf
        lines: list[str] = []

        lines.append(f"# Iteration {iteration}")
        lines.append("")
        lines.append(f"**Timestamp:** {buf.get('timestamp', 'N/A')}")
        lines.append(f"**Elapsed:** {self._elapsed()}")
        lines.append(f"**Iteration time:** {buf.get('iter_elapsed_s', 'N/A')}s")
        lines.append(f"**Total evals so far:** {buf.get('total_evals', 'N/A')}")
        lines.append(f"**Candidates in population:** {buf.get('num_candidates', 'N/A')}")
        lines.append(f"**Best score:** {self._best_score:.4f} (candidate #{self._best_candidate_idx})")
        lines.append(f"**Acceptance rate:** {self._total_accepted}/{self._total_iterations} ({self._total_accepted / self._total_iterations * 100:.1f}%)" if self._total_iterations > 0 else "")
        lines.append("")

        # Candidate selection
        if "selected_candidate_idx" in buf:
            lines.append("## Candidate Selection")
            lines.append("")
            lines.append(f"- **Selected:** candidate #{buf['selected_candidate_idx']} (score: {buf.get('selected_candidate_score', 'N/A'):.4f})")
            if "selected_candidate" in buf:
                for comp_name, comp_text in buf["selected_candidate"].items():
                    lines.append(f"- **Component `{comp_name}`:** {len(comp_text)} chars")
            lines.append("")

        # Minibatch
        if "minibatch_ids" in buf:
            lines.append("## Minibatch")
            lines.append("")
            lines.append(f"- **IDs:** {buf['minibatch_ids']}")
            lines.append(f"- **Size:** {len(buf['minibatch_ids'])} / {buf.get('trainset_size', 'N/A')} total")
            lines.append("")

        # Current evaluation
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

        # Evaluation skipped
        if "evaluation_skipped" in buf:
            lines.append("## Evaluation Skipped")
            lines.append("")
            lines.append(f"- **Reason:** {buf['evaluation_skipped']['reason']}")
            lines.append("")

        # Reflective dataset
        if "reflective_dataset" in buf:
            rd = buf["reflective_dataset"]
            lines.append("## Reflective Dataset")
            lines.append("")
            lines.append(f"- **Components to update:** {rd['components']}")
            for comp, records in rd.get("dataset", {}).items():
                lines.append(f"\n### Component: `{comp}` ({len(records)} records)")
                lines.append("")
                for idx, record in enumerate(records[:5]):  # Show first 5
                    lines.append(f"#### Record {idx + 1}")
                    for key, val in record.items():
                        val_str = str(val)
                        if len(val_str) > 500:
                            val_str = val_str[:500] + "..."
                        lines.append(f"- **{key}:** {val_str}")
                    lines.append("")
                if len(records) > 5:
                    lines.append(f"*... and {len(records) - 5} more records*")
                    lines.append("")

        # Memory state
        memory_snapshots = buf.get("memory_snapshots", [])
        memory_queries = buf.get("memory_queries", [])
        memory_updates = buf.get("memory_updates", [])

        if memory_snapshots or memory_queries or memory_updates:
            lines.append("## Reflection Memory")
            lines.append("")

            # Show before-proposal snapshot
            before_snapshots = [s for s in memory_snapshots if s["phase"] == "before_proposal"]
            if before_snapshots:
                snap = before_snapshots[0]
                lines.append(f"### Memory State ({snap['total_entries']}/{snap['max_entries']} entries, {snap['total_entries'] / snap['max_entries'] * 100:.0f}% utilization)" if snap["max_entries"] > 0 else f"### Memory State ({snap['total_entries']} entries)")
                lines.append("")
                if snap["all_entries"]:
                    lines.append("| # | Iter | Component | Change | Score | Status |")
                    lines.append("|---|------|-----------|--------|-------|--------|")
                    for i, entry in enumerate(snap["all_entries"], 1):
                        status = "ACCEPTED" if entry.get("accepted") else "REJECTED"
                        change = entry.get("change_summary", "")[:60]
                        lines.append(
                            f"| {i} | {entry.get('iteration', '?')} | {entry.get('component_name', '?')} "
                            f"| {change} | {entry.get('score_before', 0):.2f} -> {entry.get('score_after', 0):.2f} | {status} |"
                        )
                    lines.append("")
                else:
                    lines.append("*Memory is empty*")
                    lines.append("")

            # Show queries
            for mq in memory_queries:
                lines.append(f"### Memory Injected into Prompt for `{mq['component']}`")
                lines.append("")
                if mq["formatted_text"]:
                    lines.append(f"Entries used: {mq['entries_returned']} | Text length: {mq['formatted_text_length']} chars")
                    lines.append("")
                    for line in mq["formatted_text"].split("\n"):
                        lines.append(f"> {line}")
                    lines.append("")
                else:
                    lines.append("*No memory entries matched this component*")
                    lines.append("")

            # Show updates
            if memory_updates:
                lines.append("### Memory Updates This Iteration")
                lines.append("")
                for mu in memory_updates:
                    entry = mu["entry"]
                    status = "ACCEPTED" if entry.get("accepted") else "REJECTED"
                    lines.append(
                        f"- **Added:** iter={entry.get('iteration')}, component={mu['component']}, "
                        f"change=\"{entry.get('change_summary', '')[:80]}\", "
                        f"score: {entry.get('score_before', 0):.2f} -> {entry.get('score_after', 0):.2f}, {status}"
                    )
                    if mu["evicted"]:
                        evicted = mu["evicted"]
                        lines.append(f"  - *Evicted:* iter={evicted.get('iteration')}, component={evicted.get('component_name')}")
                    lines.append(f"  - *Memory utilization:* {mu['size_after']} entries ({mu['utilization']:.0%})")
                lines.append("")

        # LLM Proposal
        if "proposal_traces" in buf:
            lines.append("## LLM Reflection Calls")
            lines.append("")
            for comp_name, trace in buf["proposal_traces"].items():
                lines.append(f"### Component: `{comp_name}`")
                lines.append("")
                lines.append(f"- **Model:** {trace['model_id']}")
                lines.append(f"- **Latency:** {trace['latency_ms']:.0f}ms")
                lines.append(f"- **Memory injected:** {trace['memory_was_injected']}")
                lines.append("")
                lines.append("#### Rendered Prompt")
                lines.append("```")
                prompt_text = trace["rendered_prompt"]
                if len(prompt_text) > 3000:
                    lines.append(prompt_text[:3000] + "\n... [truncated]")
                else:
                    lines.append(prompt_text)
                lines.append("```")
                lines.append("")
                lines.append("#### Raw LLM Response")
                lines.append("```")
                response_text = trace["raw_response"]
                if len(response_text) > 3000:
                    lines.append(response_text[:3000] + "\n... [truncated]")
                else:
                    lines.append(response_text)
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

        # Proposed candidate evaluation
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

        # Decision
        if "decision" in buf:
            dec = buf["decision"]
            lines.append("## Acceptance Decision")
            lines.append("")
            if dec["accepted"]:
                lines.append(f"**ACCEPTED** — new candidate #{dec.get('new_candidate_idx', '?')} (score: {dec.get('new_score', 'N/A')})")
            else:
                lines.append(f"**REJECTED** — old: {dec.get('old_score', 'N/A')}, new: {dec.get('new_score', 'N/A')}, reason: {dec.get('reason', 'N/A')}")
            lines.append("")

        # Pareto front
        if "pareto_update" in buf:
            pu = buf["pareto_update"]
            lines.append("## Pareto Front Update")
            lines.append("")
            lines.append(f"- **Front members:** {pu['new_front']}")
            lines.append(f"- **Front size:** {len(pu['new_front'])}")
            if pu["displaced"]:
                lines.append(f"- **Displaced:** {pu['displaced']}")
            lines.append("")

        # Valset evaluation
        if "valset_eval" in buf:
            ve = buf["valset_eval"]
            lines.append("## Validation Set Evaluation")
            lines.append("")
            lines.append(f"- **Candidate:** #{ve['candidate_idx']}")
            lines.append(f"- **Average score:** {ve['average_score']:.4f}")
            lines.append(f"- **Examples evaluated:** {ve['num_examples']}/{ve['total_valset_size']}")
            lines.append(f"- **Is best program:** {ve['is_best_program']}")
            lines.append("")

        # Merge
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

        # Budget
        if "budget" in buf:
            b = buf["budget"]
            lines.append("## Budget")
            lines.append("")
            lines.append(f"- **Metric calls used:** {b['metric_calls_used']}")
            lines.append(f"- **Delta this iteration:** {b['metric_calls_delta']}")
            lines.append(f"- **Remaining:** {b['metric_calls_remaining']}")
            lines.append("")

        # Error
        if "error" in buf:
            lines.append("## Error")
            lines.append("")
            lines.append(f"- **Exception:** {buf['error']['exception']}")
            lines.append(f"- **Will continue:** {buf['error']['will_continue']}")
            lines.append("")

        # Write file
        path = os.path.join(self.output_dir, "iterations", f"iteration_{iteration:03d}.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))
