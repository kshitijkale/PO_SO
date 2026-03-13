"""AIME experiment — 45 train / 45 val, verbose output, memory ON.

Usage
-----
    uv run python -m experiments.aime_memory.run_verbose

Flags
-----
--seed INT            Random seed (default: 0).
--max-calls INT       Evaluation budget (default: 500).
--reflection-lm STR   LiteLLM model string for reflection (default: openai/gpt-4.1-mini).
--solver-lm STR       LiteLLM model string for solving (default: openai/gpt-4.1-mini).
--output-dir STR      Run directory (default: outputs/aime_verbose).
--workers INT         Parallel workers (default: 1).
--memory-entries INT  Max entries in reflection memory (default: 10).
--no-memory           Disable reflection memory.
"""

from __future__ import annotations

import argparse
import os
import sys

import dspy
from dotenv import load_dotenv
from pathlib import Path

from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import evaluate_on_dataset, math_metric, run_llm
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

TRAIN_SIZE = 45
VAL_SIZE = 45

INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)


def make_evaluator(solver_lm: dspy.LM):
    import threading
    dspy.configure(lm=solver_lm)
    _lock = threading.Lock()
    _state = {"n": 0, "correct": 0}

    def evaluate(candidate: str, example) -> tuple[float, SideInfo]:
        prediction = run_llm(example, candidate)
        score, feedback = math_metric(example, prediction)
        with _lock:
            _state["n"] += 1
            _state["correct"] += int(score >= 1.0)
            n, c = _state["n"], _state["correct"]
        mark = "+" if score >= 1.0 else "-"
        sys.stdout.write(f"  [{mark}] #{n}  pred={prediction.answer}  gt={example.answer}  running={c}/{n}\n")
        sys.stdout.flush()
        return score, {
            "score": score,
            "problem": example.problem,
            "prompt": candidate,
            "output": prediction.answer,
            "reasoning": getattr(prediction, "reasoning", ""),
            "Feedback": feedback,
        }

    return evaluate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME 45/45 verbose experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--solver-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--output-dir", type=str, default="outputs/aime_verbose")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--memory-entries", type=int, default=10)
    parser.add_argument("--no-memory", action="store_true", default=False)
    return parser.parse_args()


def main():
    args = parse_args()
    use_memory = not args.no_memory
    run_dir = args.output_dir

    # --- Dataset: 45 train, 45 val ---
    print("Loading AIME dataset...")
    trainset_full, valset_full, testset = load_aime_dataset()
    trainset = trainset_full[:TRAIN_SIZE]
    valset = valset_full[:VAL_SIZE]

    print(f"\n=== AIME Verbose Run ===")
    print(f"  solver LLM      : {args.solver_lm}")
    print(f"  reflection LLM  : {args.reflection_lm}")
    print(f"  train / val     : {len(trainset)} / {len(valset)}")
    print(f"  max_calls       : {args.max_calls}")
    print(f"  memory          : {'ON' if use_memory else 'OFF'}")
    print(f"  workers         : {args.workers}")
    print(f"  seed            : {args.seed}")
    print(f"  run_dir         : {run_dir}")
    print()

    # --- Solver LM ---
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Set it in the environment or in .claude/.env before running this script."
        )
    solver_lm = dspy.LM(args.solver_lm, api_key=api_key, temperature=0.7, max_tokens=32000)
    evaluator = make_evaluator(solver_lm)

    # --- Config ---
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
            use_reflection_memory=use_memory,
            reflection_memory_max_entries=args.memory_entries,
        ),
        tracking=TrackingConfig(
            research_mode=True,
            verbose=True,
        ),
    )

    # --- Optimize ---
    result = optimize_anything(
        seed_candidate=INITIAL_PROMPT,
        evaluator=evaluator,
        dataset=trainset,
        valset=valset,
        config=config,
        objective="Maximize accuracy on AIME math problems. The answer must be a single integer.",
    )

    # --- Results ---
    print(f"\nBest candidate:\n{result.best_candidate}")

    # --- Test set evaluation ---
    print("\n--- Baseline test evaluation ---")
    baseline_score = evaluate_on_dataset(INITIAL_PROMPT, testset, print_examples=True)

    print("\n--- Optimized test evaluation ---")
    optimized_score = evaluate_on_dataset(result.best_candidate, testset, print_examples=True)

    print(f"\nBaseline  : {baseline_score:.2%}")
    print(f"Optimized : {optimized_score:.2%}")
    print(f"Delta     : {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
