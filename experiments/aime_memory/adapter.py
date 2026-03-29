"""GEPAAdapter for the AIME experiment — wraps the dspy-based solver."""

import os
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, TypedDict

import dspy

from experiments.aime_memory.solver import math_metric, run_llm
from gepa.core.adapter import EvaluationBatch, GEPAAdapter


class AIMETrajectory(TypedDict):
    problem: str
    correct_answer: str
    predicted_answer: str
    reasoning: str
    score: float
    feedback: str


class AIMERolloutOutput(TypedDict):
    predicted_answer: str
    correct_answer: str
    reasoning: str


class AIMEAdapter(GEPAAdapter[dspy.Example, AIMETrajectory, AIMERolloutOutput]):
    """Adapter that evaluates AIME math problems using a dspy solver."""

    COMPONENT_NAME = "system_prompt"

    def __init__(
        self,
        solver_lm: dspy.LM,
        max_workers: int = 32,
        on_solver_max_tokens_exceeded: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.solver_lm = solver_lm
        self.max_workers = max_workers
        self.on_solver_max_tokens_exceeded = on_solver_max_tokens_exceeded
        self.propose_new_texts = None
        self._realtime = os.environ.get("GEPA_AIME_ADAPTER_VERBOSE", "1") == "1"

    def _rt(self, message: str) -> None:
        if self._realtime:
            print(message, flush=True)

    def _rt_row(
        self,
        *,
        phase: str,
        row_label: str,
        total: int,
        score: float,
        output: AIMERolloutOutput,
    ) -> None:
        status = "Correct" if float(score) >= 1.0 else "Wrong"
        predicted = output.get("predicted_answer", "N/A")
        correct = output.get("correct_answer", "N/A")
        self._rt(
            f"[LIVE {phase}] {row_label}/{total} | predicted_answer={predicted} | "
            f"correct_answer={correct} | {status}"
        )

    @staticmethod
    def _valset_number(example: dspy.Example, fallback_idx: int) -> int:
        for key in ("val_id", "id", "idx"):
            value = None
            if hasattr(example, key):
                value = getattr(example, key)
            elif hasattr(example, "get"):
                try:
                    value = example.get(key)  # type: ignore[attr-defined]
                except Exception:
                    value = None
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return fallback_idx

    def _eval_single(
        self, example: dspy.Example, candidate: dict[str, str]
    ) -> tuple[float, AIMERolloutOutput, AIMETrajectory, str]:
        prompt = candidate[self.COMPONENT_NAME]
        try:
            with dspy.context(lm=self.solver_lm):
                prediction = run_llm(example, prompt)
        except Exception as exc:
            self._capture_solver_failure_from_history(exc)
            self._rt(f"[AIMEAdapter] solver failure: {type(exc).__name__}")

            correct_answer = str(example.answer)
            feedback = f"Solver failure ({type(exc).__name__}: {exc}). Correct answer: {correct_answer}"
            score = 0.0

            output: AIMERolloutOutput = {
                "predicted_answer": "<PARSE_ERROR>",
                "correct_answer": correct_answer,
                "reasoning": "",
            }
            trajectory: AIMETrajectory = {
                "problem": example.problem,
                "correct_answer": correct_answer,
                "predicted_answer": "<PARSE_ERROR>",
                "reasoning": "",
                "score": score,
                "feedback": feedback,
            }
            return score, output, trajectory, feedback

        self._capture_solver_truncation_from_history()
        score, feedback = math_metric(example, prediction)

        output: AIMERolloutOutput = {
            "predicted_answer": str(prediction.answer),
            "correct_answer": str(example.answer),
            "reasoning": getattr(prediction, "reasoning", ""),
        }
        trajectory: AIMETrajectory = {
            "problem": example.problem,
            "correct_answer": str(example.answer),
            "predicted_answer": str(prediction.answer),
            "reasoning": getattr(prediction, "reasoning", ""),
            "score": score,
            "feedback": feedback,
        }
        return score, output, trajectory, feedback

    def _capture_solver_failure_from_history(self, exception: Exception) -> None:
        if self.on_solver_max_tokens_exceeded is None:
            return

        try:
            history = getattr(self.solver_lm, "history", None)
            latest = history[-1] if history else None
            response = latest.get("response") if isinstance(latest, dict) else None

            finish_reason = None
            if response is not None and hasattr(response, "choices") and response.choices:
                finish_reason = getattr(response.choices[0], "finish_reason", None)

            payload: dict[str, Any] = {
                "event_type": "solver_parse_or_generation_failure",
                "exception_type": type(exception).__name__,
                "exception_message": str(exception),
                "finish_reason": finish_reason,
            }

            if isinstance(latest, dict):
                payload.update(
                    {
                        "model": latest.get("model"),
                        "messages": latest.get("messages"),
                        "prompt": latest.get("prompt"),
                        "outputs": latest.get("outputs"),
                        "usage": latest.get("usage"),
                        "timestamp": latest.get("timestamp"),
                    }
                )

            self.on_solver_max_tokens_exceeded(payload)
        except Exception:
            return

    def _capture_solver_truncation_from_history(self) -> None:
        if self.on_solver_max_tokens_exceeded is None:
            return

        try:
            history = getattr(self.solver_lm, "history", None)
            if not history:
                return
            latest = history[-1]
            response = latest.get("response") if isinstance(latest, dict) else None

            finish_reason = None
            if response is not None and hasattr(response, "choices") and response.choices:
                finish_reason = getattr(response.choices[0], "finish_reason", None)

            if str(finish_reason).lower() != "length":
                return

            self.on_solver_max_tokens_exceeded(
                {
                    "model": latest.get("model") if isinstance(latest, dict) else None,
                    "finish_reason": finish_reason,
                    "messages": latest.get("messages") if isinstance(latest, dict) else None,
                    "prompt": latest.get("prompt") if isinstance(latest, dict) else None,
                    "outputs": latest.get("outputs") if isinstance(latest, dict) else None,
                    "usage": latest.get("usage") if isinstance(latest, dict) else None,
                    "timestamp": latest.get("timestamp") if isinstance(latest, dict) else None,
                }
            )
        except Exception:
            # Logging should never break evaluation.
            return

    def evaluate(
        self,
        batch: list[dspy.Example],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> EvaluationBatch[AIMETrajectory, AIMERolloutOutput]:
        t0 = time.perf_counter()
        self._rt(
            f"[AIMEAdapter] evaluate start batch={len(batch)} traces={capture_traces} workers={self.max_workers}"
        )

        if len(batch) > 1:
            results: list[tuple[int, tuple[float, AIMERolloutOutput, AIMETrajectory, str]]] = []
            val_numbers = [self._valset_number(ex, idx) for idx, ex in enumerate(batch)]
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_idx = {
                    executor.submit(self._eval_single, ex, candidate): idx for idx, ex in enumerate(batch)
                }
                completed = 0
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    result = future.result()
                    results.append((idx, result))
                    completed += 1
                    self._rt(f"[AIMEAdapter] evaluate progress {completed}/{len(batch)}")
            results.sort(key=lambda x: x[0])
            phase = "MINIBATCH" if capture_traces else "VAL/BATCH"
            for idx, result in results:
                row_label = f"Q{idx + 1}" if capture_traces else f"VAL#{val_numbers[idx]}"
                self._rt_row(
                    phase=phase,
                    row_label=row_label,
                    total=len(batch),
                    score=float(result[0]),
                    output=result[1],
                )
            ordered = [r for _, r in results]
        else:
            ordered = []
            for idx, ex in enumerate(batch):
                self._rt(f"[AIMEAdapter] evaluate progress start ex={idx}/{len(batch)}")
                result = self._eval_single(ex, candidate)
                ordered.append(result)
                phase = "MINIBATCH" if capture_traces else "VAL/BATCH"
                row_label = f"Q{idx + 1}" if capture_traces else f"VAL#{self._valset_number(ex, idx)}"
                self._rt_row(
                    phase=phase,
                    row_label=row_label,
                    total=len(batch),
                    score=float(result[0]),
                    output=result[1],
                )
                self._rt(f"[AIMEAdapter] evaluate progress done ex={idx}")

        scores = [r[0] for r in ordered]
        outputs = [r[1] for r in ordered]
        trajectories = [r[2] for r in ordered] if capture_traces else None

        elapsed = time.perf_counter() - t0
        avg = (sum(scores) / len(scores)) if scores else 0.0
        self._rt(f"[AIMEAdapter] evaluate done n={len(scores)} avg={avg:.4f} elapsed={elapsed:.2f}s")

        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[AIMETrajectory, AIMERolloutOutput],
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        assert eval_batch.trajectories is not None
        ret: dict[str, list[dict[str, Any]]] = {}
        for component_name in components_to_update:
            records: list[dict[str, Any]] = []
            for traj in eval_batch.trajectories:
                records.append({
                    "Inputs": f"Problem: {traj['problem']}",
                    "Generated Outputs": f"Answer: {traj['predicted_answer']}\nReasoning: {traj['reasoning']}",
                    "Feedback": traj["feedback"],
                })
            ret[component_name] = records
        return ret
