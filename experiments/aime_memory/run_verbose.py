"""AIME experiment — 45 train / 45 val, verbose output, memory ON.

Usage
-----
    uv run python -m experiments.aime_memory.run_verbose

Flags
-----
--seed INT            Random seed (default: 0).
--max-calls INT       Evaluation budget (default: 500).
--reflection-lm STR   LiteLLM model string for reflection (default: groq/openai/gpt-oss-20b).
--reflection-temperature FLOAT
                    Reflection temperature (default: provider/model default).
--solver-lm STR       LiteLLM model string for solving (default: groq/openai/gpt-oss-20b).
--solver-temperature FLOAT
                    Solver temperature (default: 0.7).
--output-dir STR      Run directory (default: outputs/aime_verbose).
--workers INT         Parallel workers (default: 1).
--memory-entries INT  Max entries in reflection memory (default: 10).
--no-memory           Disable reflection memory.
"""

from __future__ import annotations

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

TRAIN_SIZE = 45
VAL_SIZE = 45
DEFAULT_MODEL = "groq/openai/gpt-oss-20b"
GROQ_API_BASE = "https://api.groq.com/openai/v1"

INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)


def normalize_model_for_groq(model: str) -> str:
    """Normalize model aliases so Groq routing is unambiguous."""
    if model == "openai/gpt-oss-20b":
        return "groq/openai/gpt-oss-20b"
    return model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME 45/45 verbose experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--reflection-temperature", type=float, default=None)
    parser.add_argument("--solver-lm", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--solver-temperature", type=float, default=0.7)
    parser.add_argument("--output-dir", type=str, default="outputs/aime_verbose")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--memory-entries", type=int, default=10)
    parser.add_argument("--no-memory", action="store_true", default=False)
    return parser.parse_args()


def main():
    args = parse_args()
    use_memory = not args.no_memory
    run_dir = args.output_dir
    solver_model = normalize_model_for_groq(args.solver_lm)
    reflection_model = normalize_model_for_groq(args.reflection_lm)

    # --- Dataset: 45 train, 45 val ---
    print("Loading AIME dataset...")
    trainset_full, valset_full, testset = load_aime_dataset()
    trainset = trainset_full[:TRAIN_SIZE]
    valset = valset_full[:VAL_SIZE]

    print("\n=== AIME Verbose Run ===")
    print(f"  solver LLM      : {solver_model}")
    print(f"  solver temp     : {args.solver_temperature}")
    print(f"  reflection LLM  : {reflection_model}")
    print(f"  reflection temp : {args.reflection_temperature}")
    print(f"  train / val     : {len(trainset)} / {len(valset)}")
    print(f"  max_calls       : {args.max_calls}")
    print(f"  memory          : {'ON' if use_memory else 'OFF'}")
    print(f"  workers         : {args.workers}")
    print(f"  seed            : {args.seed}")
    print(f"  run_dir         : {run_dir}")
    print()

    # --- Solver LM ---
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is missing. Set it in the environment or in .claude/.env before running this script."
        )
    solver_lm = dspy.LM(
        solver_model,
        api_key=api_key,
        api_base=GROQ_API_BASE,
        temperature=args.solver_temperature,
        max_tokens=32000,
    )

    # --- Adapter ---
    adapter = AIMEAdapter(solver_lm=solver_lm, max_workers=args.workers)

    seed_candidate = {AIMEAdapter.COMPONENT_NAME: INITIAL_PROMPT}

    # --- Optimize ---
    result = optimize(
        seed_candidate=seed_candidate,
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_lm=reflection_model,
        reflection_lm_temperature=args.reflection_temperature,
        max_metric_calls=args.max_calls,
        run_dir=run_dir,
        cache_evaluation=True,
        seed=args.seed,
        research_mode=True,
        verbose=True,
        use_reflection_memory=use_memory,
        reflection_memory_max_entries=args.memory_entries,
        objective="Maximize accuracy on AIME math problems. The answer must be a single integer.",
    )

    # --- Results ---
    best_candidate = result.best_candidate
    assert isinstance(best_candidate, dict)
    best_prompt = best_candidate[AIMEAdapter.COMPONENT_NAME]
    print(f"\nBest candidate:\n{best_prompt}")

    # --- Test set evaluation ---
    dspy.configure(lm=solver_lm)

    print("\n--- Baseline test evaluation ---")
    baseline_score = evaluate_on_dataset(INITIAL_PROMPT, testset, print_examples=True)

    print("\n--- Optimized test evaluation ---")
    optimized_score = evaluate_on_dataset(best_prompt, testset, print_examples=True)

    print(f"\nBaseline  : {baseline_score:.2%}")
    print(f"Optimized : {optimized_score:.2%}")
    print(f"Delta     : {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
