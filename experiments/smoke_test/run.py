"""Smoke-test experiment: verify GEPA end-to-end with gpt-4.1-mini.

Usage
-----
    uv run python -m experiments.smoke_test.run

What it does
------------
Optimizes a system prompt for simple arithmetic word problems.
- 3 train / 3 val / 3 test examples (hardcoded, no external datasets)
- Both task LLM and reflector LLM: openai/gpt-4.1-mini
- Budget: 3 candidate proposals (fast smoke test)
"""

import re
from pathlib import Path

import litellm
from dotenv import load_dotenv

from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    optimize_anything,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env")

TASK_MODEL = "openai/gpt-4.1-mini"
REFLECTION_MODEL = "openai/gpt-4.1-mini"

INITIAL_PROMPT = "Solve the math problem. Provide only the final numerical answer."

# ---------------------------------------------------------------------------
# Tiny hardcoded dataset — (problem, answer) pairs
# ---------------------------------------------------------------------------
_RAW = [
    # train
    ("If Alice has 5 apples and gives 2 to Bob, how many does she have left?", "3"),
    ("A shop sells pencils for $0.50 each. How much do 8 pencils cost in dollars?", "4"),
    ("A train travels 60 miles per hour for 3 hours. How many miles does it travel?", "180"),
    # val
    ("There are 24 students in a class. If the teacher splits them into groups of 4, how many groups are there?", "6"),
    ("Sam reads 15 pages a day. How many pages does he read in 7 days?", "105"),
    ("A rectangle is 9 cm wide and 5 cm tall. What is its area in square centimeters?", "45"),
    # test
    ("A baker makes 12 muffins per batch. If she bakes 4 batches, how many muffins does she make?", "48"),
    ("A car uses 1 gallon of gas every 30 miles. How many gallons does it need for 150 miles?", "5"),
    ("If a ticket costs $12 and you buy 7 tickets, what is the total cost in dollars?", "84"),
]


def _make_examples(raw):
    return [{"problem": p, "answer": a} for p, a in raw]


TRAINSET = _make_examples(_RAW[:3])
VALSET = _make_examples(_RAW[3:6])
TESTSET = _make_examples(_RAW[6:])


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _call_llm(system_prompt: str, problem: str) -> str:
    """Call gpt-4.1-mini with the given system prompt and problem."""
    response = litellm.completion(
        model=TASK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": problem},
        ],
        max_tokens=256,
        temperature=0.0,
    )
    resp: Any = response  # type: ignore[assignment]
    return resp.choices[0].message.content.strip()


def _extract_number(text: str) -> str | None:
    """Extract the last number (int or float) from text."""
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    return nums[-1] if nums else None


def evaluate(candidate: str, example: dict) -> tuple[float, SideInfo]:
    """Score candidate prompt on one arithmetic example."""
    output = _call_llm(candidate, example["problem"])
    predicted = _extract_number(output)
    expected = example["answer"]

    # Normalize: compare as floats when possible
    try:
        score = float(float(predicted) == float(expected)) if predicted is not None else 0.0
    except ValueError:
        score = 0.0

    return score, {
        "problem": example["problem"],
        "expected": expected,
        "raw_output": output,
        "predicted": predicted,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Baseline evaluation helper
# ---------------------------------------------------------------------------

def evaluate_dataset(prompt: str, dataset: list[dict]) -> float:
    scores = []
    for ex in dataset:
        s, _ = evaluate(prompt, ex)
        scores.append(s)
    return sum(scores) / len(scores) if scores else 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_dir = "outputs/smoke_test"

    print("=== GEPA Smoke Test ===")
    print(f"  task LLM      : {TASK_MODEL}")
    print(f"  reflection LLM: {REFLECTION_MODEL}")
    print(f"  train/val/test : {len(TRAINSET)}/{len(VALSET)}/{len(TESTSET)}")
    print(f"  run_dir       : {run_dir}")
    print()

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=run_dir,
            max_candidate_proposals=3,
            seed=0,
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=REFLECTION_MODEL,
            reflection_minibatch_size=2,
        ),
    )

    result = optimize_anything(
        seed_candidate=INITIAL_PROMPT,
        evaluator=evaluate,
        dataset=TRAINSET,
        valset=VALSET,
        config=config,
        objective="Maximize accuracy on simple arithmetic word problems.",
    )

    # --- Results ---
    best_candidate = result.best_candidate
    # best_candidate is str for single-component optimization
    best_prompt = best_candidate if isinstance(best_candidate, str) else list(best_candidate.values())[0]
    print("\n=== Results ===")
    print(f"Best prompt:\n{best_prompt}\n")

    baseline_score = evaluate_dataset(INITIAL_PROMPT, TESTSET)
    optimized_score = evaluate_dataset(best_prompt, TESTSET)

    print(f"Baseline  (test): {baseline_score:.2%}")
    print(f"Optimized (test): {optimized_score:.2%}")
    print(f"Delta           : {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
