"""Reflection Memory v1 verification experiment.

Runs a small part-of-speech tagging task (strict exact-match scoring) under
two conditions — memory ON and memory OFF — with the same seed, then prints
a side-by-side report that confirms:

  1. Memory entries are recorded after every iteration (memory_events.jsonl)
  2. Memory text is injected into the LLM prompt on iteration 2+ (llm_calls/)
  3. Acceptance-rate and score curves are comparable (or better) with memory

Usage
-----
    uv run python -m experiments.smoke_test.memory_check
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import litellm
from dotenv import load_dotenv

from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env")

TASK_MODEL = "openai/gpt-4.1-mini"
REFLECTION_MODEL = "openai/gpt-4.1-mini"
MAX_PROPOSALS = 5
SEED = 0

# ---------------------------------------------------------------------------
# Deliberately weak initial prompt — forces failures on strict exact-match
# The model will say "This is an adjective" instead of just "adjective"
# ---------------------------------------------------------------------------
INITIAL_PROMPT = "Identify this word."

CATEGORIES = {"noun", "verb", "adjective", "adverb"}

# 3 train / 3 val / 3 test
TRAINSET = [
    {"word": "happy", "answer": "adjective"},
    {"word": "run", "answer": "verb"},
    {"word": "quickly", "answer": "adverb"},
]
VALSET = [
    {"word": "dog", "answer": "noun"},
    {"word": "beautiful", "answer": "adjective"},
    {"word": "speak", "answer": "verb"},
]
TESTSET = [
    {"word": "slowly", "answer": "adverb"},
    {"word": "cat", "answer": "noun"},
    {"word": "think", "answer": "verb"},
]


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _call_llm(system_prompt: str, user_msg: str) -> str:
    resp: Any = litellm.completion(  # type: ignore[assignment]
        model=TASK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=64,
        temperature=0.0,
    )
    return resp.choices[0].message.content.strip()


def evaluate(candidate: str, example: dict) -> tuple[float, SideInfo]:
    """Score candidate prompt on one POS-tagging example (strict exact match)."""
    output = _call_llm(candidate, f"Word: {example['word']}")
    predicted = output.strip().lower()
    expected = example["answer"].lower()

    # Strict: 1.0 only if output IS exactly the category word
    score = 1.0 if predicted == expected else 0.0

    feedback = (
        f"Correct." if score == 1.0
        else f"Expected exactly '{expected}' but got '{output}'. "
             f"Output must be a single word from: noun, verb, adjective, adverb."
    )

    return score, {
        "word": example["word"],
        "expected": expected,
        "output": output,
        "predicted": predicted,
        "score": score,
        "Feedback": feedback,
    }


def evaluate_dataset(prompt: str, dataset: list[dict]) -> float:
    scores = [evaluate(prompt, ex)[0] for ex in dataset]
    return sum(scores) / len(scores) if scores else 0.0


# ---------------------------------------------------------------------------
# Run one condition
# ---------------------------------------------------------------------------

def run_condition(memory_on: bool, run_dir: str) -> None:
    label = "MEMORY ON" if memory_on else "MEMORY OFF"
    print(f"\n{'='*60}")
    print(f"  Running: {label}  →  {run_dir}")
    print(f"{'='*60}")

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=run_dir,
            max_candidate_proposals=MAX_PROPOSALS,
            seed=SEED,
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=REFLECTION_MODEL,
            reflection_minibatch_size=2,
            use_reflection_memory=memory_on,
            reflection_memory_max_entries=10,
        ),
        tracking=TrackingConfig(research_mode=True),
    )

    optimize_anything(
        seed_candidate=INITIAL_PROMPT,
        evaluator=evaluate,
        dataset=TRAINSET,
        valset=VALSET,
        config=config,
        objective="Output exactly one word (noun, verb, adjective, or adverb) for each word given.",
    )


# ---------------------------------------------------------------------------
# Analysis helpers
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


def print_memory_events(run_dir: Path) -> None:
    events_path = run_dir / "memory" / "memory_events.jsonl"
    events = load_jsonl(events_path)
    if not events:
        print("  [!] No memory events found — memory was not recording")
        return

    print(f"  Memory events recorded: {len(events)}")
    for ev in events:
        ev_type = ev.get("event", "unknown")
        if ev_type == "add":
            size_after = ev.get("size_after", "?")
            evicted = ev.get("evicted", False)
            score_delta = ev.get("score_delta", 0.0)
            print(
                f"    [iter {ev.get('iteration', '?')}] ADD  {ev.get('component', '?')}  "
                f"delta={score_delta:+.2f}  "
                f"{'ACCEPTED' if ev.get('accepted') else 'REJECTED'}  "
                f"memory_size={size_after}{'  (evicted oldest)' if evicted else ''}"
            )
        elif ev_type == "query":
            n_returned = ev.get("entries_returned", 0)
            injected = n_returned > 0
            print(
                f"    [iter {ev.get('iteration', '?')}] QUERY {ev.get('component', '?')}  "
                f"entries_returned={n_returned}  "
                f"{'→ INJECTED into prompt' if injected else '→ empty (not injected)'}"
            )


def print_injection_evidence(run_dir: Path) -> None:
    """Show whether memory text appears in a later-iteration LLM call."""
    llm_dir = run_dir / "llm_calls"
    if not llm_dir.exists():
        print("  [!] No llm_calls/ directory found")
        return

    prompt_files = sorted(llm_dir.glob("iter_*_prompt.txt"))
    injected_count = 0
    for pf in prompt_files:
        text = pf.read_text()
        if "## Optimization History" in text:
            injected_count += 1
            print(f"  Memory text found in: {pf.name}")
            # Show a snippet of the memory block
            idx = text.index("## Optimization History")
            snippet = text[idx : idx + 400].replace("\n", "\n    ")
            print(f"    ---\n    {snippet}\n    ---")
            if injected_count >= 2:
                break

    if injected_count == 0:
        print("  [!] '## Optimization History' not found in any LLM prompt — memory was NOT injected")
    else:
        print(f"  Memory injected in {injected_count} of {len(prompt_files)} LLM call(s)")


def print_summary(run_dir: Path) -> dict:
    summary_path = run_dir / "summary.jsonl"
    rows = load_jsonl(summary_path)
    if not rows:
        print("  [!] summary.jsonl not found")
        return {}

    accepted = sum(1 for r in rows if r.get("accepted", False))
    total = len(rows)
    scores: list[float] = [float(r["best_score"]) for r in rows if r.get("best_score") is not None]
    best = max(scores) if scores else None

    print(f"  Iterations    : {total}")
    print(f"  Accepted      : {accepted}/{total}  ({accepted/total:.0%})")
    print(f"  Best val score: {best}")
    return {"accepted": accepted, "total": total, "best_val": best}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    base_dir = "outputs/memory_check"
    run_off = f"{base_dir}/memory_off"
    run_on = f"{base_dir}/memory_on"

    print("=== Reflection Memory v1 Verification ===")
    print(f"  task LLM      : {TASK_MODEL}")
    print(f"  reflection LLM: {REFLECTION_MODEL}")
    print(f"  proposals     : {MAX_PROPOSALS}")
    print(f"  train/val/test: {len(TRAINSET)}/{len(VALSET)}/{len(TESTSET)}")
    print(f"  initial prompt: '{INITIAL_PROMPT}'")

    # Baseline on test with initial prompt
    print("\n--- Baseline test score (initial prompt) ---")
    baseline = evaluate_dataset(INITIAL_PROMPT, TESTSET)
    print(f"  {baseline:.2%}")

    # Run both conditions
    run_condition(memory_on=False, run_dir=run_off)
    run_condition(memory_on=True, run_dir=run_on)

    # -----------------------------------------------------------------------
    # Analysis
    # -----------------------------------------------------------------------
    print("\n\n" + "="*60)
    print("  ANALYSIS")
    print("="*60)

    off_dir = Path(run_off)
    on_dir = Path(run_on)

    print("\n[MEMORY OFF] Summary")
    off_stats = print_summary(off_dir)

    print("\n[MEMORY ON] Summary")
    on_stats = print_summary(on_dir)

    print("\n[MEMORY ON] Memory event log:")
    print_memory_events(on_dir)

    print("\n[MEMORY ON] Injection evidence in LLM prompts:")
    print_injection_evidence(on_dir)

    # Test set evaluation of best candidate from each run
    print("\n--- Test set scores ---")

    def best_candidate_from_summary(run_path: Path) -> str:
        # Read best candidate from candidates/ dir (highest numbered = best accepted)
        cands_dir = run_path / "candidates"
        if not cands_dir.exists():
            return INITIAL_PROMPT
        files = sorted(cands_dir.glob("candidate_*.json"))
        if not files:
            return INITIAL_PROMPT
        # Find the best by val score from summary
        rows = load_jsonl(run_path / "summary.jsonl")
        accepted_rows = [r for r in rows if r.get("accepted", False)]
        if not accepted_rows:
            return INITIAL_PROMPT
        best_row = max(accepted_rows, key=lambda r: r.get("best_score") or 0.0)
        cand_idx = best_row.get("best_candidate_idx")
        if cand_idx is None:
            return INITIAL_PROMPT
        cand_file = cands_dir / f"candidate_{cand_idx:03d}.json"
        if not cand_file.exists():
            return INITIAL_PROMPT
        data = json.loads(cand_file.read_text())
        texts = data.get("candidate", {})
        if isinstance(texts, dict):
            return next(iter(texts.values()), INITIAL_PROMPT)
        return str(texts)

    best_off = best_candidate_from_summary(off_dir)
    best_on = best_candidate_from_summary(on_dir)

    score_off = evaluate_dataset(best_off, TESTSET)
    score_on = evaluate_dataset(best_on, TESTSET)

    print(f"  Baseline             : {baseline:.2%}")
    print(f"  Memory OFF best      : {score_off:.2%}   prompt='{best_off[:80]}'")
    print(f"  Memory ON  best      : {score_on:.2%}   prompt='{best_on[:80]}'")

    # -----------------------------------------------------------------------
    # Verdict
    # -----------------------------------------------------------------------
    print("\n--- Verification checklist ---")
    on_events = load_jsonl(on_dir / "memory" / "memory_events.jsonl")
    add_events = [e for e in on_events if e.get("event") == "add"]
    query_events = [e for e in on_events if e.get("event") == "query"]
    injected_queries = [e for e in query_events if (e.get("entries_returned") or 0) > 0]

    llm_dir = on_dir / "llm_calls"
    prompt_files = sorted(llm_dir.glob("iter_*_prompt.txt")) if llm_dir.exists() else []
    prompts_with_memory = [p for p in prompt_files if "## Optimization History" in p.read_text()]

    print(f"  [{'✓' if add_events else '✗'}] Memory entries recorded   : {len(add_events)} entries added")
    print(f"  [{'✓' if injected_queries else '✗'}] Memory queried non-empty  : {len(injected_queries)} queries returned entries")
    print(f"  [{'✓' if prompts_with_memory else '✗'}] Memory injected in prompts: found in {len(prompts_with_memory)} prompt file(s)")
    off_ok = off_stats.get("total", 0) >= MAX_PROPOSALS
    on_ok = on_stats.get("total", 0) >= MAX_PROPOSALS
    print(f"  [{'✓' if off_ok else '✗'}] Memory OFF ran to completion: {off_stats.get('total', 0)}/{MAX_PROPOSALS} iterations")
    print(f"  [{'✓' if on_ok else '✗'}] Memory ON  ran to completion: {on_stats.get('total', 0)}/{MAX_PROPOSALS} iterations")

    all_pass = all([add_events, injected_queries, prompts_with_memory, off_ok, on_ok])
    print(f"\n{'✓ All checks passed — Reflection Memory v1 is working correctly.' if all_pass else '✗ Some checks failed — see details above.'}")


if __name__ == "__main__":
    main()
