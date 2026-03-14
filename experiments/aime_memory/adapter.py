"""GEPAAdapter for the AIME experiment — wraps the dspy-based solver."""

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypedDict

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
    reasoning: str


class AIMEAdapter(GEPAAdapter[dspy.Example, AIMETrajectory, AIMERolloutOutput]):
    """Adapter that evaluates AIME math problems using a dspy solver."""

    COMPONENT_NAME = "system_prompt"

    def __init__(self, solver_lm: dspy.LM, max_workers: int = 32):
        self.solver_lm = solver_lm
        self.max_workers = max_workers
        self.propose_new_texts = None

    def _eval_single(
        self, example: dspy.Example, candidate: dict[str, str]
    ) -> tuple[float, AIMERolloutOutput, AIMETrajectory, str]:
        prompt = candidate[self.COMPONENT_NAME]
        with dspy.context(lm=self.solver_lm):
            prediction = run_llm(example, prompt)
        score, feedback = math_metric(example, prediction)

        output: AIMERolloutOutput = {
            "predicted_answer": str(prediction.answer),
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

    def evaluate(
        self,
        batch: list[dspy.Example],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> EvaluationBatch[AIMETrajectory, AIMERolloutOutput]:
        if len(batch) > 1:
            results: list[tuple[int, tuple[float, AIMERolloutOutput, AIMETrajectory, str]]] = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_idx = {
                    executor.submit(self._eval_single, ex, candidate): idx for idx, ex in enumerate(batch)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    results.append((idx, future.result()))
            results.sort(key=lambda x: x[0])
            ordered = [r for _, r in results]
        else:
            ordered = [self._eval_single(ex, candidate) for ex in batch]

        scores = [r[0] for r in ordered]
        outputs = [r[1] for r in ordered]
        trajectories = [r[2] for r in ordered] if capture_traces else None

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
