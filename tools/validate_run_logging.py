#!/usr/bin/env python3
"""Validate research logging artifacts for a GEPA run directory.

Usage:
    uv run python tools/validate_run_logging.py --run-dir outputs/my_run
    uv run python tools/validate_run_logging.py --run-dir outputs/my_run --strict

The validator focuses on research_mode output completeness and consistency:
- Required files/directories exist
- JSONL files parse cleanly
- Iteration markdown/state snapshots align
- LLM call traces have companion prompt/response files
- Memory/lesson artifacts are internally consistent
- Accepted candidates are persisted under candidates/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENT_REQUIRED = {"optimization_start", "optimization_end", "iteration_start", "iteration_end"}


@dataclass
class Issue:
    level: str  # "FAIL" | "WARN"
    message: str


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_no}: invalid JSON ({exc})")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"{path}:{line_no}: JSON line is not an object")
            continue
        rows.append(parsed)
    return rows, errors


def _parse_iteration_filename(path: Path) -> int | None:
    match = re.fullmatch(r"iteration_(\d+)\.md", path.name)
    if not match:
        return None
    return int(match.group(1))


def _parse_state_filename(path: Path) -> tuple[int, str] | None:
    match = re.fullmatch(r"iter_(\d{3})_(.+)\.json", path.name)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def _parse_candidate_filename(path: Path) -> int | None:
    match = re.fullmatch(r"candidate_(\d+)\.json", path.name)
    if not match:
        return None
    return int(match.group(1))


def _state_path(run_dir: Path, iteration: int, step: str) -> Path:
    return run_dir / "states" / f"iter_{iteration:03d}_{step}.json"


def validate_run_dir(run_dir: Path) -> tuple[list[Issue], dict[str, Any]]:
    issues: list[Issue] = []
    facts: dict[str, Any] = {}

    required_files = ["config.json", "index.md", "log.jsonl", "summary.jsonl"]
    required_dirs = ["iterations", "llm_calls", "memory", "states", "candidates"]

    for rel in required_files:
        path = run_dir / rel
        if not path.exists():
            issues.append(Issue("FAIL", f"Missing required file: {rel}"))
    for rel in required_dirs:
        path = run_dir / rel
        if not path.exists():
            issues.append(Issue("FAIL", f"Missing required directory: {rel}/"))

    if any(issue.level == "FAIL" for issue in issues):
        return issues, facts

    log_rows, log_errors = _read_jsonl(run_dir / "log.jsonl")
    summary_rows, summary_errors = _read_jsonl(run_dir / "summary.jsonl")
    memory_rows, memory_errors = _read_jsonl(run_dir / "memory" / "memory_events.jsonl")

    for err in log_errors + summary_errors + memory_errors:
        issues.append(Issue("FAIL", err))

    if not log_rows:
        issues.append(Issue("FAIL", "log.jsonl contains no events"))
    if not summary_rows:
        issues.append(Issue("FAIL", "summary.jsonl contains no iterations"))

    event_types = {str(row.get("event_type")) for row in log_rows if "event_type" in row}
    missing_required_events = sorted(EVENT_REQUIRED - event_types)
    if missing_required_events:
        issues.append(Issue("FAIL", f"log.jsonl missing required event_type(s): {missing_required_events}"))

    if "proposal_trace" not in event_types:
        issues.append(Issue("WARN", "No proposal_trace events found; reflection traceability is limited"))
    if "budget_updated" not in event_types:
        issues.append(Issue("WARN", "No budget_updated events in log.jsonl"))

    summary_iters: set[int] = set()
    for row in summary_rows:
        it = row.get("iteration")
        if not isinstance(it, int):
            issues.append(Issue("FAIL", f"summary.jsonl has row with non-int iteration: {row!r}"))
            continue
        summary_iters.add(it)

    iteration_files = sorted((run_dir / "iterations").glob("iteration_*.md"))
    md_iters = {it for p in iteration_files if (it := _parse_iteration_filename(p)) is not None}

    if summary_iters != md_iters:
        missing_md = sorted(summary_iters - md_iters)
        missing_summary = sorted(md_iters - summary_iters)
        if missing_md:
            issues.append(Issue("FAIL", f"Missing iteration markdown files for iterations: {missing_md}"))
        if missing_summary:
            issues.append(Issue("WARN", f"Iteration markdown exists without summary rows: {missing_summary}"))

    # Validate state snapshots
    state_files = list((run_dir / "states").glob("iter_*.json"))
    state_index: dict[int, set[str]] = {}
    for path in state_files:
        parsed = _parse_state_filename(path)
        if parsed is None:
            issues.append(Issue("WARN", f"Unrecognized state file name: {path.name}"))
            continue
        it, step = parsed
        state_index.setdefault(it, set()).add(step)

    for it in sorted(summary_iters):
        needed = {"01_iteration_start", "12_iteration_end", "02_selection_and_minibatch", "08_decision"}
        available = state_index.get(it, set())
        missing = sorted(needed - available)
        if missing:
            issues.append(Issue("FAIL", f"states/ missing required steps for iter {it}: {missing}"))

    # Validate llm_calls triplets
    proposal_rows = [row for row in log_rows if row.get("event_type") == "proposal_trace"]
    for row in proposal_rows:
        iteration = row.get("iteration")
        component = row.get("component_name")
        if not isinstance(iteration, int) or not isinstance(component, str):
            issues.append(Issue("FAIL", f"Invalid proposal_trace row: {row!r}"))
            continue
        base = f"iter_{iteration:03d}_{component}"
        files = [
            run_dir / "llm_calls" / f"{base}.json",
            run_dir / "llm_calls" / f"{base}_prompt.txt",
            run_dir / "llm_calls" / f"{base}_response.txt",
        ]
        for file_path in files:
            if not file_path.exists():
                issues.append(Issue("FAIL", f"Missing llm_calls companion file: {file_path.relative_to(run_dir)}"))

    # Validate memory events + prompt injections
    valid_memory_events = {"add", "query"}
    for row in memory_rows:
        event = row.get("event")
        if event not in valid_memory_events:
            issues.append(Issue("FAIL", f"Unknown memory event type in memory_events.jsonl: {event!r}"))
            continue
        if event == "query":
            iteration = row.get("iteration")
            component = row.get("component")
            formatted = row.get("formatted_text", "")
            if not isinstance(iteration, int) or not isinstance(component, str):
                issues.append(Issue("FAIL", f"Malformed memory query row: {row!r}"))
                continue
            if isinstance(formatted, str) and formatted:
                prompt_path = run_dir / "memory" / "prompts_with_memory" / f"iter_{iteration:03d}_{component}.txt"
                if not prompt_path.exists():
                    rel = prompt_path.relative_to(run_dir)
                    issues.append(Issue("FAIL", f"Missing memory-injected prompt file: {rel}"))

    lesson_rows: list[dict[str, Any]] = []
    lesson_path = run_dir / "memory" / "lesson_events.jsonl"
    if lesson_path.exists():
        lesson_rows, lesson_errors = _read_jsonl(lesson_path)
        for err in lesson_errors:
            issues.append(Issue("FAIL", err))

    lesson_log_rows = [row for row in log_rows if row.get("event_type") == "lesson_generated"]
    if lesson_log_rows and not lesson_path.exists():
        issues.append(Issue("FAIL", "lesson_generated exists in log.jsonl but memory/lesson_events.jsonl is missing"))
    if lesson_path.exists() and len(lesson_rows) != len(lesson_log_rows):
        issues.append(
            Issue(
                "FAIL",
                "Mismatch in lesson event counts: "
                f"log.jsonl has {len(lesson_log_rows)} vs lesson_events.jsonl has {len(lesson_rows)}",
            )
        )

    # Validate memory snapshot files for each snapshot event
    snapshot_rows = [row for row in log_rows if row.get("event_type") == "memory_state_snapshot"]
    for row in snapshot_rows:
        iteration = row.get("iteration")
        phase = row.get("phase")
        if not isinstance(iteration, int) or not isinstance(phase, str):
            issues.append(Issue("FAIL", f"Malformed memory_state_snapshot row: {row!r}"))
            continue
        file_path = run_dir / "memory" / f"memory_state_iter_{iteration:03d}_{phase}.json"
        if not file_path.exists():
            rel = file_path.relative_to(run_dir)
            issues.append(Issue("FAIL", f"Missing memory snapshot file: {rel}"))

    # Validate candidate snapshots for accepted proposals
    candidate_files = list((run_dir / "candidates").glob("candidate_*.json"))
    candidate_ids = {cid for p in candidate_files if (cid := _parse_candidate_filename(p)) is not None}
    if 0 not in candidate_ids:
        issues.append(Issue("FAIL", "Missing seed candidate file: candidates/candidate_000.json"))

    accepted_rows = [row for row in log_rows if row.get("event_type") == "candidate_accepted"]
    accepted_ids: set[int] = set()
    for row in accepted_rows:
        idx = row.get("new_candidate_idx")
        if isinstance(idx, int):
            accepted_ids.add(idx)
        else:
            issues.append(Issue("FAIL", f"candidate_accepted row missing int new_candidate_idx: {row!r}"))
    missing_candidate_files = sorted(accepted_ids - candidate_ids)
    if missing_candidate_files:
        issues.append(Issue("FAIL", f"Missing candidate file(s) for accepted ids: {missing_candidate_files}"))

    # Facts for report
    facts["event_count"] = len(log_rows)
    facts["iteration_count"] = len(summary_iters)
    facts["accepted_count"] = len(accepted_rows)
    facts["proposal_trace_count"] = len(proposal_rows)
    facts["memory_event_count"] = len(memory_rows)
    facts["lesson_event_count"] = len(lesson_rows)
    facts["candidate_file_count"] = len(candidate_ids)

    return issues, facts


def _print_report(run_dir: Path, issues: list[Issue], facts: dict[str, Any]) -> None:
    fails = [i for i in issues if i.level == "FAIL"]
    warns = [i for i in issues if i.level == "WARN"]

    print(f"Run directory: {run_dir}")
    print(
        "Summary: "
        f"{len(fails)} fail(s), {len(warns)} warning(s), "
        f"{facts.get('iteration_count', 0)} iteration(s), {facts.get('event_count', 0)} event(s)"
    )
    if facts:
        print(
            "Counts: "
            f"accepted={facts.get('accepted_count', 0)}, "
            f"proposal_trace={facts.get('proposal_trace_count', 0)}, "
            f"memory_events={facts.get('memory_event_count', 0)}, "
            f"lesson_events={facts.get('lesson_event_count', 0)}, "
            f"candidate_files={facts.get('candidate_file_count', 0)}"
        )

    if not issues:
        print("PASS: Logging artifacts are consistent and complete for research analysis.")
        return

    if fails:
        print("\nFailures:")
        for issue in fails:
            print(f"- {issue.message}")
    if warns:
        print("\nWarnings:")
        for issue in warns:
            print(f"- {issue.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate GEPA research logging artifacts in a run directory.")
    parser.add_argument("--run-dir", type=Path, required=True, help="Path to GEPA run output directory.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir.exists() or not run_dir.is_dir():
        print(f"FAIL: run directory does not exist or is not a directory: {run_dir}", file=sys.stderr)
        return 2

    issues, facts = validate_run_dir(run_dir)
    _print_report(run_dir, issues, facts)

    has_fail = any(i.level == "FAIL" for i in issues)
    has_warn = any(i.level == "WARN" for i in issues)
    if has_fail:
        return 1
    if args.strict and has_warn:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
