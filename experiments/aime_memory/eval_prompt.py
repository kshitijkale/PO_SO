"""Evaluate a single prompt on the AIME 2025 test set.

Edit PROMPT below, then run:
    uv run python -m experiments.aime_memory.eval_prompt
"""

import os
from pathlib import Path

import dspy
from dotenv import load_dotenv

from experiments.aime_memory.dataset import load_aime_dataset
from experiments.aime_memory.solver import evaluate_on_dataset

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".claude" / ".env", override=True)

# ── Edit your prompt here ────────────────────────────────────────────────────

PROMPT = '''\nSolve the given mathematical problem with thorough and rigorous step-by-step reasoning before providing only the final answer as a single integer.\n\nYour step-by-step reasoning must include:\n\n- A careful interpretation of the problem statement, explicitly restating all conditions and constraints.\n- Identification and explanation of any combinatorial, geometric, algebraic, or logical structures relevant to the problem.\n- Explicit treatment of conditions that affect maximality, exclusivity, uniqueness, or validity of configurations.\n- Application of combinatorial methods such as casework, inclusion-exclusion, symmetry, or recursion when appropriate.\n- Clear justifications for counting arguments, including enumeration of sets, partitions, or colorings, and how overlapping or disjoint subsets influence counts.\n- Verification of assumptions about maximal or minimal configurations, and whether adding or removing elements violates problem conditions.\n- Explicit handling of symmetry, equivalence classes, or indistinguishability where relevant.\n- Detailed algebraic or geometric reasoning, including coordinate assignments, vector or complex number manipulations, or angle calculations, as needed.\n- Use of Euler\u2019s formula or other topological or graph-theoretic tools when counting faces or regions in planar graphs or arrangements.\n- Confirmation that the final count or numeric answer satisfies all problem constraints (such as chip counts, simultaneous arrival conditions, or geometric properties).\n- When dealing with arrangements or configurations involving segments between points on lines, carefully account for intersections by considering pairs of points on each line and how these contribute to vertices, edges, and faces.\n- When maximizing or minimizing expressions involving complex numbers or trigonometric parameters, parameterize variables appropriately and use trigonometric identities, calculus, or inequalities (like Cauchy\u2013Schwarz) to find extrema.\n- Always reduce final numeric expressions to simplest integer form as required by the problem.\n\nDo not output extraneous or explanatory text beyond what is necessary for the rigorous reasoning and the final integer answer.\n\nThis instruction is designed to ensure precision in combinatorial enumeration, geometric reasoning, algebraic manipulation, and optimization problems, with special emphasis on:\n\n- Correctly counting planar regions formed by segments, including accounting for intersection points as vertices, and applying Euler\u2019s formula accurately.\n- Recognizing when recursive or combinatorial formulas apply and deriving or verifying them with base cases and induction or direct combinatorial arguments.\n- Handling complex numbers and trigonometric parametrizations carefully to maximize real parts of complex expressions, ensuring the final answer is an integer.\n- Using problem examples to confirm or refute candidate formulas and adjust reasoning accordingly.\n\nFollow this methodology strictly to produce both a fully justified detailed solution and the exact final integer answer.\n'''
# ── Config ───────────────────────────────────────────────────────────────────

SOLVER_LM = "openai/gpt-4.1-mini"
SOLVER_TEMPERATURE = 1.0
SOLVER_MAX_TOKENS = 32768

# ─────────────────────────────────────────────────────────────────────────────


def main():
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing.")

    print("Loading dataset...")
    _, _, testset = load_aime_dataset()
    print(f"Test set: {len(testset)} problems")

    solver_lm = dspy.LM(
        SOLVER_LM,
        api_key=api_key,
        temperature=SOLVER_TEMPERATURE,
        max_tokens=SOLVER_MAX_TOKENS,
        cache=False,
    )
    dspy.configure(lm=solver_lm)

    print(f"\nPrompt ({len(PROMPT)} chars):\n{PROMPT}\n")
    print("─" * 60)

    score = evaluate_on_dataset(PROMPT, testset, print_examples=True)
    print(f"\nFinal score: {score:.2%} ({int(score * len(testset))}/{len(testset)})")


if __name__ == "__main__":
    main()
