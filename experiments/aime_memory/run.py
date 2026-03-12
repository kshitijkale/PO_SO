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
--reflection-lm STR   LiteLLM model string for reflection (default: openai/gpt-4.1).
--solver-lm STR       LiteLLM model string for solving (default: gpt-4.1-mini).
--output-dir STR      Base output dir (default: outputs/aime_memory).
--workers INT         Parallel workers (default: 32).
--memory-entries INT  Max entries in reflection memory (default: 10).
"""

import argparse
import os

import dspy

from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import evaluate_on_dataset, math_metric, run_llm
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    optimize_anything,
)


INITIAL_PROMPT = (
    "Solve the math problem carefully. "
    "Break down the steps and provide the final answer as a single integer."
)


def make_evaluator(solver_lm: dspy.LM):
    """Return a GEPA evaluator closure bound to the given dspy LM."""
    dspy.configure(lm=solver_lm)

    def evaluate(candidate: str, example) -> tuple[float, SideInfo]:
        prediction = run_llm(example, candidate)
        score, feedback = math_metric(example, prediction)
        return score, {
            "score": score,
            "problem": example.problem,
            "prompt": candidate,
            "output": prediction.answer,
            "reasoning": getattr(prediction, "reasoning", ""),
            "execution_feedback": feedback,
        }

    return evaluate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME reflection-memory experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--memory", action="store_true", default=False)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default="openai/gpt-4.1")
    parser.add_argument("--solver-lm", type=str, default="gpt-4.1-mini")
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
    api_key = os.environ.get("OPENAI_API_KEY")
    solver_lm = dspy.LM(args.solver_lm, api_key=api_key, temperature=1.0, max_tokens=32000)
    evaluator = make_evaluator(solver_lm)

    # --- GEPA config ---
    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=run_dir,
            max_metric_calls=args.max_calls,
            track_best_outputs=True,
            parallel=True,
            max_workers=args.workers,
            cache_evaluation=True,
            seed=args.seed,
        ),
        reflection=ReflectionConfig(
            reflection_lm=args.reflection_lm,
            use_reflection_memory=args.memory,
            reflection_memory_max_entries=args.memory_entries,
        ),
    )

    # --- Optimize ---
    result = optimize_anything(
        seed_candidate=INITIAL_PROMPT,
        evaluator=evaluator,
        dataset=trainset,
        valset=valset,
        config=config,
    )

    # --- Evaluate baseline and best prompt on test set ---
    print("\n--- Baseline evaluation ---")
    baseline_score = evaluate_on_dataset(INITIAL_PROMPT, testset)

    print("\n--- Best optimized prompt ---")
    best_prompt = result.best_candidate
    print(best_prompt)

    print("\n--- Optimized evaluation ---")
    optimized_score = evaluate_on_dataset(best_prompt, testset)

    print(f"\nBaseline  : {baseline_score:.2%}")
    print(f"Optimized : {optimized_score:.2%}")
    print(f"Delta     : {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
