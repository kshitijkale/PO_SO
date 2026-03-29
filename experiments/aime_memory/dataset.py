"""AIME dataset loading — mirrors the DSPy setup exactly.

Splits:
  train  : first 50% of AI-MO/aimo-validation-aime (shuffled, seed=0)
  val    : last  50% of AI-MO/aimo-validation-aime
  test   : MathArena/aime_2025 × 5 (repeated for stable test-set evaluation)

Each example is a dspy.Example with fields:
  problem   (InputField)
  answer    (str, the correct integer answer)
"""

import random

import dspy
from datasets import load_dataset


def load_aime_dataset():
    """Return (trainset, valset, testset) as lists of dspy.Example."""
    train_split = load_dataset("AI-MO/aimo-validation-aime")["train"]
    train_split = [
        dspy.Example({
            "problem": x["problem"],
            "solution": x["solution"],
            "answer": x["answer"],
        }).with_inputs("problem")
        for x in train_split
    ]
    random.Random(0).shuffle(train_split)
    tot_num = len(train_split)

    test_split = load_dataset("MathArena/aime_2025")["train"]
    test_split = [
        dspy.Example({
            "problem": x["problem"],
            "answer": x["answer"],
        }).with_inputs("problem")
        for x in test_split
    ]

    train_set = train_split[: int(0.5 * tot_num)]
    val_set = train_split[int(0.5 * tot_num) :]
    test_set = test_split * 5

    return train_set, val_set, test_set
