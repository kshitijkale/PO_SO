"""DSPy-based AIME solver and metric — uses `problem` as the input field."""

import dspy


class AIMESolverSignature(dspy.Signature):
    problem = dspy.InputField(desc="The math problem to solve.")
    answer = dspy.OutputField(desc="The final numerical answer as a single integer.")


def run_llm(example, prompt: str):
    """Run the solver LLM with `prompt` as the system instructions.

    Creates a fresh predictor per call to avoid race conditions when called
    from multiple threads concurrently.
    """
    local_predictor = dspy.ChainOfThought(AIMESolverSignature)
    local_predictor.predict.signature.instructions = prompt
    return local_predictor(problem=example.problem)


def math_metric(example, prediction) -> tuple[float, str]:
    """Return (score, feedback_text).

    score = 1.0 if predicted integer matches the correct answer, else 0.0.
    feedback_text is shown to the reflection LLM as side_info.
    Includes the written solution when available (train/val set only).
    """
    correct_answer = int(example.answer)
    written_solution = getattr(example, "solution", "") or ""

    try:
        llm_answer = int(prediction.answer)
    except (ValueError, TypeError):
        feedback = (
            f"Your answer '{prediction.answer}' could not be parsed as an integer. "
            f"The correct answer is {correct_answer}."
        )
        if written_solution:
            feedback += (
                f" Here is the full step-by-step solution:\n{written_solution}\n\n"
                "Think about what takeaways you can learn from this solution to improve "
                "your approach to similar problems."
            )
        return 0.0, feedback

    score = float(correct_answer == llm_answer)
    if score == 1.0:
        feedback = f"Your answer is correct. The correct answer is {correct_answer}."
    else:
        feedback = f"Your answer is incorrect. The correct answer is {correct_answer}."
        if written_solution:
            feedback += (
                f" Here is the full step-by-step solution:\n{written_solution}\n\n"
                "Think about what takeaways you can learn from this solution to improve "
                "your approach to similar problems."
            )
    return score, feedback


def evaluate_on_dataset(prompt: str, dataset: list, print_examples: bool = False) -> float:
    """Evaluate `prompt` on `dataset`. Returns accuracy in [0, 1].

    Uses an explicit per-example loop so reported numbers are exact and
    transparent (correct/total), and can optionally print predicted vs
    correct answer for every example.
    """
    local_predictor = dspy.ChainOfThought(AIMESolverSignature)
    local_predictor.predict.signature.instructions = prompt
    total = len(dataset)
    if total == 0:
        return 0.0

    num_correct = 0
    for idx, example in enumerate(dataset, start=1):
        prediction = local_predictor(problem=example.problem)
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
