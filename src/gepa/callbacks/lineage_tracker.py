# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""LineageTracker — builds a structured ancestry graph of all candidates.

Produces:
    lineage.jsonl       — one JSON line per candidate
    lineage_graph.json  — machine-readable adjacency list
    lineage_tree.md     — human-readable tree of the best candidate's ancestry
"""

from __future__ import annotations

import json
import os
from typing import Any

from gepa.core.callbacks import (
    CandidateAcceptedEvent,
    CandidateSelectedEvent,
    MergeAcceptedEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ParetoFrontUpdatedEvent,
    ProposalEndEvent,
    ValsetEvaluatedEvent,
)


class LineageTracker:
    """Tracks candidate lineage and produces ancestry reports at end of run.

    Args:
        output_dir: Base run directory. Files written directly here.
    """

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self._lineage_jsonl = open(os.path.join(output_dir, "lineage.jsonl"), "a")

        # Candidate records
        self._candidates: list[dict[str, Any]] = []
        self._scores: dict[int, float] = {}
        self._pareto_front: list[int] = []

        # Per-iteration state
        self._current_selected_idx = 0
        self._current_selected_score = 0.0
        self._current_new_instructions: dict[str, str] = {}
        self._current_components: list[str] = []

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        record = {
            "candidate_idx": 0,
            "parent_idxs": [],
            "iteration": 0,
            "operation": "seed",
            "components_changed": list(event["seed_candidate"].keys()),
        }
        self._candidates.append(record)
        self._lineage_jsonl.write(json.dumps(record) + "\n")
        self._lineage_jsonl.flush()

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        self._current_selected_idx = event["candidate_idx"]
        self._current_selected_score = event["score"]

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        self._current_new_instructions = event["new_instructions"]
        self._current_components = list(event["new_instructions"].keys())

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        record = {
            "candidate_idx": event["new_candidate_idx"],
            "parent_idxs": list(event["parent_ids"]),
            "iteration": event["iteration"],
            "operation": "mutation",
            "components_changed": self._current_components,
            "score": event["new_score"],
            "parent_score": self._current_selected_score,
            "score_delta": event["new_score"] - self._current_selected_score,
        }
        self._candidates.append(record)
        self._scores[event["new_candidate_idx"]] = event["new_score"]
        self._lineage_jsonl.write(json.dumps(record) + "\n")
        self._lineage_jsonl.flush()

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        record = {
            "candidate_idx": event["new_candidate_idx"],
            "parent_idxs": list(event["parent_ids"]),
            "iteration": event["iteration"],
            "operation": "merge",
            "components_changed": [],
        }
        self._candidates.append(record)
        self._lineage_jsonl.write(json.dumps(record) + "\n")
        self._lineage_jsonl.flush()

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        self._scores[event["candidate_idx"]] = event["average_score"]

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._pareto_front = event["new_front"]

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._lineage_jsonl.close()

        # Write adjacency list
        graph: dict[str, Any] = {"candidates": {}, "pareto_front": self._pareto_front}
        for rec in self._candidates:
            idx = rec["candidate_idx"]
            graph["candidates"][str(idx)] = {
                "parents": rec["parent_idxs"],
                "iteration": rec["iteration"],
                "operation": rec["operation"],
                "score": self._scores.get(idx),
            }
        with open(os.path.join(self.output_dir, "lineage_graph.json"), "w") as f:
            json.dump(graph, f, indent=2)

        # Write lineage tree
        best_idx = event["best_candidate_idx"]
        self._write_lineage_tree(best_idx)

    def _write_lineage_tree(self, best_idx: int) -> None:
        """Write a human-readable ancestry tree rooted at the best candidate."""
        # Build parent->children mapping
        children: dict[int, list[int]] = {}
        record_by_idx: dict[int, dict[str, Any]] = {}
        for rec in self._candidates:
            idx = rec["candidate_idx"]
            record_by_idx[idx] = rec
            for parent in rec["parent_idxs"]:
                children.setdefault(parent, []).append(idx)

        lines: list[str] = ["# Candidate Lineage Tree", ""]
        lines.append(f"Best candidate: #{best_idx} (score: {self._scores.get(best_idx, '?')})")
        lines.append(f"Pareto front: {self._pareto_front}")
        lines.append("")

        # Trace ancestry of best
        ancestry: list[int] = []
        current = best_idx
        visited: set[int] = set()
        while current is not None and current not in visited:
            visited.add(current)
            ancestry.append(current)
            rec = record_by_idx.get(current)
            if rec and rec["parent_idxs"]:
                current = rec["parent_idxs"][0]
            else:
                break
        ancestry.reverse()

        lines.append("## Ancestry of Best Candidate")
        lines.append("")
        for depth, idx in enumerate(ancestry):
            rec = record_by_idx.get(idx, {})
            score = self._scores.get(idx, "?")
            op = rec.get("operation", "?")
            iteration = rec.get("iteration", "?")
            comps = rec.get("components_changed", [])
            prefix = "  " * depth + ("└── " if depth > 0 else "")
            marker = " ★ BEST" if idx == best_idx else ""
            comp_str = f" [{', '.join(comps)}]" if comps and op != "seed" else ""
            lines.append(f"{prefix}#{idx} ({op} iter {iteration}, score={score}{comp_str}){marker}")

        lines.append("")

        # Full tree from root
        lines.append("## Full Lineage Tree")
        lines.append("")

        def _render_tree(idx: int, prefix: str, is_last: bool) -> None:
            rec = record_by_idx.get(idx, {})
            score = self._scores.get(idx, "?")
            op = rec.get("operation", "?")
            iteration = rec.get("iteration", "?")
            connector = "└── " if is_last else "├── "
            marker = " ★" if idx == best_idx else ""
            pareto = " (P)" if idx in self._pareto_front else ""
            lines.append(f"{prefix}{connector}#{idx} ({op} i{iteration}, s={score}{pareto}){marker}")

            child_prefix = prefix + ("    " if is_last else "│   ")
            kids = children.get(idx, [])
            for i, child in enumerate(kids):
                _render_tree(child, child_prefix, i == len(kids) - 1)

        if 0 in record_by_idx:
            score = self._scores.get(0, "?")
            lines.append(f"#0 (seed, score={score})")
            kids = children.get(0, [])
            for i, child in enumerate(kids):
                _render_tree(child, "", i == len(kids) - 1)

        with open(os.path.join(self.output_dir, "lineage_tree.md"), "w") as f:
            f.write("\n".join(lines))
