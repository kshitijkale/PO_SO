"""AIME light run — memory ON, 45 train / 10 val, small iteration budget.

Verifies that ReflectionMemory records and injects entries correctly on a real
math-reasoning task.  Uses gpt-4.1-mini for both solver and reflection LLM.

Usage
-----
    uv run python -m experiments.aime_memory.run_light

Config
------
  - 45 train / 10 val examples from AI-MO/aimo-validation-aime
  - max_metric_calls = 200  (light budget)
  - reflection_minibatch_size = 3
  - parallel = True, max_workers = 8
  - memory ON, max_entries = 10
  - research_mode = True  (full logging to run_dir)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import dspy
from dotenv import load_dotenv

from experiments.aime_memory.adapter import AIMEAdapter
from experiments.aime_memory.dataset import load_aime_dataset
from gepa.api import optimize

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)

MODEL = "openai/gpt-4.1-mini"
RUN_DIR = "outputs/aime_light_memory_on"
MAX_METRIC_CALLS = 200
MINIBATCH_SIZE = 3
MAX_WORKERS = 8
TRAIN_SIZE = 45
VAL_SIZE = 10

INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)


# ---------------------------------------------------------------------------
# Memory report helpers
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return lines


def print_memory_report(run_dir: Path) -> None:
    print("\n" + "=" * 60)
    print("  MEMORY VERIFICATION REPORT")
    print("=" * 60)

    # --- Memory events ---
    events = load_jsonl(run_dir / "memory" / "memory_events.jsonl")
    add_events = [e for e in events if e.get("event") == "add"]
    query_events = [e for e in events if e.get("event") == "query"]
    injected = [e for e in query_events if (e.get("entries_returned") or 0) > 0]

    print(f"\n[Memory Events]  total={len(events)}  adds={len(add_events)}  queries={len(query_events)}  injected_queries={len(injected)}")
    for ev in events:
        ev_type = ev.get("event", "?")
        if ev_type == "add":
            print(
                f"  iter {ev.get('iteration'):>3}  ADD    {ev.get('component', '?'):<25}"
                f"  delta={ev.get('score_delta', 0):+.3f}"
                f"  {'ACCEPTED' if ev.get('accepted') else 'REJECTED'}"
                f"  size={ev.get('size_after')}"
            )
        elif ev_type == "query":
            n = ev.get("entries_returned", 0)
            print(
                f"  iter {ev.get('iteration'):>3}  QUERY  {ev.get('component', '?'):<25}"
                f"  returned={n}"
                f"  {'→ INJECTED' if n > 0 else '→ empty'}"
            )

    # --- Injection in LLM prompts ---
    llm_dir = run_dir / "llm_calls"
    prompt_files = sorted(llm_dir.glob("iter_*_prompt.txt")) if llm_dir.exists() else []
    injected_files = [p for p in prompt_files if "## Optimization History" in p.read_text()]
    print(f"\n[Prompt Injection]  {len(injected_files)}/{len(prompt_files)} prompt files contain '## Optimization History'")

    if injected_files:
        pf = injected_files[0]
        text = pf.read_text()
        idx = text.index("## Optimization History")
        snippet = text[idx: idx + 500].replace("\n", "\n    ")
        print(f"\n  First injected prompt: {pf.name}")
        print(f"    ---\n    {snippet}\n    ---")

    # --- Summary stats ---
    rows = load_jsonl(run_dir / "summary.jsonl")
    if rows:
        accepted = sum(1 for r in rows if r.get("accepted", False))
        total = len(rows)
        best = max((float(r["best_score"]) for r in rows if r.get("best_score") is not None), default=None)
        print(f"\n[Run Summary]  iterations={total}  accepted={accepted}/{total} ({accepted/total:.0%})  best_val={best}")

    # --- Checklist ---
    print("\n[Checklist]")
    print(f"  [{'✓' if add_events else '✗'}] Memory entries recorded    : {len(add_events)} adds")
    print(f"  [{'✓' if injected else '✗'}] Memory injected into prompts: {len(injected)} queries with entries")
    print(f"  [{'✓' if injected_files else '✗'}] '## Optimization History' in LLM prompts: {len(injected_files)} files")

    all_ok = bool(add_events and injected and injected_files)
    print(f"\n{'✓ Memory is recording and injecting correctly.' if all_ok else '✗ Memory check failed — see above.'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Set it in the environment or in .claude/.env before running this script."
        )

    # --- Dataset: 45 train, 10 val ---
    print("Loading AIME dataset…")
    trainset_full, valset_full, _ = load_aime_dataset()
    trainset = trainset_full[:TRAIN_SIZE]
    valset = valset_full[:VAL_SIZE]
    print(f"  train={len(trainset)}  val={len(valset)}")

    # --- Solver LM ---
    solver_lm = dspy.LM(MODEL, api_key=api_key, temperature=0.7, max_tokens=32000)

    # --- Adapter ---
    adapter = AIMEAdapter(solver_lm=solver_lm, max_workers=MAX_WORKERS)

    seed_candidate = {AIMEAdapter.COMPONENT_NAME: INITIAL_PROMPT}

    print(f"\n=== AIME Light Run — Memory ON ===")
    print(f"  solver LLM      : {MODEL}")
    print(f"  reflection LLM  : {MODEL}")
    print(f"  train / val     : {len(trainset)} / {len(valset)}")
    print(f"  max_metric_calls: {MAX_METRIC_CALLS}")
    print(f"  minibatch_size  : {MINIBATCH_SIZE}")
    print(f"  max_workers     : {MAX_WORKERS}")
    print(f"  run_dir         : {RUN_DIR}")
    print()

    result = optimize(
        seed_candidate=seed_candidate,
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_lm=MODEL,
        max_metric_calls=MAX_METRIC_CALLS,
        reflection_minibatch_size=MINIBATCH_SIZE,
        run_dir=RUN_DIR,
        cache_evaluation=True,
        seed=0,
        research_mode=True,
        display_progress_bar=True,
        use_reflection_memory=True,
        reflection_memory_max_entries=10,
        objective="Maximize accuracy on AIME math problems. The answer must be a single integer.",
    )

    best_candidate = result.best_candidate
    assert isinstance(best_candidate, dict)
    print(f"\nBest candidate:\n{best_candidate[AIMEAdapter.COMPONENT_NAME]}")

    print_memory_report(Path(RUN_DIR))


if __name__ == "__main__":
    main()
