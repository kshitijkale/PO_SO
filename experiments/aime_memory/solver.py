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

    try:
        llm_answer = int(prediction.answer)
    except (ValueError, TypeError):
        return 0.0, (
            f"Your answer '{prediction.answer}' could not be parsed as an integer. "
            f"The correct answer is {correct_answer}."
        )

    score = float(correct_answer == llm_answer)
    status = "correct" if score == 1.0 else "incorrect"
    return score, f"Your answer is {status}. The correct answer is {correct_answer}."


def evaluate_on_dataset(prompt: str, dataset: list, print_examples: bool = False) -> float:
    """Evaluate `prompt` on `dataset`. Returns accuracy in [0, 1].

    Uses an explicit per-example loop so reported numbers are exact and
    transparent (correct/total), and can optionally print predicted vs
    correct answer for every example.
    """
    predictor.predict.signature.instructions = prompt
    total = len(dataset)
    if total == 0:
        return 0.0

    num_correct = 0
    for idx, example in enumerate(dataset, start=1):
        prediction = predictor(problem=example.problem)
        score, _ = math_metric(example, prediction)
        num_correct += int(score)

        if print_examples:
            mark = "✓" if score == 1.0 else "✗"
            print(
                f"  [{mark}] q#{idx:03d}  predicted={prediction.answer}  correct={example.answer}"
            )

    accuracy = num_correct / total
    print(f"  Summary: {num_correct}/{total} correct ({accuracy:.2%})")
    return accuracy
