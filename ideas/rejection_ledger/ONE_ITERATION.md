# One Iteration of the GEPA-AIME Loop

A single iteration has two distinct phases with two separate LLMs: **Solver LM** and **Reflector LM**. There is optionally a third call for the **Summarizer LM** (rejection ledger). Here is exactly what flows where.

---

## The Two LLMs

| Role | Model | Set by |
|---|---|---|
| **Solver LM** | `gpt-4.1-mini` (default) | `--solver-lm` flag, via DSPy |
| **Reflector LM** | `gpt-4.1-mini` (default) | `--reflection-lm` flag, via LiteLLM |
| **Summarizer LM** | same as Reflector | only active if `--memory-version ledger` |

The Solver and Reflector never share a call — they are invoked at separate points in the loop.

---

## Step-by-Step: One Iteration

### 1. Select a Candidate (no LLM call)

`ParetoCandidateSelector` picks the current best prompt from the Pareto frontier tracked in `GEPAState`. On iteration 1 this is always the seed prompt:

```
"Solve the problem and provide the answer provide the final answer as a single integer."
```

This is a `dict[str, str]` with a single key `"system_prompt"`.

---

### 2. Sample a Minibatch (no LLM call)

`EpochShuffledBatchSampler` draws 3 problems from the 45-problem train set without repeats until the epoch is exhausted. Each problem is a `dspy.Example` with fields:

- `problem` — the AIME problem text
- `answer` — the correct integer answer
- `solution` — written step-by-step solution (used for feedback only, not passed to solver)

---

### 3. Evaluate the Current Prompt → Solver LM (3 calls)

**Who calls it:** `AIMEAdapter.evaluate(minibatch, current_candidate, capture_traces=True)`

**What the Solver LM receives per problem:**

```
[system]  <current system_prompt text>
[user]    Let's think step by step. Problem: <AIME problem text>
          Answer: <blank — model fills this in>
```

DSPy's `ChainOfThought` injects the system prompt as the signature's instructions and asks the model to produce `reasoning` (chain-of-thought) and `answer` (final integer). The model sees 1 call per problem, so 3 Solver LM calls in parallel (up to 32 workers).

**What comes back:** `prediction.answer` (integer string) and `prediction.reasoning` (chain-of-thought text, internal only).

**Scoring:** `math_metric` compares `int(prediction.answer)` to `int(example.answer)`. Returns `score=1.0` or `score=0.0` and a feedback string. The written solution is **not** included in feedback — only the correct answer is revealed.

**Outputs stored:**
- `eval_curr.scores` — `[1.0, 0.0, 1.0]` (example)
- `eval_curr.outputs` — list of `{predicted_answer, correct_answer, reasoning}`
- `eval_curr.trajectories` — full `AIMETrajectory` per problem (includes problem text, both answers, reasoning, score, feedback)

---

### 4. Build the Reflective Dataset (no LLM call)

`AIMEAdapter.make_reflective_dataset()` converts the trajectories into a structured dataset for the Reflector. One record per problem:

```
Inputs:            Problem: <AIME problem text>
Generated Outputs: Answer: <predicted integer>
                   Reasoning: <solver's chain-of-thought>
Feedback:          Your answer is incorrect. The correct answer is 42.
```

The `Feedback` field contains **only** whether the answer was right or wrong and the correct integer — no written solution is included. The `Reasoning` field is the solver's internal chain-of-thought — the reflector sees how the solver thought about the problem.

---

### 5. Inject Rejection Ledger (no LLM call, conditional)

Only active when `--memory-version ledger`.

`RejectionLedger.format_for_prompt(parent_hash, "system_prompt")` looks up all previously rejected mutations from this exact parent prompt (keyed by SHA-256 hash of the candidate dict). If any entries exist, they are formatted as:

```
== PAST ATTEMPTS FROM THIS PROMPT (context only — use your judgment) ==
• "tried adding step-by-step verification instructions" — scored 1/3, needed >2/3
• "added geometry-specific heuristics" — scored 0/3, needed >2/3

These attempts didn't help on past minibatches. They may or may not be relevant to the current one.
```

This block is spliced into the reflection prompt template just before the `"Provide the new instructions"` line.

---

### 6. Call the Reflector LM (1 call)

**Who calls it:** `ReflectiveMutationProposer.propose_new_texts()` → `InstructionProposalSignature.run_with_trace()`

**What the Reflector LM receives (one message):**

```
You are an expert prompt engineer...

Current instruction:
  <current system_prompt text>

Below are examples where the current instruction produced outputs,
along with feedback on those outputs:

Example 1:
  Inputs:            Problem: <problem text>
  Generated Outputs: Answer: 37
                     Reasoning: <chain-of-thought>
  Feedback:          Your answer is incorrect. The correct answer is 42.
                     Here is the full step-by-step solution: ...

Example 2: ...
Example 3: ...

[optionally: rejection ledger block here]

Provide the new instructions that will improve performance.
```

**What comes back:** Raw text response containing a new system prompt, parsed out by `InstructionProposalSignature`. The extracted instruction goes into `new_texts["system_prompt"]`.

Temperature is 0.7 (default), so the reflector is creative. The solver runs at 0.0 (deterministic).

---

### 7. Evaluate the Proposed Prompt → Solver LM (up to 3 more calls)

**Who calls it:** `state.cached_evaluate_full()` → `AIMEAdapter.evaluate(minibatch, new_candidate, capture_traces=False)`

Same 3 problems, same format as Step 3, but with the **new proposed system prompt** instead of the current one. Results may be served from cache if this exact (candidate, example) pair was already evaluated.

**What comes back:** `new_scores = [1.0, 1.0, 0.0]` (example).

The proposed candidate's outputs are also captured into `_proposed_outputs` — used for the rejection ledger summarizer if this proposal is rejected.

---

### 8. Accept or Reject (no LLM call)

```
new_sum = sum(new_scores)           # e.g. 2.0
curr_sum = sum(eval_curr.scores)    # e.g. 1.0

if new_sum > curr_sum:  → ACCEPTED
else:                   → REJECTED
```

The threshold is strict: the proposed prompt must score **strictly higher** than the current prompt on the same 3 problems.

---

### 9. Record Rejection → Summarizer LM (1 call, conditional)

Only fires if the proposal was **rejected** AND `--memory-version ledger` is active.

**Who calls it:** `ReflectiveMutationProposer._summarize_rejection()`

**What the Summarizer LM receives:**

```
You are analyzing a failed prompt change for an AI system.
In one sentence (max 100 chars), describe what strategy the new prompt attempted
and why it didn't improve results.

Old prompt:
  <current system_prompt, up to 400 chars>

New prompt:
  <proposed system_prompt, up to 400 chars>

Results on minibatch (scored 1/3, needed >2/3):
  Example 1 [FAIL]: input=<problem> output=<predicted answer + reasoning>
  Example 2 [PASS]: input=<problem> output=<predicted answer + reasoning>
  Example 3 [FAIL]: input=<problem> output=<predicted answer + reasoning>

Respond with only the one-sentence description.
```

**What comes back:** One sentence, e.g. `"Tried adding arithmetic verification steps but solver still made algebra errors."` This is stored in `LedgerEntry.change_summary` under the parent prompt's hash, to be injected in Step 5 of future iterations from the same parent.

Falls back to a heuristic difflib-based summary if the LM call fails.

---

### 10. Validate (conditional, Solver LM × 45 calls)

If the proposal was **accepted**, GEPA evaluates the new candidate on the full 45-problem val set to assign it a stable score for Pareto tracking. This is 45 more Solver LM calls (parallelized). The result goes into `state.program_full_scores_val_set[new_candidate_idx]`.

This val-set score determines whether the new candidate can become the parent for future iterations.

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      GEPAState / Pareto Frontier            │
│   current_candidate = { "system_prompt": "<text>" }         │
└───────────────────┬─────────────────────────────────────────┘
                    │ Step 1: select
                    ▼
              [current prompt]
                    │
                    │ Step 2: sample 3 problems from trainset
                    ▼
         [minibatch: 3 × dspy.Example]
                    │
    ┌───────────────┤ Step 3: evaluate current prompt
    │               ▼
    │       ┌──────────────┐
    │       │  Solver LM   │  × 3 calls (parallel)
    │       │  (DSPy CoT)  │  receives: system_prompt + problem
    │       └──────┬───────┘
    │              │ prediction.answer + prediction.reasoning
    │              ▼
    │        math_metric → score (0 or 1) + feedback string
    │              │
    │       eval_curr: scores, outputs, trajectories
    │               │
    │ Step 4: make_reflective_dataset
    │         → 3 records: {Inputs, Generated Outputs, Feedback}
    │               │
    │ Step 5: inject rejection ledger (if enabled)
    │         → append ledger block to prompt template
    │               │
    │               ▼
    │       ┌──────────────┐
    │       │  Reflector   │  × 1 call
    │       │     LM       │  receives: current prompt + 3 reflective
    │       │  (LiteLLM)   │            records + optional ledger
    │       └──────┬───────┘
    │              │ new system_prompt text
    │              ▼
    │       new_candidate = { "system_prompt": "<new text>" }
    │               │
    └───────────────┤ Step 7: evaluate proposed prompt
                    ▼
            ┌──────────────┐
            │  Solver LM   │  × up to 3 calls (cached if seen before)
            │  (DSPy CoT)  │  receives: new_prompt + same 3 problems
            └──────┬───────┘
                   │ new_scores
                   ▼
            new_sum > curr_sum?
           /                   \
         YES                    NO
          │                      │
    ACCEPTED                REJECTED
          │                      │
    Step 10:           Step 9 (if ledger):
    val-set eval       ┌──────────────┐
    (45 × Solver LM)   │ Summarizer   │  × 1 call
                       │    LM        │  receives: old+new prompt,
                       │ (LiteLLM)   │            3 outputs+scores
                       └──────┬───────┘
                              │ one-sentence summary
                              ▼
                       LedgerEntry stored under parent_hash
                       (injected in next iteration from same parent)
```

---

## Call Count per Iteration

| Event | LLM | Calls |
|---|---|---|
| Eval current prompt (Step 3) | Solver LM | 3 |
| Reflection / proposal (Step 6) | Reflector LM | 1 |
| Eval proposed prompt (Step 7) | Solver LM | 0–3 (cached) |
| Rejection summary (Step 9, ledger only) | Summarizer LM | 0 or 1 |
| Val-set evaluation (Step 10, if accepted) | Solver LM | 45 |

A typical rejected iteration: **3 + 1 + 3 = 7 LLM calls** (plus 1 summarizer if ledger is on).
A typical accepted iteration: **3 + 1 + 3 + 45 = 52 LLM calls**.
