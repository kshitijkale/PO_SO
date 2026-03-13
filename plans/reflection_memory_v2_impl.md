# Reflection Memory V2 — Implementation Reference

This document describes the actual implemented state of the V2 memory feature.
All steps listed here are **complete** unless explicitly noted.

---

## Files Changed

```
src/gepa/proposer/reflective_mutation/memory.py          DONE
src/gepa/proposer/reflective_mutation/reflective_mutation.py  DONE
src/gepa/core/callbacks.py                               DONE
src/gepa/optimize_anything.py                            DONE
src/gepa/api.py                                          DONE
src/gepa/callbacks/research_logger.py                    DONE
tests/test_reflection_memory.py                          DONE
experiments/aime_memory/run_light.py                     NOT DONE (print_lesson_events step skipped)
```

---

## Step 1 — `ReflectionMemoryEntry` in `memory.py` ✅

```python
@dataclass
class ReflectionMemoryEntry:
    iteration: int
    component_name: str
    score_before: float
    score_after: float
    accepted: bool
    failure_modes: list[str]       # all per-example feedback strings (not capped)

    # V2 fields (populated by lesson LLM; default "" / [] for fallback path)
    intent: str = ""
    lesson: str = ""
    categories_succeeded: list[str] = field(default_factory=list)
    categories_failed: list[str] = field(default_factory=list)

    # V1 fallback (populated only when lesson LLM call fails)
    change_summary: str = ""
```

`to_dict()` includes all fields (V1 + V2).

---

## Step 2 — `generate_lesson()` in `memory.py` ✅

**Actual signature** (differs from original plan):

```python
def generate_lesson(
    lm: LanguageModel,
    old_text: str,
    new_text: str,
    failure_feedbacks: list[str],              # all eval context, not capped to 3×200
    score_before: float,
    score_after: float,
    accepted: bool,
    per_example_scores_before: list[float] | None = None,   # NEW vs plan
    per_example_scores_after: list[float] | None = None,    # NEW vs plan
    objective: str = "",
) -> tuple[str, str, list[str], list[str]]:
```

Returns `("", "", [], [])` on any exception (JSON parse error, LLM failure, missing keys).

**Actual prompt template** (in `_LESSON_PROMPT_TEMPLATE`):

Sections:
1. `Task objective`
2. `## Prompt Before` / `## Prompt After`
3. `## Evaluation Examples` — all evaluation context (not capped to 3 × 200 chars)
4. `## Aggregate Scores` — total before/after + delta
5. `## Per-Example Scores` — per-example before/after/delta table
6. `## Outcome`

Category labels are **open-ended** — the LLM infers them from per-example score deltas
and evaluation context. The original plan specified a fixed vocab (algebra, geometry, etc.);
the actual implementation removed this constraint.

Markdown code fences are stripped from the LLM response before JSON parsing.

---

## Step 3 — `format_for_prompt()` in `memory.py` ✅

`max_recent` default is 5 (changed from 7 in V1).

**V2 path** (when `entry.lesson` is non-empty):
```
### Iter N [ACCEPTED/REJECTED +/-delta]
Intent: ...
Lesson: ...
Strong on: cat1, cat2          (omitted if empty)
Still failing: cat3, cat4      (omitted if empty)
```

**V1 fallback path** (when `entry.lesson == ""`):
```
Iter N: <change_summary>. Score: X.XX → Y.YY (ACCEPTED/REJECTED)
  Failures addressed: "...", "..."     (up to 2 failure_modes)
```

Note: V1 fallback format includes score range inline; the original plan had it as a bare
`{entry.change_summary}` line only.

**Persistent weak spots** line: auto-generated when a category appears in `categories_failed`
across 2+ entries. Uses `collections.Counter` on all `categories_failed` in recent entries.

Fires `MemoryQueriedEvent` in both the "no entries" early-return path and the normal path.

---

## Step 4 — `LessonGeneratedEvent` in `callbacks.py` ✅

```python
class LessonGeneratedEvent(TypedDict):
    iteration: int
    component_name: str
    intent: str
    lesson: str
    categories_succeeded: list[str]
    categories_failed: list[str]
    score_before: float
    score_after: float
    accepted: bool
    latency_ms: float
    fallback_used: bool    # True when lesson LLM failed and summarize_change() was used
```

`on_lesson_generated` added to `GEPACallback` Protocol and `CompositeCallback`.

---

## Step 5 — `ReflectiveMutationProposer` in `reflective_mutation.py` ✅

### New constructor parameters

```python
def __init__(
    self,
    ...,
    reflection_memory: ReflectionMemory | None = None,
    lesson_lm: LanguageModel | None = None,
    objective: str = "",
):
```

### Feedback collection (actual behavior — differs from plan)

Plan said: cap at 3 × 200 chars, only failure examples.

Actual: collects **all** examples from the reflective dataset regardless of score:

```python
for record in reflective_dataset[comp_name]:
    feedback = str(record.get("Feedback") or record.get("feedback") or "").strip()
    if feedback:
        example_feedbacks.append(feedback)
    else:
        example_feedbacks.append(str(record))   # fallback to full record
```

No length cap. No filtering by score.

### `generate_lesson()` call

Passes `per_example_scores_before=list(eval_curr.scores)` and
`per_example_scores_after=list(new_scores)` (both available at call site).

`effective_lm = self.lesson_lm or self.reflection_lm` — falls back to reflection LM if
no dedicated lesson LM is configured.

### Memory recording block

Fires `LessonGeneratedEvent` before calling `self.reflection_memory.add()`.
Memory snapshot events fired at `before_proposal` (start of `propose()`) and
`after_proposal` (end of memory recording block).

---

## Step 6 — `ReflectionConfig` in `optimize_anything.py` ✅

```python
@dataclass
class ReflectionConfig:
    ...
    use_reflection_memory: bool = False
    reflection_memory_max_entries: int = 10
    lesson_lm: LanguageModel | str | None = None   # defaults to reflection_lm
```

Resolution logic:
```python
if config.reflection.use_reflection_memory:
    _raw = config.reflection.lesson_lm or config.reflection.reflection_lm
    resolved_lesson_lm = make_litellm_lm(_raw) if isinstance(_raw, str) else _raw
```

`lesson_lm=None` passed to proposer when `use_reflection_memory=False`.

`api.py` mirrors this: `lesson_lm: LanguageModel | str | None = None` parameter on
`optimize()`, with the same resolution logic.

---

## Step 7 — `ResearchLogger` in `research_logger.py` ✅

`on_lesson_generated` handler:
- Logs event to main `log.jsonl` via `_log_event("lesson_generated", ...)`
- Appends to `memory/lesson_events.jsonl`

**Actual JSONL record format** (differs from plan):
```json
{
  "timestamp": "...",
  "event": "lesson",
  "iteration": N,
  "component": "...",
  "intent": "...",
  "lesson": "...",
  "categories_succeeded": [...],
  "categories_failed": [...],
  "accepted": true/false,
  "latency_ms": N.N,
  "fallback_used": false
}
```

Note: key is `"component"` (not `"component_name"` as in the plan), and `"event": "lesson"`
field is added. `score_before`/`score_after` are not written to the JSONL (they are in
`log.jsonl` via `_log_event`).

---

## Step 8 — Unit tests in `test_reflection_memory.py` ✅

All 8 planned tests are implemented:
1. `test_entry_v2_fields` — V2 fields in `to_dict()`
2. `test_format_for_prompt_v2_path` — Intent/Lesson/Strong/Still failing rendering
3. `test_format_for_prompt_v1_fallback` — V1 format with `change_summary`
4. `test_format_for_prompt_5_recent_default` — max 5 entries rendered
5. `test_generate_lesson_success` — mock LM returns valid JSON
6. `test_generate_lesson_bad_json_fallback` — non-JSON → `("", "", [], [])`
7. `test_generate_lesson_exception_fallback` — exception → `("", "", [], [])`
8. `test_persistent_weak_spots` — category recurring in 2+ entries surfaces in output

---

## Step 9 — `run_light.py` verification helper ❌ NOT IMPLEMENTED

The `print_lesson_events()` function was planned but not added to `run_light.py`. The
`lesson_events.jsonl` file is still written by `ResearchLogger` and can be inspected
manually, but there is no automated reporting from `run_light.py`.

---

## Acceptance Criteria

- [x] `uv run pytest tests/test_reflection_memory.py` — all tests pass (existing + 8 new)
- [x] `uv run pytest` — full suite passes (no regressions)
- [ ] `uv run python -m experiments.aime_memory.run_light` — memory report shows
  `lesson_events.jsonl` entries (requires live run; `print_lesson_events` not yet in script)

---

## Known Deviations from Original Plan

| Area | Plan | Actual |
|------|------|--------|
| `failure_feedbacks` scope | 3 failures × 200 chars | All examples, no cap, no failure filter |
| `generate_lesson()` params | 7 params | 9 params (adds `per_example_scores_before/after`) |
| Prompt: eval context section | "Failure Examples (up to 3)" | "Evaluation Examples" + Aggregate Scores + Per-Example Scores |
| Category vocabulary | Fixed list (algebra, geometry, ...) | Open-ended, inferred by LLM |
| V1 fallback rendering | bare `change_summary` | `change_summary` + score range + status inline |
| `lesson_events.jsonl` key | `component_name` | `component` |
| `run_light.py` step | `print_lesson_events()` added | Not implemented |
