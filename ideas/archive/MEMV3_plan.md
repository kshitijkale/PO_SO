# Memory V3 Plan

## The Problem V3 Solves

V2 correctly diagnosed that raw text diffs (V1) are useless as memory — and replaced
them with LLM-generated lessons. The lessons were recorded, injected, and even included
attribution tracking. But the experiment (aime_memv2_run_01) showed that **the reflection
LLM ignored the lessons anyway**. `memory_reuse_detected` was false for 25/27 iterations.
All 27 proposals were variations of "add more verification language." The optimizer
plateaued at iteration 13 and never recovered.

The root cause is not that memory is bad — it's that memory alone can't fix **strategy
collapse**. The reflection LLM has one attractor strategy and no mechanism pushes it
toward alternatives. V3 addresses the three upstream failures that make memory ineffective:

1. The reflection LLM has no diagnostic signal about *why* answers are wrong
2. The reflection LLM has no mechanism to avoid repeating the same strategy
3. The memory evicts useful lessons before they accumulate enough weight

---

## How ReflectionMemory Works (Background)

ReflectionMemory is a rolling buffer of `ReflectionMemoryEntry` objects that lives on the
`ReflectiveMutationProposer`. It is **not** the candidate prompts, not the reflective
dataset, and not the evaluation cache — it is a separate data structure that records
*what was learned* from each optimization step.

**When entries are created:** At the end of every iteration in
`ReflectiveMutationProposer.propose()`, after both the old and new candidates have been
evaluated on the minibatch. The proposer compares the scores, determines accept/reject,
then calls the lesson LLM to generate a structured lesson (intent, lesson text, categories
succeeded/failed). This lesson is stored as a `ReflectionMemoryEntry` and added to the
buffer via `ReflectionMemory.add()`.

**When entries are read:** At the start of the *next* iteration, inside
`propose_new_texts()`. Before calling the reflection LLM to generate a new candidate
prompt, the proposer calls `ReflectionMemory.format_for_prompt()` which renders the most
recent 5 entries as a text block. This block is appended to the reflection prompt template,
so the reflection LLM can see what was tried before and what worked/failed.

**When entries are evicted:** The buffer has a fixed capacity (`max_entries`, default 10).
When `add()` would exceed capacity, the oldest entry is silently discarded (FIFO).

**The lifecycle in one iteration:**

```
1. Select candidate from Pareto front
2. Sample minibatch (3 training examples)
3. Evaluate current candidate on minibatch (with traces)
4. Build reflective dataset from traces
5. Read memory → format_for_prompt() → inject into reflection prompt    ← MEMORY READ
6. Call reflection LLM → get proposed new candidate text
7. Evaluate proposed candidate on same minibatch
8. Compare scores → accept/reject
9. Call lesson LLM → generate structured lesson
10. memory.add(lesson_entry)                                            ← MEMORY WRITE
11. Return proposal to engine (engine handles Pareto update, val eval)
```

Steps 5 and 10 are the only points where memory is touched. The rest of the loop is
unchanged from the memoryless baseline.

---

## High-Level Architecture

V3 adds three components, each targeting one root cause:

```
V2 loop:
  select candidate → eval on minibatch (with traces) → build reflective dataset
  → propose new candidate → eval new candidate → lesson LLM → store lesson

V3 loop:
  select candidate → eval on minibatch (with traces)
  → [NEW] error diagnosis: per-example LLM call to identify WHERE reasoning failed
  → build reflective dataset (now includes error diagnosis)
  → [NEW] novelty gate: reject prompt if cosine-similar to recent proposals
  → propose new candidate (reflection prompt now includes strategy constraint)
  → eval new candidate → lesson LLM → store lesson
  → [NEW] consolidation: periodically merge old entries into a standing summary
```

### Component 1: Error Diagnosis

**What:** After evaluation but before building the reflective dataset, make a small LLM
call per *incorrect* example that compares the model's reasoning trace to the correct
answer and produces a structured error diagnosis.

**Why:** The current feedback is `"Your answer is incorrect. The correct answer is 321."`
The reflection LLM sees this and can only guess what went wrong. But the full reasoning
trace (500+ lines of chain-of-thought) is *right there* in the trajectory — it's just
never analyzed. The reflection LLM receives the raw trace in the reflective dataset but
can't effectively process 1500+ lines across 3 examples to find the bugs.

**What the diagnosis produces:**

```json
{
  "error_location": "Step 7: simplified (13π/4) mod 2π as π/4 instead of 5π/4",
  "error_type": "arithmetic",
  "problem_domain": "complex_numbers_roots_of_unity",
  "what_would_fix_it": "Need to compute 13/4 = 3.25, so 3.25 - 3 = 0.25 periods, giving π/2 not π/4"
}
```

This gives the reflection LLM a specific, actionable signal: "the solver made an
arithmetic error in modular reduction of angles." From this, the reflection LLM can
propose targeted fixes ("double-check modular arithmetic", "reduce angles to [0, 2π)
explicitly before using them") instead of generic "verify your work."

**How it fits in the adapter:** The error diagnosis call happens inside
`AIMEAdapter.evaluate()` (or a new `diagnose_errors()` method). The diagnosis is stored
on the trajectory object and flows into `make_reflective_dataset()` as a new
`"Error Diagnosis"` field alongside the existing `"Feedback"` field.

### Component 2: Novelty Gate

**What:** Before accepting a proposed prompt from the reflection LLM, check whether it
is semantically novel relative to recent proposals. If it's too similar, reject it and
re-prompt with an explicit constraint.

**Why:** In the experiment, all 27 proposals were semantic near-duplicates. The reflection
LLM is stuck in a local attractor. Memory says "try something different" but that's
advisory — nothing enforces it. The novelty gate makes it structural.

**How it works:**

1. Maintain a rolling buffer of the last K proposed prompt texts (accepted or rejected).
2. When a new proposal comes in, compute a similarity score against the buffer.
   Use a cheap metric: normalized edit distance, or Jaccard similarity on word n-grams.
   (Embedding-based cosine is better but adds a dependency; start with n-gram Jaccard.)
3. If similarity > threshold (e.g., 0.7), reject the proposal and re-call the reflection
   LLM with an augmented prompt that says:

   ```
   Your previous proposal was too similar to past attempts. Here are the strategies
   that have already been tried:
   - [strategy 1 intent]
   - [strategy 2 intent]
   - [strategy 3 intent]

   You MUST try a fundamentally different approach. Consider:
   - Changing the structure, not just the wording
   - Removing content instead of adding
   - Adding domain-specific heuristics (e.g., "check your answer mod 10")
   - Using a different problem-solving framework entirely
   ```

4. Allow max 2 retries. If still too similar after 2 retries, accept the proposal anyway
   (don't block progress entirely).

**Where it lives:** Inside `ReflectiveMutationProposer.propose_new_texts()`, after
the reflection LLM returns but before the result is accepted.

### Component 3: Memory Consolidation

**What:** When memory reaches capacity and is about to evict an entry, first consolidate
all current entries into a standing 2-3 sentence summary that persists across evictions.

**Why:** V2 uses FIFO eviction. By iteration 19, lessons 1-9 were gone — including the
critical early lesson that "verbosity doesn't help." The optimizer re-learned this at
iteration 22, wasting iterations. A consolidated summary retains the cumulative wisdom
even as individual entries are evicted.

**How it works:**

1. `ReflectionMemory` gains a `consolidated_summary: str` field (initially empty).
2. When `add()` triggers an eviction, check if there are 3+ evicted entries since the
   last consolidation. If so, make a small LLM call:

   ```
   Here are the lessons from a prompt optimization run, oldest first:

   [existing consolidated_summary, if any]

   [entries being evicted or recently evicted]

   Produce a 2-3 sentence summary of the key findings. What strategies were tried?
   Which worked, which didn't? What types of problems remain unsolved?
   ```

3. The consolidated summary is prepended to the memory block in `format_for_prompt()`:

   ```
   ## Optimization History

   ### Standing Summary (from earlier iterations)
   Seven variations of "add step-by-step verification" were tried. Only one improved
   accuracy (iter 8, +2.0). Increasing verbosity beyond ~300 chars consistently fails
   to help and sometimes hurts. Geometry problems remain unsolved across all attempts.

   ### mem_000018 | Iter 18 [REJECTED ±0.00]
   ...
   ```

4. Consolidation uses the `lesson_lm` (same model as lesson generation). One extra LLM
   call every ~5 evictions — negligible cost.

---

## Implementation Steps

### Step 1: Error Diagnosis (in the adapter, not in core GEPA)

This is adapter-specific. For the AIME experiment:

**File:** `experiments/aime_memory/adapter.py`

Add a `_diagnose_error()` method to `AIMEAdapter`:

```python
def _diagnose_error(
    self, problem: str, reasoning: str, predicted: str, correct: str
) -> dict[str, str]:
    """Call diagnosis LLM to identify where reasoning went wrong."""
    prompt = _ERROR_DIAGNOSIS_PROMPT.format(
        problem=problem,
        reasoning=reasoning[:3000],  # cap to avoid token blowup
        predicted_answer=predicted,
        correct_answer=correct,
    )
    raw = self.diagnosis_lm(prompt)
    # parse JSON, fallback to {"error_location": "unknown", ...}
    ...
```

Call it inside `evaluate()` when `capture_traces=True` and `score == 0.0`. Store the
result on `AIMETrajectory` as a new `error_diagnosis` field. In
`make_reflective_dataset()`, include it as an `"Error Diagnosis"` key.

**New constructor param:** `diagnosis_lm: LanguageModel | None = None`. When None,
skip diagnosis (backward compatible). Wire through from `run.py` via a `--diagnosis-lm`
flag.

The diagnosis prompt template:

```
You are analyzing an incorrect math solution.

## Problem
{problem}

## Model's Reasoning (may be long — focus on where it diverges from correct)
{reasoning}

## Model's Answer: {predicted_answer}
## Correct Answer: {correct_answer}

Identify the FIRST point where the reasoning goes wrong. Respond with JSON only:
{
  "error_location": "<quote the specific step or sentence where the error occurs>",
  "error_type": "<one of: arithmetic, algebraic_manipulation, conceptual_misunderstanding, wrong_formula, missed_case, premature_conclusion, other>",
  "problem_domain": "<e.g. number_theory, geometry, combinatorics, algebra, trigonometry>",
  "what_would_fix_it": "<1 sentence: what should have been done instead>"
}
```

**Important:** This is NOT a core GEPA change. It's an adapter enhancement. Other
adapters can implement their own diagnosis logic. The core framework doesn't need to
know about error diagnosis — it just sees richer reflective dataset records.

### Step 2: Novelty Gate (in core GEPA)

**File:** `src/gepa/proposer/reflective_mutation/reflective_mutation.py`

Add to `ReflectiveMutationProposer`:

```python
# New fields
self._recent_proposals: list[str] = []
self._max_recent_proposals: int = 10
self._novelty_threshold: float = 0.7
self._max_novelty_retries: int = 2
```

New method:

```python
def _jaccard_ngram_similarity(self, a: str, b: str, n: int = 3) -> float:
    """Word n-gram Jaccard similarity between two strings."""
    words_a = a.lower().split()
    words_b = b.lower().split()
    if len(words_a) < n or len(words_b) < n:
        return 0.0
    ngrams_a = set(tuple(words_a[i:i+n]) for i in range(len(words_a) - n + 1))
    ngrams_b = set(tuple(words_b[i:i+n]) for i in range(len(words_b) - n + 1))
    if not ngrams_a or not ngrams_b:
        return 0.0
    return len(ngrams_a & ngrams_b) / len(ngrams_a | ngrams_b)

def _is_novel(self, proposed_text: str) -> bool:
    """Check if proposed text is sufficiently different from recent proposals."""
    for past in self._recent_proposals:
        if self._jaccard_ngram_similarity(proposed_text, past) > self._novelty_threshold:
            return False
    return True
```

Modify `propose_new_texts()`: after getting `result["new_instruction"]`, check novelty.
If not novel, rebuild the prompt with a novelty constraint appended and re-call the LLM.
Track retries. After all components are proposed, append the new texts to
`_recent_proposals`.

**New constructor params:**
- `novelty_threshold: float = 0.7`
- `max_novelty_retries: int = 2`

Wire through `optimize()` in `api.py`.

**New callback event:** `NoveltyRejectionEvent` — fired when a proposal is rejected for
being too similar. Contains the similarity score, the retry count, and the past proposal
it was most similar to.

### Step 3: Memory Consolidation (in core GEPA)

**File:** `src/gepa/proposer/reflective_mutation/memory.py`

Add to `ReflectionMemory`:

```python
consolidated_summary: str = ""
_consolidation_lm: LanguageModel | None = field(default=None, repr=False)
_evictions_since_consolidation: int = field(default=0, repr=False)
_consolidation_interval: int = field(default=3, repr=False)
_pending_evicted: list[ReflectionMemoryEntry] = field(default_factory=list, repr=False)
```

Modify `add()`: when an entry is evicted, increment `_evictions_since_consolidation`
and append to `_pending_evicted`. If `_evictions_since_consolidation >= _consolidation_interval`
and `_consolidation_lm is not None`, call `_consolidate()`.

New method:

```python
def _consolidate(self) -> None:
    """Merge evicted entries into consolidated_summary."""
    evicted_text = "\n".join(
        f"Iter {e.iteration}: intent='{e.intent}', lesson='{e.lesson}', "
        f"{'ACCEPTED' if e.accepted else 'REJECTED'} ({e.score_before:.1f}→{e.score_after:.1f})"
        for e in self._pending_evicted
    )
    prompt = _CONSOLIDATION_PROMPT.format(
        existing_summary=self.consolidated_summary or "(none yet)",
        new_entries=evicted_text,
    )
    self.consolidated_summary = self._consolidation_lm(prompt).strip()
    self._pending_evicted = []
    self._evictions_since_consolidation = 0
```

Modify `format_for_prompt()`: if `consolidated_summary` is non-empty, prepend it after
the `## Optimization History` header:

```python
lines: list[str] = ["## Optimization History\n"]
if self.consolidated_summary:
    lines.append("### Standing Summary (from earlier iterations)")
    lines.append(self.consolidated_summary)
    lines.append("")
```

The consolidation prompt:

```
You are summarizing the history of a prompt optimization run.

## Existing Summary
{existing_summary}

## New Entries (being evicted from the rolling window)
{new_entries}

Write a 2-3 sentence summary that integrates these new findings with the existing
summary. Focus on: which strategies were tried, which worked/failed, and what problem
types remain unsolved. Be specific — name the strategies and outcomes.
```

**New constructor param for ReflectionMemory:** `consolidation_lm: LanguageModel | None`.
Wire through from `ReflectiveMutationProposer` → `optimize()`.

**New callback event:** `MemoryConsolidatedEvent` with `iteration`, `entries_consolidated`,
`old_summary`, `new_summary`.

### Step 4: Wire Everything Through `api.py`

New params on `optimize()`:

```python
# Novelty gate
novelty_threshold: float = 0.7,
max_novelty_retries: int = 2,
```

The `consolidation_lm` reuses `lesson_lm` (or `reflection_lm` as fallback) — no new
param needed. Pass `lesson_lm_callable` to `ReflectionMemory(consolidation_lm=...)`.

### Step 5: Update `experiments/aime_memory/`

**`adapter.py`:** Add `diagnosis_lm` param, `_diagnose_error()`, wire into `evaluate()`
and `make_reflective_dataset()`.

**`run.py`:** Add `--diagnosis-lm` flag (default: same as `--solver-lm`). Pass to adapter.

### Step 6: Tests

**`tests/test_reflection_memory.py`:**
- `test_consolidation_triggers_after_interval` — add entries until eviction, verify
  `consolidated_summary` is populated
- `test_consolidation_lm_failure_graceful` — consolidation LLM raises, verify no crash
- `test_format_for_prompt_includes_standing_summary` — verify the standing summary
  appears in formatted output

**`tests/test_novelty_gate.py`** (new file):
- `test_jaccard_similarity_identical` → 1.0
- `test_jaccard_similarity_different` → low score
- `test_is_novel_rejects_similar` — similar prompt rejected
- `test_novelty_retry_produces_different_output` — mock LLM, verify retry prompt includes
  strategy constraint
- `test_novelty_max_retries_accepts_anyway` — after N retries, accepts even if similar

---

## What This Does NOT Include (Deliberately)

- **Embedding-based similarity.** Word n-gram Jaccard is cheap and doesn't need an
  embedding model. If it's insufficient, swap in embeddings later.
- **Strategy bank / pre-seeded strategies.** Tempting, but it would make the system
  task-specific. The novelty gate achieves diversity pressure without hard-coding
  alternatives.
- **Meta-lessons or lesson-over-lessons.** Over-engineering. Consolidation handles the
  "accumulated wisdom" problem without recursive LLM calls.
- **Changing the reflection prompt template.** The default template in
  `instruction_proposal.py` is generic by design (it works across all adapters). V3's
  improvements come through richer input data (error diagnosis) and structural constraints
  (novelty gate), not by rewriting the template.
- **Larger minibatches.** This is a hyperparameter, not an architectural change. The user
  can already set `reflection_minibatch_size=5` in `optimize()`.

---

## Expected Outcome

| Component | What it fixes | Evidence from experiment |
|-----------|---------------|------------------------|
| Error diagnosis | Reflection LLM gets specific failure modes instead of "incorrect" | Iter 19: solver had a modular arithmetic bug in step 7, but reflection LLM only saw "incorrect, correct answer is X" |
| Novelty gate | Prevents 27 iterations of the same strategy | All 27 intents were near-identical variants of "add verification language" |
| Consolidation | Preserves "verbosity doesn't help" even after FIFO eviction | Lesson from iter 4 was evicted by iter 14; optimizer re-tried verbose prompts at iters 16-20 |

The goal is not to make memory smarter — V2 already did that. The goal is to make
the information that reaches the reflection LLM **specific enough to act on** and
to **structurally prevent strategy collapse** when the LLM can't self-correct.
