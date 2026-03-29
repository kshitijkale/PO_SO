"""AIME experiment.

Usage
-----
Run baseline (no memory):
    uv run python -m experiments.aime_memory.run --seed 0

Run with rejection ledger:
    uv run python -m experiments.aime_memory.run --seed 0 --memory-version ledger

Flags
-----
--seed INT                  Random seed (default: 0). Use 0-4 for the 5-seed study.
--max-calls INT             Evaluation budget (default: 500).
--reflection-lm STR         LiteLLM model string for reflection (default: openai/gpt-4.1-mini).
--solver-lm STR             LiteLLM model string for solving (default: openai/gpt-4.1-mini).
--output-dir STR            Base output dir (default: outputs/aime_memory).
--workers INT               Parallel workers (default: 32).
--memory-version STR        Memory variant: ledger or None (default: None).
--train-size INT            Number of training examples (default: 45).
--val-size INT              Number of validation examples (default: 45).
--solver-temperature FLOAT  Temperature for the solver LLM (default: 0.0).
--reflection-temperature FLOAT  Temperature for the reflection LLM (default: 0.7).
--minibatch-size INT        Reflection minibatch size (default: 3).
"""

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)


INITIAL_PROMPT = (
    "Solve the problem and provide the answer provide the final answer as a single integer."
)

DEFAULT_MAX_TOKENS = 32_768

REFLECTION_PROMPT_TEMPLATE = """I provided an assistant with the following instructions to perform a task for me:
'''
<curr_param>
'''
The following are examples of different task inputs provided to the assistant
along with the assistant's response for each of them, and some feedback on how
the assistant's response could be better:
'''
<side_info>
'''
Your task is to write a new instruction for the assistant. Read the inputs carefully and identify the input format and infer detailed task
description about the task I wish to solve with the assistant.
Read all the assistant responses and the corresponding feedback. Identify all
niche and domain specific factual information about the task and include it in
the instruction, as a lot of it may not be available to the assistant in the
future. The assistant may have utilized a generalizable strategy to solve the
task, if so, include that in the instruction as well.
Below you may see an OPTIMIZATION CONTEXT block. If present it contains:
- Progress stats: how many iterations have run and how many were accepted.
- Diffs of accepted edits: unified diffs showing exactly what changed in
  instructions that improved scores. Use these to understand what directions
  have worked so far and build on them.
- Strategy patterns: a brief summary of what types of changes have succeeded
  or failed across the run.
Use this context to make a more informed decision about what to try next.
Provide the new instructions within ''' blocks."""


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
                inp = inputs_by_val_id.get(val_id)
                # dspy.Example is not JSON-serializable; convert to plain dict
                if hasattr(inp, "toDict"):
                    inp = inp.toDict()
                elif hasattr(inp, "__dict__"):
                    inp = vars(inp)
                row = {
                    "order": order,
                    "iteration": event.get("iteration"),
                    "candidate_idx": event.get("candidate_idx"),
                    "val_id": val_id,
                    "score": scores_by_val_id.get(val_id),
                    "input": inp,
                    "output": outputs_by_val_id.get(val_id),
                }
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        self._written = True


def make_reflection_lm(
    model_name: str,
    temperature: float,
    max_tokens: int,
    overflow_logger: MaxTokensExceededLogger,
):
    """Return a LanguageModel callable for the reflection LLM with explicit temperature."""

    def _lm(prompt: str | list[dict[str, Any]]) -> str:
        if isinstance(prompt, str):
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        else:
            messages = prompt
        completion = litellm.completion(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = completion.choices[0]  # type: ignore[index]
        content = choice.message.content or ""  # type: ignore[union-attr]
        finish_reason = getattr(choice, "finish_reason", None)
        if str(finish_reason).lower() == "length":
            overflow_logger.log_event(
                source="reflection_lm",
                payload={
                    "model": model_name,
                    "max_tokens": max_tokens,
                    "finish_reason": finish_reason,
                    "response": content,
                },
            )
        return content

    return _lm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AIME reflection-memory experiment")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=500)
    parser.add_argument("--reflection-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--solver-lm", type=str, default="openai/gpt-4.1-mini")
    parser.add_argument("--output-dir", type=str, default="outputs/aime_memory")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--memory-version", choices=["ledger", "diary", "diary_full"], default=None)
    parser.add_argument("--train-size", type=int, default=45)
    parser.add_argument("--val-size", type=int, default=45)
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

    mem_label = args.memory_version if args.memory_version else "no_memory"
    condition = mem_label
    run_name = f"seed{args.seed}_{condition}"
    run_dir = os.path.join(args.output_dir, run_name)
    if os.path.exists(run_dir) and not args.resume:
        run_dir = f"{run_dir}_{int(time.time())}"

    print("=== AIME Memory Experiment ===")
    print(f"  condition            : {condition}")
    print(f"  seed                 : {args.seed}")
    print(f"  max_calls            : {args.max_calls}")
    print(f"  train / val          : {args.train_size} / {args.val_size}")
    print(f"  solver temperature   : {args.solver_temperature}")
    print(f"  reflection temperature: {args.reflection_temperature}")
    print(f"  solver max_tokens    : {args.solver_max_tokens}")
    print(f"  reflection max_tokens: {args.reflection_max_tokens}")
    print(f"  minibatch_size       : {args.minibatch_size}")
    print(f"  refinement_steps     : {args.refinement_steps}")
    print(f"  run_dir              : {run_dir}")
    if args.resume:
        print("  mode                 : resume if run_dir exists")
    else:
        print("  mode                 : fresh run (auto-suffix if run_dir exists)")
    print()

    os.makedirs(run_dir, exist_ok=True)
    overflow_log_path = os.path.join(run_dir, "max_tokens_exceeded.jsonl")
    overflow_logger = MaxTokensExceededLogger(overflow_log_path)
    first_valset_log_path = os.path.join(run_dir, "first_valset_eval_outcomes.jsonl")
    first_valset_logger = FirstValsetEvalLogger(first_valset_log_path)
    dspy_warning_handler = DSPYMaxTokensWarningHandler(overflow_logger)
    dspy_logger = logging.getLogger("dspy.clients.lm")
    dspy_logger.addHandler(dspy_warning_handler)

    # --- Dataset ---
    print("Loading dataset...")
    from gepa.core.data_loader import ListDataLoader

    trainset_full, valset_full, testset = load_aime_dataset()
    trainset = trainset_full[:args.train_size]
    valset = ListDataLoader(valset_full[:args.val_size], offset=len(trainset_full))
    print(f"Dataset: train={len(trainset)}, val={len(valset)}, test={len(testset)}")

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
    reflection_lm_callable = make_reflection_lm(
        args.reflection_lm,
        args.reflection_temperature,
        args.reflection_max_tokens,
        overflow_logger,
    )

    # --- Adapter ---
    adapter = AIMEAdapter(
        solver_lm=solver_lm,
        max_workers=args.workers,
        on_solver_max_tokens_exceeded=lambda payload: overflow_logger.log_event(
            source="solver_lm_history",
            payload=payload,
        ),
    )

    # --- Optimize ---
    seed_candidate = {AIMEAdapter.COMPONENT_NAME: INITIAL_PROMPT}

    try:
        result = optimize(
            seed_candidate=seed_candidate,
            trainset=trainset,
            valset=valset,
            adapter=adapter,
            reflection_lm=reflection_lm_callable,
            reflection_prompt_template=REFLECTION_PROMPT_TEMPLATE,
            max_metric_calls=args.max_calls,
            reflection_minibatch_size=args.minibatch_size,
            run_dir=run_dir,
            cache_evaluation=True,
            seed=args.seed,
            research_mode=True,
            display_progress_bar=False,
            memory_version=args.memory_version,
            refinement_steps=args.refinement_steps,
            callbacks=[first_valset_logger],
        )
    finally:
        dspy_logger.removeHandler(dspy_warning_handler)

    if args.skip_test_eval:
        print("\nSkipping baseline/optimized test evaluation (--skip-test-eval).")
        return

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
