"""DSPy-based AIME solver and metric — uses `problem` as the input field."""

import dspy


class AIMESolverSignature(dspy.Signature):
    problem = dspy.InputField(desc="The math problem to solve.")
    answer = dspy.OutputField(desc="The final numerical answer as a single integer.")


predictor = dspy.ChainOfThought(AIMESolverSignature)


def run_llm(example, prompt: str):
    """Run the solver LLM with `prompt` as the system instructions."""
    predictor.predict.signature.instructions = prompt
    return predictor(problem=example.problem)


def math_metric(example, prediction) -> tuple[float, str]:
    """Return (score, feedback_text).

    score = 1.0 if predicted integer matches the correct answer, else 0.0.
    feedback_text is shown to the reflection LLM as side_info.
    """
    correct_answer = int(example.answer)
    solution = getattr(example, "solution", "")
    solution_suffix = (
        f" Here's the step-by-step solution:\n{solution}\n\n"
        "Think about what takeaways you can learn from this to improve future answers."
        if solution
        else ""
    )

    try:
        llm_answer = int(prediction.answer)
    except (ValueError, TypeError):
        return 0.0, (
            f"Your answer '{prediction.answer}' could not be parsed as an integer. "
            f"The correct answer is {correct_answer}.{solution_suffix}"
        )

    score = float(correct_answer == llm_answer)
    status = "correct" if score == 1.0 else "incorrect"
    return score, f"Your answer is {status}. The correct answer is {correct_answer}.{solution_suffix}"


def evaluate_on_dataset(prompt: str, dataset: list) -> float:
    """Evaluate `prompt` on `dataset` with dspy.Evaluate. Returns accuracy in [0, 1]."""
    predictor.predict.signature.instructions = prompt

    def dspy_metric(example, prediction):
        return math_metric(example, prediction)[0]

    evaluator = dspy.Evaluate(
        devset=dataset,
        metric=dspy_metric,
        num_threads=16,
        display_progress=True,
    )
    return evaluator(predictor).score / 100.0
