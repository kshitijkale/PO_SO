"""AIME light run — memory ON, 45 train / 10 val, small iteration budget.

Verifies that ReflectionMemory records and injects entries correctly on a real
math-reasoning task.  Uses gpt-4.1-mini for both solver and reflection LLM.

Usage
-----
    uv run python -m experiments.aime_memory.run_light

Config
------
  - 45 train / 10 val examples from AI-MO/aimo-validation-aime
  - max_candidate_proposals = 10   (light budget)
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

from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import math_metric, run_llm
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)

MODEL = "openai/gpt-4.1-mini"
RUN_DIR = "outputs/aime_light_memory_on"
MAX_PROPOSALS = 10
MINIBATCH_SIZE = 3
MAX_WORKERS = 8
TRAIN_SIZE = 45
VAL_SIZE = 10

INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def make_evaluator(solver_lm: dspy.LM):
    dspy.configure(lm=solver_lm)

    def evaluate(candidate: str, example) -> tuple[float, SideInfo]:
        prediction = run_llm(example, candidate)
        score, feedback = math_metric(example, prediction)
        return score, {
            "problem": example.problem,
            "prompt": candidate,
            "output": prediction.answer,
            "reasoning": getattr(prediction, "reasoning", ""),
            "Feedback": feedback,
        }

    return evaluate


# ---------------------------------------------------------------------------
# Memory report helpers (same as memory_check.py)
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
    evaluator = make_evaluator(solver_lm)

    # --- Config ---
    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=RUN_DIR,
            max_candidate_proposals=MAX_PROPOSALS,
            seed=0,
            display_progress_bar=True,
            parallel=True,
            max_workers=MAX_WORKERS,
            cache_evaluation=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=MODEL,
            reflection_minibatch_size=MINIBATCH_SIZE,
            use_reflection_memory=True,
            reflection_memory_max_entries=10,
        ),
        tracking=TrackingConfig(research_mode=True),
    )

    print(f"\n=== AIME Light Run — Memory ON ===")
    print(f"  solver LLM      : {MODEL}")
    print(f"  reflection LLM  : {MODEL}")
    print(f"  train / val     : {len(trainset)} / {len(valset)}")
    print(f"  max_proposals   : {MAX_PROPOSALS}")
    print(f"  minibatch_size  : {MINIBATCH_SIZE}")
    print(f"  max_workers     : {MAX_WORKERS}")
    print(f"  run_dir         : {RUN_DIR}")
    print()

    result = optimize_anything(
        seed_candidate=INITIAL_PROMPT,
        evaluator=evaluator,
        dataset=trainset,
        valset=valset,
        config=config,
        objective="Maximize accuracy on AIME math problems. The answer must be a single integer.",
    )

    print(f"\nBest candidate:\n{result.best_candidate}")

    print_memory_report(Path(RUN_DIR))


if __name__ == "__main__":
    main()
