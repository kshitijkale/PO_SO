# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""RunIndexLogger — master navigation index for GEPA optimization runs.

Writes a single ``run_log.md`` that acts as a structured table of contents
for a run, plus per-question JSON files and a full prompt tree.

Output files:
    run_log.md          — master navigation index (appended per iteration)
    questions/          — per-question JSON files for minibatch evals
    valset/             — per-question JSON files for valset evals
    ledger/             — ledger text injected into reflection prompts
    prompt_tree.md      — full prompt tree written at optimization end
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from gepa.core.callbacks import (
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    CandidateSelectedEvent,
    DiaryInjectedEvent,
    EvaluationEndEvent,
    EvaluationStartEvent,
    IterationEndEvent,
    IterationStartEvent,
    LedgerInjectedEvent,
    MinibatchSampledEvent,
    OptimizationEndEvent,
    OptimizationStartEvent,
    ProposalTraceEvent,
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
        if obj != obj:  # NaN
            return None
        return obj
    if isinstance(obj, int | str | bool | type(None)):
        return obj
    type_name = type(obj).__name__
    if type_name == "GEPAState":
        return "<GEPAState>"
    return str(obj)


class RunIndexLogger:
    """Writes run_log.md, per-question files, ledger files, and prompt_tree.md.

    Provides a single master navigation index for a GEPA optimization run.
    All files contain only pointers and summaries — actual data lives in
    existing log artifacts (states/, llm_calls/, log.jsonl).

    Args:
        output_dir: Directory for all output files. Created if missing.
    """

    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        for subdir in ["questions", "valset", "ledger"]:
            os.makedirs(os.path.join(output_dir, subdir), exist_ok=True)

        self._run_log = open(os.path.join(output_dir, "run_log.md"), "w")
        self._iter_buf: dict[str, Any] = {}

        # Candidate registry: idx → {parent_ids, val_score, iteration, accepted, mb_score, label}
        self._candidates: dict[int, dict[str, Any]] = {
            0: {
                "parent_ids": [],
                "val_score": None,
                "iteration": 0,
                "accepted": True,
                "mb_score": None,
                "label": "seed",
                "rejected_iters": [],
            }
        }
        self._best_idx: int = 0
        self._best_val_score: float = 0.0
        self._total_iters: int = 0

    # =========================================================================
    # Optimization Lifecycle
    # =========================================================================

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._run_log.write(f"# Run Log — {self.output_dir}\n\n")
        self._run_log.write(f"Started: {time.strftime('%Y-%m-%dT%H:%M:%S')}  \n")
        self._run_log.write(f"Trainset size: {event['trainset_size']}  \n")
        self._run_log.write(f"Valset size: {event['valset_size']}  \n")
        self._run_log.write("\n---\n\n")
        self._run_log.flush()

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._iter_buf = {
            "iteration": event["iteration"],
            "curr_inputs": None,
            "new_inputs": None,
            "minibatch_ids": None,
            "ledger": {},
            "llm_calls": {},
            "decision": None,
            "selected_idx": None,
            "selected_score": None,
            "mb_scores_current": None,
            "mb_scores_proposed": None,
            "val_avg": None,
            "val_candidate_idx": None,
            "val_scores_by_id": None,
            "val_num_examples": None,
        }

    # =========================================================================
    # Candidate Selection and Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        self._iter_buf["selected_idx"] = event["candidate_idx"]
        self._iter_buf["selected_score"] = event["score"]

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        self._iter_buf["minibatch_ids"] = event["minibatch_ids"]

    # =========================================================================
    # Evaluation Events
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        if event["capture_traces"]:
            self._iter_buf["curr_inputs"] = _safe_json(event["inputs"])
        else:
            self._iter_buf["new_inputs"] = _safe_json(event["inputs"])

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        iteration = event["iteration"]
        scores = event["scores"]
        outputs = _safe_json(event["outputs"])
        candidate_idx = event["candidate_idx"]

        if event["capture_traces"]:
            # Current candidate evaluation — write per-question minibatch files
            self._iter_buf["mb_scores_current"] = scores
            inputs = self._iter_buf.get("curr_inputs") or []
            minibatch_ids = self._iter_buf.get("minibatch_ids") or []
            for pos, (qid, inp, out, score) in enumerate(
                zip(minibatch_ids, inputs, outputs, scores, strict=False)
            ):
                record: dict[str, Any] = {
                    "iteration": iteration,
                    "position": pos,
                    "question_id": str(qid),
                    "candidate_idx": candidate_idx,
                    "score": score,
                    "input": inp,
                    "output": out,
                }
                fname = f"iter_{iteration:03d}_mb_{pos}_{qid}.json"
                fpath = os.path.join(self.output_dir, "questions", fname)
                with open(fpath, "w") as f:
                    json.dump(_safe_json(record), f, indent=2, default=str)
        else:
            # Proposed candidate evaluation
            self._iter_buf["mb_scores_proposed"] = scores

    # =========================================================================
    # Ledger Event
    # =========================================================================

    def on_ledger_injected(self, event: LedgerInjectedEvent) -> None:
        iteration = event["iteration"]
        comp = event["component_name"]
        self._iter_buf["ledger"][comp] = {
            "num_entries": event["num_entries"],
            "char_count": event["char_count"],
            "parent_hash": event["parent_hash"],
        }
        # Write ledger text file
        fname = f"iter_{iteration:03d}_{comp}.txt"
        fpath = os.path.join(self.output_dir, "ledger", fname)
        with open(fpath, "w") as f:
            f.write(f"# Rejection Ledger — iter {iteration}, component: {comp}\n")
            f.write(f"# entries: {event['num_entries']}  chars: {event['char_count']}\n")
            f.write(f"# parent_hash: {event['parent_hash']}\n\n")
            f.write(event["rendered_text"])

    # =========================================================================
    # Diary Event
    # =========================================================================

    def on_diary_injected(self, event: DiaryInjectedEvent) -> None:
        iteration = event["iteration"]
        comp = event["component_name"]
        self._iter_buf.setdefault("diary", {})[comp] = {
            "total_entries": event["total_entries"],
            "char_count": event["char_count"],
            "layer2_active": event["layer2_active"],
        }
        # Write diary text file
        diary_dir = os.path.join(self.output_dir, "diary")
        os.makedirs(diary_dir, exist_ok=True)
        fname = f"iter_{iteration:03d}_{comp}.txt"
        fpath = os.path.join(diary_dir, fname)
        with open(fpath, "w") as f:
            f.write(f"# Optimization Diary — iter {iteration}, component: {comp}\n")
            f.write(f"# entries: {event['total_entries']}  chars: {event['char_count']}\n")
            f.write(f"# layer2_active: {event['layer2_active']}\n\n")
            f.write(event["rendered_text"])

    # =========================================================================
    # Proposal Trace
    # =========================================================================

    def on_proposal_trace(self, event: ProposalTraceEvent) -> None:
        iteration = event["iteration"]
        comp = event["component_name"]
        self._iter_buf["llm_calls"][comp] = {
            "prompt_file": f"llm_calls/iter_{iteration:03d}_{comp}_prompt.txt",
            "response_file": f"llm_calls/iter_{iteration:03d}_{comp}_response.txt",
        }

    # =========================================================================
    # Acceptance/Rejection
    # =========================================================================

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        new_idx = event["new_candidate_idx"]
        new_score = event["new_score"]
        parent_ids = list(event["parent_ids"])
        mb_scores = self._iter_buf.get("mb_scores_proposed") or []
        mb_sum = sum(mb_scores) if mb_scores else None
        self._iter_buf["decision"] = {
            "accepted": True,
            "new_candidate_idx": new_idx,
            "new_score": new_score,
            "parent_ids": parent_ids,
        }
        self._candidates[new_idx] = {
            "parent_ids": parent_ids,
            "val_score": None,
            "iteration": event["iteration"],
            "accepted": True,
            "mb_score": mb_sum,
            "label": "mutation" if len(parent_ids) <= 1 else "merge",
            "rejected_iters": [],
        }

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        mb_scores = self._iter_buf.get("mb_scores_proposed") or []
        mb_sum = sum(mb_scores) if mb_scores else None
        selected_idx = self._iter_buf.get("selected_idx")
        self._iter_buf["decision"] = {
            "accepted": False,
            "old_score": event["old_score"],
            "new_score": event["new_score"],
            "reason": event["reason"],
            "parent_ids": [selected_idx] if selected_idx is not None else [],
            "mb_score_proposed": mb_sum,
        }
        # Record rejected attempt on the parent candidate
        if selected_idx is not None and selected_idx in self._candidates:
            self._candidates[selected_idx]["rejected_iters"].append(event["iteration"])

    # =========================================================================
    # Valset Evaluation
    # =========================================================================

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        iteration = event["iteration"]
        candidate_idx = event["candidate_idx"]
        avg = event["average_score"]
        scores_by_id = event.get("scores_by_val_id") or {}
        inputs_by_id = event.get("inputs_by_val_id") or {}
        outputs_by_id = event.get("outputs_by_val_id") or {}

        self._iter_buf["val_avg"] = avg
        self._iter_buf["val_candidate_idx"] = candidate_idx
        self._iter_buf["val_scores_by_id"] = scores_by_id
        self._iter_buf["val_num_examples"] = event["num_examples_evaluated"]

        if event["is_best_program"]:
            self._best_idx = candidate_idx
            self._best_val_score = avg

        # Update candidate registry val score
        if candidate_idx in self._candidates:
            self._candidates[candidate_idx]["val_score"] = avg
        elif candidate_idx == 0:
            self._candidates[0]["val_score"] = avg

        # Write per-val-id files
        for val_id, score in scores_by_id.items():
            record = {
                "iteration": iteration,
                "candidate_idx": candidate_idx,
                "val_id": str(val_id),
                "score": score,
                "input": _safe_json(inputs_by_id.get(val_id)),
                "output": _safe_json(outputs_by_id.get(val_id)),
            }
            fname = f"iter_{iteration:03d}_val_{val_id}.json"
            fpath = os.path.join(self.output_dir, "valset", fname)
            with open(fpath, "w") as f:
                json.dump(_safe_json(record), f, indent=2, default=str)

    # =========================================================================
    # Iteration End — flush iteration section to run_log.md
    # =========================================================================

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._total_iters += 1
        iteration = event["iteration"]
        buf = self._iter_buf
        lines: list[str] = []

        lines.append(f"## Iteration {iteration}")
        lines.append("")

        # Selected candidate
        sel_idx = buf.get("selected_idx")
        sel_score = buf.get("selected_score")
        if sel_idx is not None:
            score_str = f"{sel_score:.3f}" if isinstance(sel_score, float) else str(sel_score)
            state_file = f"states/iter_{iteration:03d}_02_selection_and_minibatch.json"
            lines.append(f"- **Selected**: prompt #{sel_idx} (val={score_str}) → `{state_file}`")

        # Minibatch table
        mb_ids = buf.get("minibatch_ids") or []
        mb_scores = buf.get("mb_scores_current") or []
        if mb_ids:
            lines.append(f"- **Minibatch** ({len(mb_ids)} examples): IDs {mb_ids}")
            lines.append("")
            lines.append("  | # | ID | Score | File |")
            lines.append("  |---|-----|-------|------|")
            for pos, qid in enumerate(mb_ids):
                score = mb_scores[pos] if pos < len(mb_scores) else "N/A"
                score_str = f"{score:.3f}" if isinstance(score, float) else str(score)
                fname = f"questions/iter_{iteration:03d}_mb_{pos}_{qid}.json"
                lines.append(f"  | {pos} | {qid} | {score_str} | `{fname}` |")
            lines.append("")

        # Ledger injections
        ledger = buf.get("ledger") or {}
        if ledger:
            for comp, info in ledger.items():
                num_e = info.get("num_entries", 0)
                char_c = info.get("char_count", 0)
                lfile = f"ledger/iter_{iteration:03d}_{comp}.txt"
                lines.append(f"- **Ledger injected** ({comp}): {num_e} entries, {char_c} chars → `{lfile}`")
        else:
            lines.append("- **Ledger injected**: 0 entries (first iteration or no prior rejections)")

        # LLM calls
        llm_calls = buf.get("llm_calls") or {}
        for comp, paths in llm_calls.items():
            lines.append(f"- **LLM call** ({comp}): `{paths['prompt_file']}` → `{paths['response_file']}`")

        # Decision
        decision = buf.get("decision")
        if decision:
            dec_file = f"states/iter_{iteration:03d}_08_decision.json"
            mb_current = buf.get("mb_scores_current") or []
            mb_proposed = buf.get("mb_scores_proposed") or []
            old_mb_sum = sum(mb_current)
            new_mb_sum = sum(mb_proposed)
            n = len(mb_ids) if mb_ids else max(len(mb_current), len(mb_proposed))
            if decision.get("accepted"):
                new_idx = decision.get("new_candidate_idx", "?")
                lines.append(
                    f"- **New prompt score**: {new_mb_sum:.0f}/{n}"
                    f" (old: {old_mb_sum:.0f}/{n}) → **ACCEPTED** as #{new_idx}"
                    f" | full decision: `{dec_file}`"
                )
            else:
                lines.append(
                    f"- **New prompt score**: {new_mb_sum:.0f}/{n}"
                    f" (old: {old_mb_sum:.0f}/{n}) → **REJECTED**"
                    f" | full decision: `{dec_file}`"
                )

        # Valset evaluation
        val_avg = buf.get("val_avg")
        val_num = buf.get("val_num_examples")
        val_scores = buf.get("val_scores_by_id") or {}
        if val_avg is not None:
            val_file = f"states/iter_{iteration:03d}_10_valset.json"
            lines.append(f"- **Valset run**: avg={val_avg:.3f}, {val_num} examples → `{val_file}`")
            if val_scores:
                lines.append("")
                lines.append("  | ID | Score | File |")
                lines.append("  |----|-------|------|")
                for val_id, score in val_scores.items():
                    score_str = f"{score:.3f}" if isinstance(score, float) else str(score)
                    vfname = f"valset/iter_{iteration:03d}_val_{val_id}.json"
                    lines.append(f"  | {val_id} | {score_str} | `{vfname}` |")
                lines.append("")

        lines.append("")
        self._run_log.write("\n".join(lines) + "\n")
        self._run_log.flush()

    # =========================================================================
    # Optimization End — write footer and prompt_tree.md
    # =========================================================================

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        total_iters = event["total_iterations"]
        total_calls = event["total_metric_calls"]

        self._run_log.write("---\n\n")
        self._run_log.write(f"**Total iterations:** {total_iters}  \n")
        self._run_log.write(f"**Total metric calls:** {total_calls}  \n")
        self._run_log.write(f"**Best candidate:** #{self._best_idx} (val={self._best_val_score:.3f})  \n")
        self._run_log.flush()
        self._run_log.close()

        self._write_prompt_tree()

    def _write_prompt_tree(self) -> None:
        """Write prompt_tree.md showing all accepted candidates in a tree."""
        best_idx = self._best_idx

        # Build adjacency: parent → list of children
        children: dict[int, list[int]] = {}
        for idx, info in self._candidates.items():
            for parent_id in info.get("parent_ids") or []:
                children.setdefault(int(parent_id), []).append(idx)

        lines: list[str] = []
        lines.append("# Prompt Tree")
        lines.append("")
        lines.append(f"Best: #{best_idx} (val={self._best_val_score:.3f}) | Generated: {time.strftime('%Y-%m-%d')}")
        lines.append("")

        def _render(idx: int, prefix: str, is_last: bool) -> None:
            info = self._candidates.get(idx, {})
            val_score = info.get("val_score")
            mb_score = info.get("mb_score")
            iteration = info.get("iteration", 0)
            rejected_iters = info.get("rejected_iters") or []

            if val_score is not None:
                score_str = f"val={val_score:.3f}"
            elif mb_score is not None:
                score_str = f"mb={mb_score:.1f}(mb)"
            else:
                score_str = "val=?"

            best_mark = " ← BEST" if idx == best_idx else ""
            connector = "└── " if is_last else "├── "

            if idx == 0:
                lines.append(f"#{idx} seed  {score_str}")
            else:
                iter_str = f"iter={iteration}"
                lines.append(f"{prefix}{connector}#{idx}  {score_str}  {iter_str}  accepted ✓{best_mark}")

            # Show rejected attempts from this candidate as its children
            new_prefix = prefix + ("    " if is_last else "│   ")
            kids = sorted(children.get(idx, []))
            # Interleave by iteration order
            rej_sorted = sorted(rejected_iters)
            all_items: list[tuple[str, Any]] = []
            for ri in rej_sorted:
                all_items.append(("rejected", ri))
            for ki in kids:
                kid_iter = self._candidates.get(ki, {}).get("iteration", 0)
                all_items.append(("accepted", (ki, kid_iter)))
            all_items.sort(key=lambda x: x[1] if x[0] == "rejected" else x[1][1])

            for item_idx, (kind, val) in enumerate(all_items):
                child_is_last = item_idx == len(all_items) - 1
                if kind == "rejected":
                    rej_iter = val
                    rej_connector = "└── " if child_is_last else "├── "
                    lines.append(f"{new_prefix}{rej_connector}iter={rej_iter}  REJECTED")
                else:
                    ki, _ = val
                    _render(ki, new_prefix, child_is_last)

        _render(0, "", True)
        lines.append("")

        path = os.path.join(self.output_dir, "prompt_tree.md")
        with open(path, "w") as f:
            f.write("\n".join(lines))
