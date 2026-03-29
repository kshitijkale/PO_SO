"""AIME experiment — 45 train / 45 val, verbose output.

Usage
-----
    uv run python -m experiments.aime_memory.run_verbose

Flags
-----
--seed INT              Random seed (default: 0).
--max-calls INT         Evaluation budget (default: 500).
--reflection-lm STR     LiteLLM model string for reflection (default: openai/gpt-4.1-mini).
--solver-lm STR         LiteLLM model string for solving (default: openai/gpt-4.1-mini).
--output-dir STR        Run directory (default: outputs/aime_verbose).
--workers INT           Parallel workers (default: 1).
--memory-version STR    Memory variant: ledger or None (default: None).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import dspy
import litellm
from dotenv import load_dotenv

from experiments.aime_memory.adapter import AIMEAdapter
from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import evaluate_on_dataset
from gepa.api import optimize
from gepa.core.callbacks import ValsetEvaluatedEvent
from gepa.core.data_loader import ListDataLoader

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)

TRAIN_SIZE = 45
VAL_SIZE = 45

INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)

DEFAULT_MAX_TOKENS = 32_768


class MaxTokensExceededLogger:
    """Append max-token truncation events to a dedicated JSONL file."""

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self._lock = Lock()

    def log_event(self, *, source: str, payload: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            **payload,
        }
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


class DSPYMaxTokensWarningHandler(logging.Handler):
    """Mirror DSPy truncation warnings into the dedicated overflow log file."""

    def __init__(self, overflow_logger: MaxTokensExceededLogger) -> None:
        super().__init__()
        self.overflow_logger = overflow_logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            lower = message.lower()
            if "truncated" in lower and "max_tokens" in lower:
                self.overflow_logger.log_event(
                    source="solver_dspy_warning",
                    payload={
                        "logger": record.name,
                        "level": record.levelname,
                        "message": message,
                    },
                )
        except Exception:
            pass


class FirstValsetEvalLogger:
    """Write the first valset evaluation outcomes to disk in stable order."""

    def __init__(self, output_path: str) -> None:
        self.output_path = output_path
        self._written = False

    @staticmethod
    def _val_id_sort_key(val_id: Any) -> tuple[int, str]:
        try:
            return (0, f"{int(val_id):020d}")
        except (TypeError, ValueError):
            return (1, str(val_id))

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        if self._written:
            return

        scores_by_val_id = event.get("scores_by_val_id", {})
        inputs_by_val_id = event.get("inputs_by_val_id") or {}
        outputs_by_val_id = event.get("outputs_by_val_id") or {}
        ordered_ids = sorted(scores_by_val_id.keys(), key=self._val_id_sort_key)

        with open(self.output_path, "w", encoding="utf-8") as f:
            for order, val_id in enumerate(ordered_ids, start=1):
                row = {
                    "order": order,
                    "iteration": event.get("iteration"),
                    "candidate_idx": event.get("candidate_idx"),
                    "val_id": val_id,
                    "score": scores_by_val_id.get(val_id),
                    "input": inputs_by_val_id.get(val_id),
                    "output": outputs_by_val_id.get(val_id),
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        self._written = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME 45/45 verbose experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--solver-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--output-dir", type=str, default="outputs/aime_verbose")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--memory-version", choices=["ledger"], default=None)
    parser.add_argument("--solver-temperature", type=float, default=0.0)
    parser.add_argument("--reflection-temperature", type=float, default=0.7)
    parser.add_argument("--solver-max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--reflection-max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--minibatch-size", type=int, default=3)
    parser.add_argument(
        "--skip-test-eval",
        action="store_true",
        default=False,
        help="Skip baseline/optimized test-set evaluation and exit after optimization.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from existing run_dir if present. By default, creates a fresh run dir.",
    )
    parser.add_argument(
        "--refinement-steps",
        type=int,
        default=1,
        help="Number of inner refinement steps per iteration (default: 1, i.e. single-shot).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    run_dir = args.output_dir
    if os.path.exists(run_dir) and not args.resume:
        run_dir = f"{run_dir}_{int(time.time())}"

    # --- Dataset: 45 train, 45 val ---
    print("Loading AIME dataset...")
    trainset_full, valset_full, testset = load_aime_dataset()
    trainset = trainset_full[:TRAIN_SIZE]
    valset = ListDataLoader(valset_full[:VAL_SIZE], offset=len(trainset_full))

    print("\n=== AIME Verbose Run ===")
    print(f"  solver LLM      : {args.solver_lm}")
    print(f"  reflection LLM  : {args.reflection_lm}")
    print(f"  train / val     : {len(trainset)} / {len(valset)}")
    print(f"  max_calls       : {args.max_calls}")
    print(f"  memory_version  : {args.memory_version or 'none'}")
    print(f"  workers         : {args.workers}")
    print(f"  seed            : {args.seed}")
    print(f"  solver temp     : {args.solver_temperature}")
    print(f"  reflection temp : {args.reflection_temperature}")
    print(f"  solver max_tokens    : {args.solver_max_tokens}")
    print(f"  reflection max_tokens: {args.reflection_max_tokens}")
    print(f"  minibatch_size  : {args.minibatch_size}")
    print(f"  refinement_steps: {args.refinement_steps}")
    print(f"  run_dir         : {run_dir}")
    if args.resume:
        print("  mode            : resume if run_dir exists")
    else:
        print("  mode            : fresh run (auto-suffix if run_dir exists)")
    print()

    os.makedirs(run_dir, exist_ok=True)
    overflow_log_path = os.path.join(run_dir, "max_tokens_exceeded.jsonl")
    overflow_logger = MaxTokensExceededLogger(overflow_log_path)
    first_valset_log_path = os.path.join(run_dir, "first_valset_eval_outcomes.jsonl")
    first_valset_logger = FirstValsetEvalLogger(first_valset_log_path)
    dspy_warning_handler = DSPYMaxTokensWarningHandler(overflow_logger)
    dspy_logger = logging.getLogger("dspy.clients.lm")
    dspy_logger.addHandler(dspy_warning_handler)

    # --- Solver LM ---
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Set it in the environment or in .claude/.env before running this script."
        )
    solver_lm = dspy.LM(
        args.solver_lm,
        api_key=api_key,
        temperature=args.solver_temperature,
        max_tokens=args.solver_max_tokens,
        cache=False,
    )

    # --- Reflection LM callable with explicit temperature ---
    reflection_lm_name = args.reflection_lm
    reflection_temperature = args.reflection_temperature

    def _reflection_lm(prompt: str | list[dict[str, Any]]) -> str:
        msgs: list[dict[str, Any]] = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        completion = litellm.completion(
            model=reflection_lm_name,
            messages=msgs,
            temperature=reflection_temperature,
            max_tokens=args.reflection_max_tokens,
        )
        choice = completion.choices[0]  # type: ignore[index]
        content = choice.message.content or ""  # type: ignore[union-attr]
        finish_reason = getattr(choice, "finish_reason", None)
        if str(finish_reason).lower() == "length":
            overflow_logger.log_event(
                source="reflection_lm",
                payload={
                    "model": reflection_lm_name,
                    "max_tokens": args.reflection_max_tokens,
                    "finish_reason": finish_reason,
                    "response": content,
                },
            )
        return content

    # --- Adapter ---
    adapter = AIMEAdapter(
        solver_lm=solver_lm,
        max_workers=args.workers,
        on_solver_max_tokens_exceeded=lambda payload: overflow_logger.log_event(
            source="solver_lm_history",
            payload=payload,
        ),
    )

    seed_candidate = {AIMEAdapter.COMPONENT_NAME: INITIAL_PROMPT}

    # --- Optimize ---
    try:
        result = optimize(
            seed_candidate=seed_candidate,
            trainset=trainset,
            valset=valset,
            adapter=adapter,
            reflection_lm=_reflection_lm,
            max_metric_calls=args.max_calls,
            reflection_minibatch_size=args.minibatch_size,
            run_dir=run_dir,
            cache_evaluation=True,
            seed=args.seed,
            research_mode=True,
            verbose=True,
            memory_version=args.memory_version,
            refinement_steps=args.refinement_steps,
            callbacks=[first_valset_logger],
        )
    finally:
        dspy_logger.removeHandler(dspy_warning_handler)

    if args.skip_test_eval:
        print("\nSkipping baseline/optimized test evaluation (--skip-test-eval).")
        return

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
