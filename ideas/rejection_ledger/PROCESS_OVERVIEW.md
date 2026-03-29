# AIME Prompt Optimization — Process Overview

This document describes how the current GEPA-based AIME experiment works end to end.

---

## The Goal

Automatically improve a system prompt for an LLM math solver. The solver attempts AIME competition problems and must output a single integer answer. The process tries to find a prompt that maximizes accuracy across the problem set without any human intervention.

---

## The Models

Up to three separate LLMs are in play:

- **Solver LM** — does the actual math. Given a system prompt and an AIME problem, produces a chain-of-thought and a final integer answer. Runs at temperature 0.0 (deterministic).
- **Reflector LM** — does the prompt engineering. Given evidence of where the current prompt is failing, proposes a revised prompt. Runs at temperature 0.7.
- **Summarizer LM** — active only with `--memory-version ledger`. Generates a one-sentence description of each rejected mutation by analyzing what changed and what the new prompt actually produced. Defaults to the same model as the Reflector but is a completely separate LLM call.

The Solver and Reflector never interact directly. The Solver runs, produces answers, and those are evaluated. The Reflector reads a structured summary of those results and writes a new prompt. The Summarizer only fires after a rejection, to populate the ledger for future iterations.

---

## The Data

Three splits, all drawn from published AIME problem sets:

- **Train set (45 problems)** — used for iterative improvement. The reflector sees failures from this set.
- **Val set (45 problems)** — used to score every accepted prompt. Determines which prompt is currently "best."
- **Test set (AIME 2025)** — held out entirely. Evaluated once at the end to measure real-world performance.

The train/val split comes from `AI-MO/aimo-validation-aime` (shuffled, seeded). The test set is `MathArena/aime_2025`.

---

## One Iteration of the Loop

### 1. Select a Prompt

GEPA picks the current best prompt from the Pareto frontier — the one with the highest validation accuracy so far. On iteration 1, this is the seed prompt:

> "Solve the problem and provide the answer provide the final answer as a single integer."

### 2. Sample a Minibatch

3 problems are sampled from the train set (epoch-shuffled, no repeats until all 45 are seen).

### 3. Evaluate the Current Prompt → Solver LM (3 calls)

The Solver LM runs on all 3 problems using the selected prompt. It uses DSPy's `ChainOfThought` module, which internally generates a reasoning trace before producing a final integer answer. Both the answer and the reasoning are recorded per problem.

### 4. Build the Reflective Dataset

For each of the 3 problems, a record is assembled:

```
Inputs:            Problem: [AIME problem text]
Generated Outputs: Answer: 37
                   Reasoning: [solver's chain-of-thought]
Feedback:          Your answer is incorrect. The correct answer is 42.
```

Key design choices:

- **Feedback contains only the correct answer.** No written solution is ever included. The reflector must diagnose failure from the outcome signal and the solver's own reasoning — it cannot copy a worked solution.
- **Generated Outputs includes the solver's reasoning.** The reflector sees how the solver thought about the problem, not just what answer it gave.

### 5. Inject the Rejection Ledger (conditional)

If `--memory-version ledger` is active, the ledger for the current parent prompt is formatted and spliced into the reflection prompt template. It lists every prior rejected mutation from this exact parent, each described in one sentence with its score, framed as soft context:

```
== PAST ATTEMPTS FROM THIS PROMPT (context only — use your judgment) ==
• "Tried step-by-step verification — solver still made sign errors" — scored 1/3, needed >2/3
• "Restructured as numbered list — no answer improvement" — scored 2/3, needed >2/3

These attempts didn't help on past minibatches. They may or may not be relevant to the current one.
```

The reflector is free to try a similar direction — it uses its own judgment about whether the evidence is conclusive.

### 6. Call the Reflector → Reflector LM (1 call)

The Reflector LM receives:
- The current prompt text
- The 3-example reflective dataset (problem → reasoning + answer → correct answer)
- Optionally: the rejection ledger block

It proposes an improved prompt. The response is parsed to extract the new prompt text.

### 7. Evaluate the Proposed Prompt → Solver LM (up to 3 calls)

The Solver runs the proposed prompt on the same 3 minibatch problems. Results may be served from cache if this exact (candidate, example) pair was already evaluated. The proposed candidate's outputs are retained in case the proposal is rejected (used by the Summarizer in Step 8).

### 8. Accept or Reject

```
new_sum > curr_sum  →  ACCEPTED
new_sum ≤ curr_sum  →  REJECTED
```

The threshold is strict: the proposed prompt must score strictly higher than the current prompt on the same 3 problems. Ties are rejections.

### 8a. On Rejection — Summarize → Summarizer LM (1 call, ledger only)

If the proposal is rejected and `--memory-version ledger` is active, the Summarizer LM generates a one-sentence ledger entry. It receives the old prompt, the new prompt, and what the new prompt actually produced on each minibatch problem (answer + reasoning + pass/fail). The result is stored in the ledger under the parent's hash and will be injected in Step 5 of any future iteration that starts from this same parent.

Falls back to a heuristic difflib summary if the LM call fails.

### 9. Validate (conditional) → Solver LM (45 calls)

If the new prompt is accepted, GEPA evaluates it on the full 45-problem val set to get a stable accuracy score. This score determines its position on the Pareto frontier and whether it can become the selected parent for future iterations.

---

## Across Iterations

The loop runs until the evaluation budget (`--max-calls`) is exhausted. With a budget of 500 and minibatches of 3, this gives roughly 80–160 iterations depending on how often val-set evaluations trigger.

The best prompt is whichever accepted candidate achieved the highest val-set accuracy. If no proposal was ever accepted, the seed prompt is returned.

At the end, the best prompt and the seed prompt are both evaluated on the held-out test set (AIME 2025) to measure actual improvement.

---

## What the Reflector Knows vs. Does Not Know

| What the reflector sees | What the reflector does NOT see |
|---|---|
| The full problem text | Internal DSPy state |
| The solver's predicted answer | Val-set scores |
| The solver's chain-of-thought reasoning | Other candidate prompts |
| Whether the answer was correct | The test set |
| The correct answer | Written solutions |
| Prior rejected mutations with scores (ledger mode) | |

The core constraint: the reflector sees the solver's reasoning and the correct answer, but is never given a worked solution to copy from. It must diagnose prompt-level failure modes — things a better system prompt could plausibly fix — from the solver's own output alone.

---

## LLM Call Budget per Iteration

| Step | LLM | Calls |
|---|---|---|
| Eval current prompt | Solver LM | 3 |
| Reflection / proposal | Reflector LM | 1 |
| Eval proposed prompt | Solver LM | 0–3 (cached) |
| Rejection summarization (ledger only) | Summarizer LM | 0 or 1 |
| Val-set evaluation (if accepted) | Solver LM | 45 |

Typical rejected iteration: **7 LLM calls** (+ 1 summarizer if ledger is on).
Typical accepted iteration: **52 LLM calls**.

---

## Output

Each run produces a `run_dir` with:
- `log.jsonl` — complete event stream
- `iterations/` — full narrative markdown for each iteration
- `candidates/` — JSON snapshot of every proposed prompt
- `llm_calls/` — verbatim prompts and responses for every reflector call
- `states/` — 12 numbered JSON snapshots per iteration (one per step)
- `memory/` — ledger injection events and memory state snapshots
- `lineage.jsonl` / `lineage_tree.md` — ancestry graph of all candidates
- `metrics.jsonl` / `metrics_summary.json` — aggregate counters over time
