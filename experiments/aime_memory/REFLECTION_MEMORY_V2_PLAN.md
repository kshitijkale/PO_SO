# Reflection Memory v2 — LLM-Generated Lessons Plan

## The V1 Problem

V1 uses `summarize_change(old_text, new_text)` — a `difflib`-based heuristic — to describe
each editing episode. What it actually produces:

```
Added: 'Verify all algebraic manipulations and logical deductions, and double-check
        for completeness and correctness before concluding.'
```

This describes **what changed textually**, but not:
- *Why* the edit was made (which failure mode it was targeting)
- *What the outcome reveals* (did the strategy address the right problem?)
- *What strategic insight* a future reflection LLM should take from it

The AIME light run confirmed this: 9/10 prompts had memory injected, memory was growing
correctly — but the injected text was syntactic diff noise, not actionable lessons. The
reflection LLM has to re-derive the lesson from raw text diffs and truncated feedback strings.

---

## V2 Core Idea

After each proposal is evaluated and accepted/rejected, make a **small LLM call** to generate
a structured lesson from the episode. The lesson is stored in memory instead of the heuristic
diff. Future reflection calls receive rich, pre-digested strategic insight rather than raw diffs.

**Input to the lesson LLM:**
- Old prompt text (before this iteration's edit)
- New prompt text (the proposed mutation)
- 1–3 failure feedback examples from the reflective dataset (what went wrong before the edit)
- Score before and after (`score_before → score_after`)
- Whether the proposal was accepted or rejected

**Output from the lesson LLM (structured JSON):**
```json
{
  "intent": "Strengthen verification by requiring double-checking of algebraic steps",
  "lesson": "Explicit verification reduced arithmetic errors but didn't help with
             structural setup mistakes — those require different prompting",
  "takeaway": "Target problem setup and variable definition next; arithmetic
               verification is already addressed"
}
```

---

## What the Memory Block Looks Like in V2

### V1 format (current):
```
## Optimization History

Iter 1: Added: 'Verify all algebraic manipulations...'. Score: 2.00 → 3.00 (ACCEPTED)
  Failures addressed: "Your answer is incorrect. The correct answer is 601. Here's the
  step-by-step solution: a²(b+c)+b²(a+c)+c²(a+b) = 6000000, thus a²(300-a)+b²(30..."

Iter 2: Changed: 'double-check for completeness' → 'review deductions and calculations
  to ensure accuracy'. Score: 3.00 → 3.00 (REJECTED)
  Failures addressed: "Your answer is incorrect. The correct answer is..."

IMPORTANT: Do not repeat strategies that were REJECTED. Build on strategies that were ACCEPTED.
```

### V2 format (proposed):
```
## Optimization History

### Iter 1 [ACCEPTED +1.00]
Intent: Require explicit verification of algebraic steps before committing to an answer
Lesson: Adding a verification step improved performance on problems where arithmetic
        errors were the main failure mode, but didn't help on problems requiring more
        creative problem setup
Takeaway: Arithmetic verification is addressed — next target problem decomposition
          strategy and geometric/algebraic setup for harder problems

### Iter 2 [REJECTED ±0.00]
Intent: Strengthen the verification language with more comprehensive coverage
Lesson: This is a minor restatement of the Iter 1 accepted change with no new strategy —
        the model is already verifying; more emphasis on the same idea doesn't help
Takeaway: Avoid rewording existing accepted instructions; introduce a genuinely new
          strategy like explicit modular arithmetic or case-enumeration guidance

### Iter 4 [REJECTED −1.00]
Intent: Add domain-specific guidance for combinatorics and optimization problems
Lesson: Adding problem-type-specific advice hurt performance — the model may be
        over-specializing when the minibatch contains diverse problem types
Takeaway: Broad instructions that apply to all problem types outperform narrow type-specific
          ones in this diverse setting; consider problem-agnostic reasoning strategies instead

IMPORTANT: Do not repeat REJECTED strategies. Build on ACCEPTED ones.
```

The V2 format is **actionable**: the reflection LLM reading this has a concrete, synthesized
history of what was tried, why it did/didn't work, and specifically what to try next.

---

## Implementation Plan

### What changes vs V1

| Component | V1 | V2 |
|-----------|----|----|
| `ReflectionMemoryEntry.change_summary` | heuristic diff string | removed |
| `ReflectionMemoryEntry.lesson` | (does not exist) | LLM-generated lesson |
| `ReflectionMemoryEntry.intent` | (does not exist) | LLM-generated intent |
| `ReflectionMemoryEntry.takeaway` | (does not exist) | LLM-generated takeaway |
| `ReflectionMemoryEntry.failure_modes` | raw truncated feedback strings | LLM-extracted failure type labels |
| `format_for_prompt()` | renders diff + raw feedback | renders intent + lesson + takeaway |
| `ReflectiveMutationProposer.propose()` | calls `summarize_change()` | calls `generate_lesson()` |
| New: `LessonGeneratorSignature` | (does not exist) | prompt + structured JSON output |
| New: `lesson_lm` parameter | (does not exist) | LM used for lesson generation (defaults to reflection LM) |

`summarize_change()` is kept as a **fallback** — used if lesson generation fails or is disabled.

---

### Step 1: Update `ReflectionMemoryEntry`

In `memory.py`, add three new fields and mark `change_summary` as optional (for backward
compatibility with tests):

```python
@dataclass
class ReflectionMemoryEntry:
    iteration: int
    component_name: str

    # V1: heuristic diff (kept as fallback)
    change_summary: str = ""

    # V2: LLM-generated lesson (populated when use_llm_lesson=True)
    intent: str = ""
    lesson: str = ""
    takeaway: str = ""

    score_before: float = 0.0
    score_after: float = 0.0
    accepted: bool = False
    failure_modes: list[str] = field(default_factory=list)
```

All new fields default to empty string so existing V1 tests continue to pass without
modification.

---

### Step 2: Build `LessonGeneratorSignature`

New file or addition to `base.py`. The lesson generator takes a structured input and returns
a JSON object parsed into three fields.

**Prompt template:**

```
You are analyzing one iteration of a prompt optimization loop for the task:
{objective}

## Prompt Before
{old_prompt}

## Prompt After
{new_prompt}

## Failure Examples (before this edit)
{failure_examples}

## Outcome
Minibatch score: {score_before:.2f} → {score_after:.2f} ({ACCEPTED or REJECTED})

---

Analyze this optimization step. Be specific — reference the actual failure examples and
the actual changes made. Avoid generic advice.

Respond with a JSON object:
{
  "intent": "<1 sentence: what was this edit trying to achieve?>",
  "lesson": "<1-2 sentences: what does the outcome reveal about this type of change?>",
  "takeaway": "<1 sentence: one specific, actionable directive for the next iteration>"
}
```

**Key design choices:**
- Instruction to "be specific — reference actual failure examples" prevents generic lessons
- Structured JSON output avoids parsing ambiguity
- 3 fields map cleanly to the 3 roles: *what was tried*, *what was learned*, *what to do next*
- The objective is passed in so the lesson is task-aware

**Failure modes extraction**: The lesson LLM also distills raw feedback into the `failure_modes`
list (e.g. `["arithmetic error", "wrong case setup", "misread constraint"]`) which are
surfaced more cleanly than raw truncated feedback strings.

---

### Step 3: Add `generate_lesson()` to `ReflectionMemory`

Or keep it as a standalone function in `memory.py`. Signature:

```python
def generate_lesson(
    lm: LanguageModel,
    old_text: str,
    new_text: str,
    failure_feedbacks: list[str],   # raw Feedback strings from failed examples
    score_before: float,
    score_after: float,
    accepted: bool,
    objective: str = "",
    max_feedback_examples: int = 3,  # cap to keep prompt short
) -> tuple[str, str, str]:           # (intent, lesson, takeaway)
```

Returns `("", "", "")` on any exception — the caller falls back to `summarize_change()`.

---

### Step 4: Update `format_for_prompt()`

Use the new fields when available, fall back to V1 format when they are empty:

```python
for entry in relevant:
    status = f"{'ACCEPTED' if entry.accepted else 'REJECTED'} "
    delta = entry.score_after - entry.score_before
    status += f"{delta:+.2f}"

    if entry.lesson:  # V2 path
        lines.append(f"### Iter {entry.iteration} [{status}]")
        if entry.intent:
            lines.append(f"Intent: {entry.intent}")
        lines.append(f"Lesson: {entry.lesson}")
        if entry.takeaway:
            lines.append(f"Takeaway: {entry.takeaway}")
    else:             # V1 fallback path
        lines.append(
            f"Iter {entry.iteration}: {entry.change_summary}. "
            f"Score: {entry.score_before:.2f} → {entry.score_after:.2f} ({status})"
        )
        if entry.failure_modes:
            modes_str = "; ".join(f'"{m}"' for m in entry.failure_modes[:2])
            lines.append(f"  Failures addressed: {modes_str}")

    lines.append("")
```

---

### Step 5: Update `ReflectiveMutationProposer.propose()`

After the accept/reject decision, replace the `summarize_change()` call with `generate_lesson()`.
The call happens **after** we know `accepted` — so the lesson can reason about the outcome.

```python
if self.reflection_memory is not None:
    for comp_name in predictor_names_to_update:
        # Collect failure feedbacks for this component
        failure_feedbacks = []
        if comp_name in reflective_dataset:
            for idx, record in enumerate(reflective_dataset[comp_name]):
                if idx < len(eval_curr.scores) and eval_curr.scores[idx] < (self.perfect_score or 1.0):
                    fb = record.get("Feedback") or record.get("feedback") or ""
                    if isinstance(fb, str) and fb:
                        failure_feedbacks.append(fb)

        # V2: LLM lesson (falls back to V1 diff if lm not available or call fails)
        intent, lesson, takeaway = ("", "", "")
        if self.lesson_lm is not None:  # new optional parameter
            intent, lesson, takeaway = generate_lesson(
                lm=self.lesson_lm,
                old_text=curr_prog.get(comp_name, ""),
                new_text=new_candidate.get(comp_name, ""),
                failure_feedbacks=failure_feedbacks,
                score_before=old_sum,
                score_after=new_sum,
                accepted=new_sum > old_sum,
                objective=self._objective or "",
            )

        change_summary = summarize_change(  # V1 fallback always computed
            curr_prog.get(comp_name, ""),
            new_candidate.get(comp_name, ""),
        ) if not lesson else ""

        self.reflection_memory.add(
            ReflectionMemoryEntry(
                iteration=i,
                component_name=comp_name,
                change_summary=change_summary,
                intent=intent,
                lesson=lesson,
                takeaway=takeaway,
                score_before=old_sum,
                score_after=new_sum,
                accepted=new_sum > old_sum,
                failure_modes=failure_feedbacks[:3],  # raw, capped at 3
            )
        )
```

---

### Step 6: Wire `lesson_lm` into public APIs

**`ReflectionConfig` in `optimize_anything.py`:**
```python
@dataclass
class ReflectionConfig:
    ...
    use_reflection_memory: bool = False
    reflection_memory_max_entries: int = 10
    use_llm_lesson: bool = True          # NEW: use LLM for lesson generation
    lesson_lm: LanguageModel | str | None = None  # NEW: defaults to reflection_lm if None
```

When `use_llm_lesson=True` and `use_reflection_memory=True`, the lesson LLM is set to
`lesson_lm or reflection_lm`. This means V2 is zero-config for existing users who already
have `use_reflection_memory=True` — it automatically upgrades to LLM lessons.

---

### Step 7: Observability — `LessonGeneratedEvent`

New event in `callbacks.py`:
```python
class LessonGeneratedEvent(TypedDict):
    iteration: int
    component_name: str
    intent: str
    lesson: str
    takeaway: str
    score_before: float
    score_after: float
    accepted: bool
    latency_ms: float
    fallback_used: bool   # True if LLM failed and we fell back to summarize_change()
```

`ResearchLogger` writes these to `memory/lesson_events.jsonl` and into the iteration
markdown report under a "Lesson Generated" section.

---

### Step 8: Update unit tests

In `tests/test_reflection_memory.py`, add tests for V2:

1. **Test `ReflectionMemoryEntry` with V2 fields** — verify lesson/intent/takeaway stored
2. **Test `format_for_prompt()` V2 path** — verify V2 formatting when lesson fields populated
3. **Test `format_for_prompt()` V1 fallback** — verify V1 format used when lesson is empty
4. **Test `generate_lesson()` mock** — mock the LM call, verify JSON parsed correctly
5. **Test `generate_lesson()` failure fallback** — LM raises exception, verify returns `("", "", "")`
6. **Test `ReflectionMemory.add()` with V2 entry** — verify `to_dict()` includes new fields

No new LLM call fixtures needed — the lesson LM call is mocked.

---

## Open Design Questions

### Q1: Lesson LLM call timing — before or after accept/reject decision?

**Option A — After accept/reject (proposed above):**
- Pro: lesson can reason about the outcome ("this was rejected because...")
- Pro: don't waste a lesson call if we early-exit (no trajectories, perfect scores)
- Con: adds latency to the critical path (before returning `CandidateProposal`)

**Option B — Async / deferred:**
- Generate lesson asynchronously after returning `CandidateProposal`
- Pro: zero added latency on the critical path
- Con: lesson isn't in memory until the *next* iteration's start — one iteration lag
- Con: implementation complexity

**Recommendation**: Option A. The lesson call is ~1-2s; with `parallel=True` and 8 workers
the evaluation itself takes 10-30s. A 1-2s lesson call is negligible relative to that.

---

### Q2: Should lesson generation use the same LLM as reflection?

**Option A — Same LLM (default):**
- Simpler: no new config
- The lesson input is small (~500 tokens) so cost is low even on expensive models
- The lesson and reflection use the same "mental model" of the task

**Option B — Separate cheaper LLM:**
- Lesson generation is a simpler task than reflection — might not need the full model
- Could use `gpt-4.1-nano` or a fast model for lessons to save cost

**Recommendation**: Default to same LLM as reflection (no extra config), but allow override
via `lesson_lm` parameter for cost-conscious users.

---

### Q3: What should the lesson see from the failure examples?

**Option A — Raw feedback strings (current V1 approach):**
- Already extracted from the reflective dataset
- Can be long and contain the full solution trace
- Lesson LLM has to extract the relevant signal

**Option B — Pass the full reflective dataset record (problem + output + feedback):**
- Richer context — lesson LLM sees the actual problem that failed
- But: significantly longer prompt, more tokens, more expensive
- Could hit context limits on long AIME problems

**Option C — Just the Feedback string, truncated to 200 chars per example, max 3 examples:**
- Balanced: enough context to identify the failure pattern, compact enough to be cheap
- Consistent prompt length

**Recommendation**: Option C. Cap at 3 failure examples × 200 chars = ~600 chars of failure
context. Enough for the lesson LLM to identify failure patterns without exploding the prompt.

---

### Q4: How many lesson entries to show in the memory block?

V1 shows up to 7 recent entries. V2 entries are richer (3 fields vs 1 + feedback), so the
memory block will be ~3× longer per entry.

**Options:**
- Keep max_recent=7 but each entry is longer → memory block grows to ~1000 tokens
- Reduce max_recent to 3-4 for V2 → comparable token count to V1
- Make max_recent configurable per version

**Recommendation**: Default `max_recent=5` for V2 (vs 7 in V1). This keeps the memory block
around 500-700 tokens — large enough to show clear history, small enough not to dominate the
reflection prompt.

---

### Q5: Backward compatibility strategy

V2 should be a strict superset of V1. Upgrade path:

| Config | Behavior |
|--------|----------|
| `use_reflection_memory=False` | No memory (same as today) |
| `use_reflection_memory=True, use_llm_lesson=False` | V1 behavior: heuristic diff |
| `use_reflection_memory=True, use_llm_lesson=True` | V2 behavior: LLM lessons (default when enabled) |

`use_llm_lesson=True` is the default when `use_reflection_memory=True` — so existing users
who enable memory automatically get V2 lessons. Can be opted out with `use_llm_lesson=False`.

---

### Q6: Lesson consolidation / meta-lessons (future V3 idea)

After every K iterations (e.g., K=5), run a "meta-lesson" call that synthesizes the K most
recent lessons into 3-5 high-level principles. Replace the K detailed entries with one
consolidated entry. Keeps the memory block compact as runs get longer.

**Not in V2** — complexity too high. Add to V3 plan once V2 is validated.

---

## V2 Experiment Plan

### Primary question
Does LLM-generated lesson memory produce better prompt optimization than heuristic diff
memory (V1), and both better than no memory?

**Three conditions:**
1. `memory=off` (baseline)
2. `memory=on, use_llm_lesson=False` (V1: heuristic diff)
3. `memory=on, use_llm_lesson=True` (V2: LLM lessons)

**Same controls as V1 experiment:** same LLM, same budget, same seeds (0–4).

### What success looks like
V2 > V1 > baseline on **at least one** of:
- Higher best val score at same budget
- Fewer iterations to reach same val score
- Higher proposal acceptance rate

If V2 ≈ V1 (no improvement over heuristic diff), the lesson quality may be too generic —
diagnose by reading lesson content in `memory/lesson_events.jsonl`.

### Diagnostic: lesson quality check
After each run, manually read 3-4 lessons from `lesson_events.jsonl`. Ask:
- Are lessons specific to the actual failures (not generic)?
- Do takeaways reflect genuine strategic insight?
- Do later iterations show the LLM following the takeaways?

If lessons are generic (e.g., "be more careful", "verify your work"), the lesson prompt
needs more specificity constraints.

---

## Summary of Changes from V1

| | V1 | V2 |
|--|----|----|
| **Lesson generation** | `difflib.SequenceMatcher` heuristic | LLM call: intent + lesson + takeaway |
| **Memory entry** | `change_summary` (1 str) | `intent`, `lesson`, `takeaway` (3 strs) |
| **Failure modes** | Raw truncated feedback strings | Raw feedback capped at 3 × 200 chars (same content, cleaner cap) |
| **Memory block** | ~200 tokens per entry | ~300-400 tokens per entry |
| **Max entries shown** | 7 | 5 (to keep total block size comparable) |
| **LLM calls per iter** | 1 (reflection only) | 2 (reflection + lesson) |
| **Config opt-out** | N/A | `use_llm_lesson=False` falls back to V1 |
| **Backward compat** | Full (memory opt-in) | Full (V2 opt-in via `use_llm_lesson=True`) |
