"""Evaluate one or more prompts on the 30 AIME 2025 test examples (no repetition).

Usage
-----
    # Single prompt
    uv run python -m experiments.aime_memory.test_prompt \
        --prompt "Solve the problem and provide the final answer as a single integer."

    # Multiple prompts from a file (one per line, blank lines ignored)
    uv run python -m experiments.aime_memory.test_prompt \
        --prompt-file prompts.txt

    # Multiple inline prompts
    uv run python -m experiments.aime_memory.test_prompt \
        --prompt "Prompt A" --prompt "Prompt B"

Flags
-----
--prompt STR          Prompt text to evaluate (repeatable).
--prompt-file PATH    File with one prompt per line.
--solver-lm STR       LiteLLM model string (default: openai/gpt-4.1-mini).
--workers INT         Parallel workers (default: 4).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import dspy
from datasets import load_dataset
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)

from experiments.aime_memory.solver import math_metric  # noqa: E402


def load_testset() -> list:
    """Load the 30 AIME 2025 examples exactly once (no repetition)."""
    test_split = load_dataset("MathArena/aime_2025")["train"]
    return [
        dspy.Example(
            problem=x["problem"],
            answer=x["answer"],
        ).with_inputs("problem")
        for x in test_split
    ]


def evaluate_prompt(prompt: str, dataset: list, solver_lm: dspy.LM) -> float:
    """Run `prompt` on all examples in `dataset`, return accuracy."""
    from dspy import ChainOfThought

    class AIMESolverSignature(dspy.Signature):
        problem = dspy.InputField(desc="The math problem to solve.")
        answer = dspy.OutputField(desc="The final numerical answer as a single integer.")

    predictor = ChainOfThought(AIMESolverSignature)
    predictor.predict.signature.instructions = prompt

    dspy.configure(lm=solver_lm)

    total = len(dataset)
    num_correct = 0
    num_attempted = 0
    for idx, example in enumerate(dataset, start=1):
        try:
            prediction = predictor(problem=example.problem)
            score, _ = math_metric(example, prediction)
            num_correct += int(score)
            num_attempted += 1
            mark = "+" if score == 1.0 else "-"
            sys.stdout.write(f"  [{mark}] #{idx:02d}/{total}  pred={prediction.answer}  gt={example.answer}  running={num_correct}/{num_attempted}\n")
        except Exception as e:
            sys.stdout.write(f"  [!] #{idx:02d}/{total}  ERROR: {e}\n")
        sys.stdout.flush()

    accuracy = num_correct / num_attempted if num_attempted else 0.0
    print(f"  Result: {num_correct}/{num_attempted} correct ({accuracy:.2%})"
          + (f"  [{total - num_attempted} skipped due to errors]" if num_attempted < total else ""))
    return accuracy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate prompts on AIME 2025 (30 examples, no repetition)")
    parser.add_argument("--prompt", action="append", default=[], metavar="STR", help="Prompt text (repeatable)")
    parser.add_argument("--prompt-file", type=str, default=None, help="File with one prompt per line")
    parser.add_argument("--solver-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--max-tokens", type=int, default=32000)
    return parser.parse_args()


def main():
    args = parse_args()

    prompts: list[str] = list(args.prompt)
    if args.prompt_file:
        lines = Path(args.prompt_file).read_text().splitlines()
        prompts += [l.strip() for l in lines if l.strip()]

    if not prompts:
        print("No prompts provided. Use --prompt or --prompt-file.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in the environment or .claude/.env.")

    print("Loading AIME 2025 test set...")
    testset = load_testset()
    print(f"  {len(testset)} examples loaded.\n")

    solver_lm = dspy.LM(args.solver_lm, api_key=api_key, temperature=1.0, max_tokens=args.max_tokens)

    results: list[tuple[str, float]] = []

    for i, prompt in enumerate(prompts, 1):
        print(f"--- Prompt {i}/{len(prompts)} ---")
        print(f"  {prompt!r}")
        acc = evaluate_prompt(prompt, testset, solver_lm)
        results.append((prompt, acc))
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for i, (prompt, acc) in enumerate(results, 1):
        short = prompt[:80].replace("\n", " ")
        print(f"  [{i}] {acc:.2%}  {short!r}")


if __name__ == "__main__":
    main()
