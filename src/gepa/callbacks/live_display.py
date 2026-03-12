# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""LiveDisplay — rich terminal dashboard during optimization.

Shows a compact, live-updating summary after each iteration. Uses only
stdlib (ANSI escape codes). If ``rich`` is installed, falls back to
``rich.console`` for nicer rendering automatically.
"""

from __future__ import annotations

import sys
import time

from gepa.core.callbacks import (
    BudgetUpdatedEvent,
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    CandidateSelectedEvent,
    IterationEndEvent,
    MemoryStateSnapshotEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ParetoFrontUpdatedEvent,
    ProposalEndEvent,
)


class LiveDisplay:
    """Live terminal dashboard callback.

    Prints a compact status summary after each iteration.
    """

    def __init__(self) -> None:
        self._start_time = 0.0
        self._iteration = 0
        self._best_score = 0.0
        self._best_idx = 0
        self._pareto_size = 0
        self._total_accepted = 0
        self._total_iterations = 0
        self._last_action = ""
        self._score_history: list[float] = []
        self._discovery_markers: list[tuple[int, int]] = []  # (position, candidate_idx)
        self._budget_used = 0
        self._budget_remaining: int | None = None
        self._memory_size = 0
        self._memory_max = 0
        self._memory_accepted = 0
        self._memory_rejected = 0
        self._components_updated: list[str] = []
        self._selected_score = 0.0

    def _elapsed(self) -> str:
        secs = time.monotonic() - self._start_time
        m, s = divmod(int(secs), 60)
        return f"{m:02d}m {s:02d}s"

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._start_time = time.monotonic()

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        self._selected_score = event["score"]

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        self._components_updated = list(event["new_instructions"].keys())

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        self._total_accepted += 1
        new_score = event["new_score"]
        comps = ", ".join(self._components_updated) if self._components_updated else "?"
        self._last_action = f"ACCEPTED mutation on [{comps}] ({self._selected_score:.3f} -> {new_score:.3f})"
        if new_score > self._best_score:
            self._best_score = new_score
            self._best_idx = event["new_candidate_idx"]
            self._discovery_markers.append((len(self._score_history), event["new_candidate_idx"]))

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        self._last_action = f"REJECTED ({event['old_score']:.3f} vs {event['new_score']:.3f})"

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._pareto_size = len(event["new_front"])

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        self._budget_used = event["metric_calls_used"]
        self._budget_remaining = event["metric_calls_remaining"]

    def on_memory_state_snapshot(self, event: MemoryStateSnapshotEvent) -> None:
        if event["phase"] == "after_proposal":
            self._memory_size = event["total_entries"]
            self._memory_max = event["max_entries"]
            self._memory_accepted = int(event["accepted_ratio"] * event["total_entries"])
            self._memory_rejected = event["total_entries"] - self._memory_accepted

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._total_iterations += 1
        self._iteration = event["iteration"]
        self._score_history.append(self._best_score)
        self._render()

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _render(self) -> None:
        budget_str = f"{self._budget_used}"
        if self._budget_remaining is not None:
            budget_str += f"/{self._budget_used + self._budget_remaining}"
        budget_str += " evals"

        accept_rate = self._total_accepted / self._total_iterations * 100 if self._total_iterations > 0 else 0

        w = 70
        sep = "=" * w
        lines = [
            "",
            sep,
            f" GEPA  [iter {self._iteration}]  [budget: {budget_str}]  [{self._elapsed()}]",
            sep,
            f" Best: {self._best_score:.4f} (#{self._best_idx})  |  Pareto: {self._pareto_size} candidates",
            f" Last: {self._last_action}",
            "",
        ]

        # Score history (compact)
        if self._score_history:
            recent = self._score_history[-20:]
            scores_str = " ".join(f"{s:.2f}" for s in recent)
            lines.append(f" Score: {scores_str}")

        # Memory line
        if self._memory_max > 0:
            lines.append(
                f" Memory: {self._memory_size}/{self._memory_max} entries"
                f" | {self._memory_accepted} accepted, {self._memory_rejected} rejected"
            )

        lines.append(f" Accept rate: {self._total_accepted}/{self._total_iterations} ({accept_rate:.1f}%)")
        lines.append(sep)

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
