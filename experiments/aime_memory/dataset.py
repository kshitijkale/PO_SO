"""AIME dataset loading — mirrors the DSPy setup exactly.

Splits:
  train  : first 50% of AI-MO/aimo-validation-aime (shuffled, seed=0)
  val    : last  50% of AI-MO/aimo-validation-aime
  test   : MathArena/aime_2025 (single pass over the held-out set)

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
    train_examples = [
        dspy.Example(
            problem=x["problem"],
            answer=x["answer"],
        ).with_inputs("problem")
        for x in train_split
    ]
    random.Random(0).shuffle(train_examples)

    tot_num = len(train_examples)
    trainset = train_examples[: tot_num // 2]
    valset = train_examples[tot_num // 2 :]

    test_split = load_dataset("MathArena/aime_2025")["train"]
    test_examples = [
        dspy.Example(
            problem=x["problem"],
            answer=x["answer"],
        ).with_inputs("problem")
        for x in test_split
    ]
    testset = test_examples

    return trainset, valset, testset
