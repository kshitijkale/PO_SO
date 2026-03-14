"""AIME reflection-memory experiment.

Usage
-----
Run baseline (no memory):
    uv run python -m experiments.aime_memory.run --seed 0

Run with reflection memory:
    uv run python -m experiments.aime_memory.run --seed 0 --memory

Flags
-----
--seed INT            Random seed (default: 0). Use 0-4 for the 5-seed study.
--memory              Enable reflection memory (default: off).
--max-calls INT       Evaluation budget (default: 500).
--reflection-lm STR   LiteLLM model string for reflection (default: openai/gpt-4.1-mini).
--solver-lm STR       LiteLLM model string for solving (default: openai/gpt-4.1-mini).
--output-dir STR      Base output dir (default: outputs/aime_memory).
--workers INT         Parallel workers (default: 32).
--memory-entries INT  Max entries in reflection memory (default: 10).
"""

import argparse
import os
from pathlib import Path

import dspy
from dotenv import load_dotenv

from experiments.aime_memory.adapter import AIMEAdapter
from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import evaluate_on_dataset
from gepa.api import optimize


_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)


INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME reflection-memory experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--memory", action="store_true", default=False)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--solver-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--output-dir", type=str, default="outputs/aime_memory")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--memory-entries", type=int, default=10)
    return parser.parse_args()


def main():
    args = parse_args()

    condition = "memory_on" if args.memory else "memory_off"
    run_name = f"seed{args.seed}_{condition}"
    run_dir = os.path.join(args.output_dir, run_name)

    print(f"=== AIME Memory Experiment ===")
    print(f"  condition : {condition}")
    print(f"  seed      : {args.seed}")
    print(f"  max_calls : {args.max_calls}")
    print(f"  run_dir   : {run_dir}")
    print()

    # --- Dataset ---
    trainset, valset, testset = load_aime_dataset()
    print(f"Dataset: train={len(trainset)}, val={len(valset)}, test={len(testset)}")

    # --- Solver LM ---
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Set it in the environment or in .claude/.env before running this script."
        )
    solver_lm = dspy.LM(args.solver_lm, api_key=api_key, temperature=1.0, max_tokens=32000)

    # --- Adapter ---
    adapter = AIMEAdapter(solver_lm=solver_lm, max_workers=args.workers)

    # --- Optimize ---
    seed_candidate = {AIMEAdapter.COMPONENT_NAME: INITIAL_PROMPT}

    result = optimize(
        seed_candidate=seed_candidate,
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_lm=args.reflection_lm,
        max_metric_calls=args.max_calls,
        run_dir=run_dir,
        cache_evaluation=True,
        seed=args.seed,
        research_mode=True,
        use_reflection_memory=args.memory,
        reflection_memory_max_entries=args.memory_entries,
        objective="Optimize the system prompt to maximize correct answers on AIME math competition problems.",
    )

    # --- Evaluate baseline and best prompt on test set ---
    dspy.configure(lm=solver_lm)

    print("\n--- Baseline evaluation ---")
    baseline_score = evaluate_on_dataset(INITIAL_PROMPT, testset)

    print("\n--- Best optimized prompt ---")
    best_candidate = result.best_candidate
    best_prompt = best_candidate[AIMEAdapter.COMPONENT_NAME]
    print(best_prompt)

    print("\n--- Optimized evaluation ---")
    optimized_score = evaluate_on_dataset(best_prompt, testset)

    print(f"\nBaseline  : {baseline_score:.2%}")
    print(f"Optimized : {optimized_score:.2%}")
    print(f"Delta     : {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
