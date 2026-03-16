# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""MemV0Observer: Structured logging callback for the MemV0 memory system."""

from __future__ import annotations

import json
import os
from typing import Any


class MemV0Observer:
    """Observability callback for the MemV0 tree-based memory system.

    Prints human-readable output and writes structured JSONL logs for
    every OI call, eviction summary, tree mutation, memory render, and
    proposal trace event.
    """

    def __init__(self, log_dir: str | None = None, print_output: bool = True) -> None:
        self.print_output = print_output
        self._log_file: Any = None
        self._jsonl_file: Any = None

        if log_dir is not None:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "memv0_trace.log")
            jsonl_path = os.path.join(log_dir, "memv0_trace.jsonl")
            self._log_file = open(log_path, "a", encoding="utf-8")
            self._jsonl_file = open(jsonl_path, "a", encoding="utf-8")

    def __del__(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        if self._jsonl_file is not None:
            try:
                self._jsonl_file.close()
            except Exception:
                pass

    @staticmethod
    def _trunc(s: str, n: int = 100) -> str:
        """Truncate a string to n chars, appending ellipsis if truncated."""
        if len(s) <= n:
            return s
        return s[:n] + "…"

    def _emit(self, header: str, body: str, event_dict: dict[str, Any]) -> None:
        """Print and/or log the event."""
        if self.print_output:
            print(header)
            if body:
                print(body)

        if self._log_file is not None:
            self._log_file.write(header + "\n")
            if body:
                self._log_file.write(body + "\n")
            self._log_file.write("\n")
            self._log_file.flush()

        if self._jsonl_file is not None:
            self._jsonl_file.write(json.dumps(event_dict, default=str) + "\n")
            self._jsonl_file.flush()

    # -------------------------------------------------------------------------
    # ReflectiveDatasetBuilt — show examples
    # -------------------------------------------------------------------------

    def on_reflective_dataset_built(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        dataset: dict[str, list[dict[str, Any]]] = event.get("dataset", {})
        lines: list[str] = []
        for comp, records in dataset.items():
            lines.append(f"  Component: {comp}")
            for rec in records:
                inputs = self._trunc(str(rec.get("Inputs", "")))
                gen_out = self._trunc(str(rec.get("Generated Outputs", "")))
                feedback = str(rec.get("Feedback", ""))
                score = rec.get("score", rec.get("Score", "?"))
                lines.append(f"    score={score} | Q: {inputs} | ans: {gen_out} | fb: {feedback}")
        header = f"[MemV0] iter={iteration} REFLECTIVE DATASET BUILT"
        self._emit(header, "\n".join(lines), event)

    # -------------------------------------------------------------------------
    # OutcomeInterpreter call
    # -------------------------------------------------------------------------

    def on_outcome_interpreter_call(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        node_id = event.get("node_id", -1)
        fallback = event.get("fallback_used", False)
        num_records = event.get("num_records", 0)
        oi_prompt = event.get("oi_prompt", "")
        oi_raw_response = event.get("oi_raw_response", "")
        outcomes: list[dict[str, Any]] = event.get("outcomes", [])

        header = f"[MemV0] iter={iteration} node={node_id} OI CALL (fallback={fallback}, n={num_records})"
        lines: list[str] = []
        lines.append("--- OI Prompt ---")
        lines.append(oi_prompt)
        lines.append("--- OI Raw Response ---")
        lines.append(oi_raw_response)
        lines.append("--- Outcomes ---")
        for o in outcomes:
            q = o.get("question_summary", "")
            result = o.get("result", "")
            error_type = o.get("error_type", "")
            observation = o.get("observation", "")
            lines.append(f"  [{result}] {error_type} | Q: {q}")
            lines.append(f"    Observation: {observation}")

        self._emit(header, "\n".join(lines), event)

    # -------------------------------------------------------------------------
    # OI eviction summary
    # -------------------------------------------------------------------------

    def on_oi_eviction_summary(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        node_id = event.get("node_id", -1)
        summary_type = event.get("summary_type", "")
        evicted_count = event.get("evicted_count", 0)
        prompt = event.get("prompt", "")
        response = event.get("response", "")
        new_summary = event.get("new_summary", "")

        header = f"[MemV0] iter={iteration} node={node_id} OI EVICTION SUMMARY (type={summary_type}, evicted={evicted_count})"
        lines: list[str] = []
        lines.append("--- Eviction Prompt ---")
        lines.append(prompt)
        lines.append("--- Eviction Response ---")
        lines.append(response)
        lines.append("--- New Summary ---")
        lines.append(new_summary)

        self._emit(header, "\n".join(lines), event)

    # -------------------------------------------------------------------------
    # Memory tree updated
    # -------------------------------------------------------------------------

    def on_memory_tree_updated(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        operation = event.get("operation", "")
        node_id = event.get("node_id", -1)
        parent_id = event.get("parent_id")
        accepted = event.get("accepted")
        rejection_reason = event.get("rejection_reason", "")
        prompt = event.get("prompt", {})
        outcomes_added: list[Any] = event.get("outcomes_added", [])
        val_score = event.get("val_score")
        evicted_count = event.get("evicted_count", 0)
        new_node_summary = event.get("new_node_summary", "")
        tree_node_count = event.get("tree_node_count", 0)

        header = f"[MemV0] iter={iteration} TREE UPDATED op={operation} node={node_id} total_nodes={tree_node_count}"
        lines: list[str] = []
        lines.append(f"  parent_id={parent_id}")
        if accepted is not None:
            status = "accepted" if accepted else f"rejected ({rejection_reason})"
            lines.append(f"  status={status}")
        lines.append(f"  prompt={prompt}")
        if outcomes_added:
            lines.append(f"  outcomes_added={len(outcomes_added)}")
        if val_score is not None:
            lines.append(f"  val_score={val_score}")
        if operation == "eviction":
            lines.append(f"  evicted_count={evicted_count}")
            lines.append(f"  new_node_summary={new_node_summary}")

        self._emit(header, "\n".join(lines), event)

    # -------------------------------------------------------------------------
    # Memory rendered
    # -------------------------------------------------------------------------

    def on_memory_rendered(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        current_node_id = event.get("current_node_id", -1)
        rendered_text = event.get("rendered_text", "")
        char_count = event.get("char_count", 0)
        was_injected = event.get("was_injected", False)

        header = f"[MemV0] iter={iteration} node={current_node_id} MEMORY RENDERED (chars={char_count}, injected={was_injected})"
        body = rendered_text if rendered_text else "(empty)"

        self._emit(header, body, event)

    # -------------------------------------------------------------------------
    # Proposal trace — log only, no print
    # -------------------------------------------------------------------------

    def on_proposal_trace(self, event: dict[str, Any]) -> None:
        iteration = event.get("iteration", -1)
        component_name = event.get("component_name", "")
        rendered_prompt = event.get("rendered_prompt", "")
        raw_response = event.get("raw_response", "")
        extracted_instruction = event.get("extracted_instruction", "")
        latency_ms = event.get("latency_ms", 0.0)

        header = f"[MemV0] iter={iteration} component={component_name} PROPOSAL TRACE latency={latency_ms:.1f}ms"
        lines: list[str] = []
        lines.append("--- Rendered Prompt ---")
        lines.append(rendered_prompt)
        lines.append("--- Raw Response ---")
        lines.append(raw_response)
        lines.append("--- Extracted Instruction ---")
        lines.append(extracted_instruction)

        body = "\n".join(lines)

        # Write to files only — VerboseDisplay already prints this
        if self._log_file is not None:
            self._log_file.write(header + "\n")
            self._log_file.write(body + "\n\n")
            self._log_file.flush()

        if self._jsonl_file is not None:
            self._jsonl_file.write(json.dumps(event, default=str) + "\n")
            self._jsonl_file.flush()
