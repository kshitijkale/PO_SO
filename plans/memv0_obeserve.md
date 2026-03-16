# MemV0 Research Observability Plan

## Context

MemV0 (OutcomeInterpreter + MemoryTree + TieredMemoryRenderer) runs all its LLM calls and tree mutations entirely internally — no events fire, no output appears. Researchers cannot see OI inputs/outputs, memory tree state, rendered memory text, or how memory is injected into the reflection LLM.

This plan adds full observability: 4 new callback event types, event firing at every LLM call and tree mutation, and a new `MemV0Observer` callback that prints everything to terminal (truncating only eval questions and model answer text) and logs everything to file without truncation.

---

## Step 1 — New Event Types (`src/gepa/core/callbacks.py`)

Add 4 new TypedDicts after the existing `LessonGeneratedEvent` block (~line 335):

```python
class OutcomeInterpreterCallEvent(TypedDict):
    type: Literal["outcome_interpreter_call"]
    iteration: int
    node_id: int                         # -1 if not applicable
    candidate: dict[str, str]
    oi_prompt: str                        # full formatted OI prompt
    oi_raw_response: str                  # raw LLM response ("" if fallback)
    outcomes: list[dict[str, Any]]        # serialized OutcomeDescription list
    num_records: int
    fallback_used: bool

class OIEvictionSummaryEvent(TypedDict):
    type: Literal["oi_eviction_summary"]
    iteration: int
    node_id: int
    summary_type: str                     # "node" | "global"
    prompt: str
    response: str
    existing_summary: str
    evicted_count: int
    new_summary: str

class MemoryTreeUpdatedEvent(TypedDict):
    type: Literal["memory_tree_updated"]
    iteration: int
    operation: str     # "add_root"|"add_disconnected"|"add_child"|"add_outcomes"|"set_val_score"|"eviction"
    node_id: int
    parent_id: int | None
    accepted: bool | None              # None for non-edge operations
    rejection_reason: str
    prompt: dict[str, str]
    outcomes_added: list[dict[str, Any]]   # populated for "add_outcomes"
    val_score: float | None                # populated for "set_val_score"
    evicted_count: int                     # populated for "eviction"
    new_node_summary: str                  # populated for "eviction"
    tree_node_count: int

class MemoryRenderedEvent(TypedDict):
    type: Literal["memory_rendered"]
    iteration: int
    current_node_id: int
    rendered_text: str
    char_count: int
    was_injected: bool
```

Add 4 optional handler methods to the `GEPACallback` protocol (after `on_lesson_generated`):
```python
def on_outcome_interpreter_call(self, event: OutcomeInterpreterCallEvent) -> None: ...
def on_oi_eviction_summary(self, event: OIEvictionSummaryEvent) -> None: ...
def on_memory_tree_updated(self, event: MemoryTreeUpdatedEvent) -> None: ...
def on_memory_rendered(self, event: MemoryRenderedEvent) -> None: ...
```

Check lines 652-676 for how `notify_callbacks` dispatches to method names — add entries for the 4 new method names in whatever dispatch mapping exists.

---

## Step 2 — OutcomeInterpreter Fires Its Own Events (`outcome_interpreter.py`)

**`__init__`**: add `callbacks: Any = None` parameter, store as `self.callbacks = callbacks or []`.

**`interpret()`**: add `node_id: int = -1` parameter. Capture `prompt` (before LLM call) and `raw_response` as local variables. After `_parse_response()` or `_fallbacks()`, call `notify_callbacks` with `OutcomeInterpreterCallEvent`. Use `dataclasses.asdict(o)` to serialize each `OutcomeDescription`.

**`_summarize()`**: add `*, node_id: int = -1, iteration: int = -1, summary_type: str = "unknown"` kwargs. After LLM call (and after failure path), fire `OIEvictionSummaryEvent`. On LLM failure, `response=""` and `new_summary=existing_summary`.

**`summarize_for_eviction()` / `summarize_for_global()`**: add `*, node_id: int = -1, iteration: int = -1` kwargs and thread them through to `_summarize()` with the appropriate `summary_type`.

Import: `from gepa.core.callbacks import notify_callbacks, OutcomeInterpreterCallEvent, OIEvictionSummaryEvent` at top of file (no circular import risk since callbacks.py has no gepa.proposer imports).

---

## Step 3 — Fire Tree and Render Events (`reflective_mutation.py`)

Import new event types alongside existing MemV0 imports.

**`_ensure_node_exists()`**: after add_root (line 155) or add_disconnected (line 158), fire `MemoryTreeUpdatedEvent(operation="add_root"/"add_disconnected", ...)`.

**`_run_oi_and_maybe_evict()`**:
- Pass `node_id=node_id` to `interpret()`
- After `add_outcomes` (line 174): fire `MemoryTreeUpdatedEvent(operation="add_outcomes", outcomes_added=[dataclasses.asdict(o) for o in outcomes], ...)`
- After eviction block (lines 177-182): fire `MemoryTreeUpdatedEvent(operation="eviction", evicted_count=len(evicted), new_node_summary=new_summary, ...)`
- Pass `node_id=node_id, iteration=iteration` to `summarize_for_eviction` and `summarize_for_global`

**`propose_new_texts()`**: after `memory_renderer.render()` (line 253), fire `MemoryRenderedEvent(rendered_text=memory_text or "", was_injected=bool(memory_text), ...)`.

**`propose()` MemV0 block**:
- After `set_val_score` (line 425): fire `MemoryTreeUpdatedEvent(operation="set_val_score", val_score=val_score, ...)`
- After `add_child` (lines 739-747): fire `MemoryTreeUpdatedEvent(operation="add_child", parent_id=curr_tree_node_id, accepted=accepted_v0, rejection_reason=rejection_reason, ...)`

---

## Step 4 — api.py: Register Observer + Wire Callbacks to OutcomeInterpreter

In the MemV0 block (lines 410-438), after `MemoryTree(...)` creation but before `OutcomeInterpreter(...)`:

```python
from gepa.callbacks.memv0_observer import MemV0Observer
active_callbacks.append(MemV0Observer(log_dir=run_dir, print_output=True))
effective_callbacks = active_callbacks if active_callbacks else None
```

Then update line 430:
```python
outcome_interpreter = OutcomeInterpreter(lm=oi_lm_callable, callbacks=effective_callbacks)
```

---

## Step 5 — New File: `src/gepa/callbacks/memv0_observer.py`

**Constructor**: `__init__(self, log_dir: str | None = None, print_output: bool = True)`. Opens `{log_dir}/memv0_trace.log` (human-readable) and `{log_dir}/memv0_trace.jsonl` (structured) if log_dir given.

**`_emit(header, body, event_dict)`**: prints header+body if print_output; writes to log file; writes event_dict to jsonl.

**`_trunc(s, n=100)`**: truncate with `…` — used ONLY for `Inputs` and `Generated Outputs` fields.

**Print rule**: truncate only `Inputs` and `Generated Outputs` (eval question text and model answer text) to 100 chars. Everything else — OI prompts, OI responses, memory text, tree state, reflection LLM prompt/response — printed in full.

**Handlers:**

`on_reflective_dataset_built`: per example show `score | Q: {Inputs[:100]}… | ans: {Generated Outputs[:100]}… | fb: {Feedback}`.

`on_outcome_interpreter_call`: print banner, full `oi_prompt`, full `oi_raw_response`, then per-outcome show question_summary (already ≤80 chars), result, error_type, full observation.

`on_oi_eviction_summary`: print full prompt + response + new_summary.

`on_memory_tree_updated`: print operation, node_id, parent_id, accepted/rejected, full prompt dict, outcomes count, val_score, tree_node_count. For eviction: include evicted_count and new_node_summary.

`on_memory_rendered`: print full `rendered_text` (or "(empty)" if was_injected=False).

`on_proposal_trace`: **log only** (VerboseDisplay already prints this). Write full `rendered_prompt`, `raw_response`, `extracted_instruction`, `latency_ms` to log/jsonl. No print output.

---

## Step 6 — Export (`src/gepa/callbacks/__init__.py`)

Add `from gepa.callbacks.memv0_observer import MemV0Observer` and `"MemV0Observer"` to `__all__`.

---

## Step 7 — Update Tests (`tests/test_outcome_interpreter.py`)

Add 3 tests in a new `TestCallbacks` class:
- `test_callbacks_fired_on_interpret`: mock callback with `on_outcome_interpreter_call`, verify called once, check `num_records==3`, `fallback_used==False`, `oi_prompt` non-empty
- `test_callbacks_fired_on_fallback`: LM raises exception, verify event fires with `fallback_used==True`
- `test_eviction_summary_callbacks`: call `summarize_for_eviction` with mock callback, verify `on_oi_eviction_summary` fires with `summary_type=="node"`

---

## Critical Files

| File | Action |
|------|--------|
| `src/gepa/core/callbacks.py` | ADD 4 TypedDicts + 4 GEPACallback methods + dispatch entries |
| `src/gepa/proposer/reflective_mutation/outcome_interpreter.py` | ADD callbacks + node_id params, fire OI events |
| `src/gepa/proposer/reflective_mutation/reflective_mutation.py` | FIRE tree/render events, pass node_id to OI calls |
| `src/gepa/api.py` | APPEND MemV0Observer to active_callbacks, PASS callbacks to OutcomeInterpreter |
| `src/gepa/callbacks/memv0_observer.py` | CREATE MemV0Observer class |
| `src/gepa/callbacks/__init__.py` | EXPORT MemV0Observer |
| `tests/test_outcome_interpreter.py` | ADD 3 callback tests |

---

## Verification

```bash
uv run pytest tests/test_outcome_interpreter.py -v
uv run pytest tests/ -v

# Smoke test — each iteration should print:
# ══ EVAL QUESTIONS, ══ OI CALL (P_A), ══ MEMORY RENDERED,
# (REFLECTOR from VerboseDisplay), ══ OI CALL (P_B), ══ MEMORY TREE add_child
uv run python -m experiments.aime_memory.run_verbose \
  --seed 0 --memory --memory-version v0 --max-calls 5 \
  --solver-lm gpt-4.1-mini --reflection-lm openai/gpt-4.1-mini
```

---

# MemV0 Implementation Plan (COMPLETED)

## Context

The V2 reflection memory (lesson-based) underperformed baseline in experiments (50% vs 53.3% on AIME test set). Root cause: the lesson LLM only receives thin feedback strings, not full reasoning traces, so it produces generic lessons. MemV0 fixes this with an Outcome Interpreter that runs at evaluation time with full context, a tree-structured memory that stores prompt lineage + OI outcomes, and a tiered renderer that shows the reflection LLM graduated-detail history based on genealogical distance.

Design docs: `plans/memv0_plan.md`, `plans/memv0_impl.md`

---

## Implementation Order

### Step 1: `src/gepa/proposer/reflective_mutation/memory_tree.py` (NEW)

Pure data structure, no LLM dependency.

**Dataclasses:**

```python
@dataclass
class OutcomeDescription:
    question_summary: str        # first ~80 chars of problem
    result: str                  # "correct" / "incorrect"
    observation: str             # 1-3 sentences from OI
    error_type: str | None       # arithmetic | logic | setup | interpretation | none
    score: float
    iteration: int

@dataclass
class MemoryTreeNode:
    node_id: int
    prompt: dict[str, str]
    parent_id: int | None
    children_ids: list[int]
    accepted: bool
    val_score: float | None
    minibatch_score: float | None
    outcomes: list[OutcomeDescription]
    outcome_summary: str
    iteration_created: int
    rejection_reason: str

@dataclass
class MemoryTreeEdge:
    parent_id: int
    child_id: int
    minibatch_ids: list[Any]     # list[DataId]
    iteration: int
```

**MemoryTree class methods:**

| Method | Returns | Notes |
|--------|---------|-------|
| `add_root(candidate, iteration)` | `int` (node_id) | Sets `root_id`, auto-increments `_next_node_id` |
| `add_child(parent_id, candidate, accepted, minibatch_score, minibatch_ids, iteration, rejection_reason)` | `int` | Appends to parent's `children_ids`, creates edge |
| `add_outcomes(node_id, outcomes)` | `None` | Extends node's outcomes list |
| `set_val_score(node_id, val_score)` | `None` | |
| `get_node(node_id)` | `MemoryTreeNode` | |
| `get_ancestry(node_id)` | `list[MemoryTreeNode]` | `[root, ..., parent, node]` — walk parent pointers, reverse |
| `get_siblings(node_id)` | `list[MemoryTreeNode]` | Other children of same parent, excludes self |
| `get_rejected_children(node_id)` | `list[MemoryTreeNode]` | Children with `accepted=False` |
| `get_branches_not_in_ancestry(node_id)` | `list[tuple[MemoryTreeNode, int, float\|None]]` | `(branch_root, depth, peak_val)` for branches outside ancestry |
| `needs_eviction(node_id)` | `bool` | `len(outcomes) > 2 * outcome_eviction_k` |
| `evict_oldest(node_id)` | `list[OutcomeDescription]` | Removes and returns oldest k outcomes |
| `update_node_summary(node_id, summary)` | `None` | |
| `update_global_summary(summary)` | `None` | |
| `to_dict()` | `dict` | JSON-serializable snapshot |

Instance vars: `nodes: dict[int, MemoryTreeNode]`, `edges: list[MemoryTreeEdge]`, `root_id: int | None`, `_next_node_id: int`, `outcome_eviction_k: int`, `global_summary: str`

---

### Step 2: `src/gepa/proposer/reflective_mutation/outcome_interpreter.py` (NEW)

Depends on Step 1 (produces `OutcomeDescription`).

**OutcomeInterpreter class:**

```python
class OutcomeInterpreter:
    def __init__(self, lm: LanguageModel): ...
    def interpret(self, candidate: dict[str, str], reflective_records: list[dict[str, Any]],
                  scores: list[float], iteration: int) -> list[OutcomeDescription]: ...
    def summarize_for_eviction(self, existing_summary: str, evicted: list[OutcomeDescription]) -> str: ...
    def summarize_for_global(self, existing_global: str, evicted: list[OutcomeDescription]) -> str: ...
```

`LanguageModel` is `Callable[[str | list[dict]], str]` from `base.py`.

**OI prompt template** — observation-focused, NOT advisory:

```
You are analyzing the results of running a system prompt on problems.
For each problem below, describe WHAT happened in the model's reasoning.
Focus on observations, not advice. Do not suggest fixes.

System prompt used:
<prompt>
{candidate_text}
</prompt>

{per-problem blocks with Inputs, Generated Outputs, Score, Feedback from reflective records}

For each problem, respond with exactly:
### Problem {i}
Result: correct/incorrect
Observation: [1-3 sentences describing what the model actually did]
Error type: arithmetic | logic | setup | interpretation | none
```

**Response parsing:** Regex split on `### Problem \d+`. Extract `Result:`, `Observation:`, `Error type:` lines. On parse failure per block, produce fallback `OutcomeDescription(observation="(OI parse failed)")`. On total LLM failure, return list of fallbacks matching input length. Never raise.

**Eviction summarization prompt:**

```
Existing summary (may be empty):
{existing_summary}

New observations to integrate:
{evicted outcomes formatted}

Produce a compressed summary (max 3 sentences) capturing recurring patterns, problem types where model succeeds/fails, and notable trends.
Output ONLY the summary text.
```

Both `summarize_for_eviction` and `summarize_for_global` use same template, different context arg. On LLM failure, return existing summary unchanged.

---

### Step 3: `src/gepa/proposer/reflective_mutation/memory_renderer.py` (NEW)

Depends on Step 1.

**TieredMemoryRenderer class:**

```python
class TieredMemoryRenderer:
    def __init__(self, ring1_char_budget=6000, ring2_char_budget=2000,
                 ring3_char_budget=2000, ring4_char_budget=1200): ...
    def render(self, tree: MemoryTree, current_node_id: int) -> str: ...
```

Assembly order in `render()`: Ring4 → Ring2 → Ring1 → Ring3, joined with `\n\n`, wrapped in `== OPTIMIZATION HISTORY ==`. Returns `""` if tree has only root with no outcomes.

**Ring details:**

| Ring | Content | Format |
|------|---------|--------|
| Ring 1 | Parent full text + diff to current + siblings (diff + status + OI outcomes) + rejected children of current (diff + OI outcomes) | Highest detail. Siblings newest-first. |
| Ring 2 | Ancestry `[root, ..., grandparent]` as diffs + val scores | `Seed (val=0.45) → "Added X" (val=0.48) → ...` |
| Ring 3 | Branches not in ancestry | `• Branch from seed (3 nodes): peaked val=0.46, last iter 4` |
| Ring 4 | `tree.global_summary` | `[Global patterns]\n{summary}` or `""` if empty |

**Diff computation:** Reuse the existing `summarize_change()` logic from `memory.py:255-301` (SequenceMatcher-based, produces `Added: '...'` / `Removed: '...'` / `Changed: '...' → '...'`). Copy as a static method `_compute_diff(old_text, new_text, max_len=120)`.

**`_format_outcomes_brief(outcomes, max_chars=200)`:** Take up to 3 most recent outcomes from a node, extract observation text, join with "; ", truncate.

**Budget enforcement:** Each ring method truncates to its char budget. If over total budget, shrink ring3 first, then ring1 siblings (keep parent + diff always), then ring2 detail.

**Edge case — first iteration (root only):** All rings return `""`, renderer returns `""`. Reflection LLM runs without memory.

---

### Step 4: Modify `src/gepa/proposer/reflective_mutation/reflective_mutation.py`

**New imports:**

```python
import hashlib, json
from gepa.proposer.reflective_mutation.memory_tree import MemoryTree, OutcomeDescription
from gepa.proposer.reflective_mutation.outcome_interpreter import OutcomeInterpreter
from gepa.proposer.reflective_mutation.memory_renderer import TieredMemoryRenderer
```

**New `__init__` params** (after existing `objective`):

```python
memory_tree: MemoryTree | None = None,
outcome_interpreter: OutcomeInterpreter | None = None,
memory_renderer: TieredMemoryRenderer | None = None,
```

**New instance state:** `self._candidate_to_node: dict[str, int] = {}`

**New helper methods** (before `propose_new_texts`):

- `_hash_candidate(candidate)` — static, `hashlib.sha256(json.dumps(sorted(candidate.items())).encode()).hexdigest()[:16]`
- `_ensure_node_exists(candidate, iteration)` — lookup hash → node_id. If not found: add_root if tree empty, else log warning + add as disconnected node (handles merge-created candidates)
- `_run_oi_and_maybe_evict(node_id, candidate, records, scores, iteration)` — calls OI.interpret, adds outcomes, checks eviction, if needed: evict → summarize_for_eviction → update_node_summary → summarize_for_global → update_global_summary
- `_build_oi_records(candidate, eval_batch)` — calls `adapter.make_reflective_dataset(candidate, eval_batch, list(candidate.keys()))`, returns records from first component. Returns `[]` with warning if empty.

**Modify `propose_new_texts()`** — add param `current_tree_node_id: int | None = None`. Memory injection block (lines 165-175) becomes:

```python
if self.memory_tree and self.memory_renderer and current_tree_node_id is not None:
    memory_text = self.memory_renderer.render(self.memory_tree, current_tree_node_id)
    if memory_text:
        base_template = effective_template or InstructionProposalSignature.default_prompt_template
        effective_template = base_template + "\n\n" + memory_text
        memory_was_injected = True
elif self.reflection_memory is not None:
    # existing V2 code unchanged
```

**Modify `propose()`** — insert after P_A evaluation (after line 306):

```python
curr_node_id: int | None = None
if self.memory_tree and self.outcome_interpreter:
    curr_node_id = self._ensure_node_exists(curr_prog, i)
    val_score = state.program_full_scores_val_set[curr_prog_id]
    self.memory_tree.set_val_score(curr_node_id, val_score)
    oi_records = self._build_oi_records(curr_prog, eval_curr)
    if oi_records:
        self._run_oi_and_maybe_evict(curr_node_id, curr_prog, oi_records, eval_curr.scores, i)
```

Pass `current_tree_node_id=curr_node_id` to `propose_new_texts()`.

**Replace P_B evaluation block** (lines 415-463) — gate on `self.memory_tree`:

```python
if self.memory_tree:
    # Direct evaluation with traces for OI
    eval_new = self.adapter.evaluate(minibatch, new_candidate, capture_traces=True)
    state.increment_evals(len(subsample_ids))
    new_scores = eval_new.scores
    outputs = eval_new.outputs
    # Manual cache update
    if state.evaluation_cache is not None:
        state.evaluation_cache.put_batch(new_candidate, subsample_ids, ...)
    # Fire EvaluationStartEvent + EvaluationEndEvent (same fields as existing)
else:
    # Existing cached_evaluate_full path (unchanged)
```

**After accept/reject** (after line 470), add P_B to tree + run OI:

```python
if self.memory_tree and self.outcome_interpreter:
    accepted = new_sum > old_sum
    rejection_reason = "" if accepted else f"scored {new_sum:.1f}/{len(new_scores)} vs {old_sum:.1f}/{len(eval_curr.scores)}"
    new_node_id = self.memory_tree.add_child(
        parent_id=curr_node_id, candidate=new_candidate, accepted=accepted,
        minibatch_score=new_sum / len(new_scores) if new_scores else None,
        minibatch_ids=list(subsample_ids), iteration=i, rejection_reason=rejection_reason,
    )
    self._candidate_to_node[self._hash_candidate(new_candidate)] = new_node_id
    # OI always runs — rejected candidates carry the most important lessons
    oi_records_new = self._build_oi_records(new_candidate, eval_new)
    if oi_records_new:
        self._run_oi_and_maybe_evict(new_node_id, new_candidate, oi_records_new, new_scores, i)
```

---

### Step 5: Modify `src/gepa/api.py`

**New params** to `optimize()` (after `objective`):

```python
memory_version: Literal["v2", "v0"] | None = None,
oi_lm: LanguageModel | str | None = None,
outcome_eviction_k: int = 10,
memory_ring1_budget: int = 6000,
memory_ring2_budget: int = 2000,
memory_ring3_budget: int = 2000,
memory_ring4_budget: int = 1200,
```

**Validation:** `memory_version="v0"` + `use_reflection_memory=True` → raise ValueError.

**Construction** (after existing reflection_memory setup ~line 393):

```python
if memory_version == "v0":
    # Lazy imports
    from gepa.proposer.reflective_mutation.memory_tree import MemoryTree
    from gepa.proposer.reflective_mutation.outcome_interpreter import OutcomeInterpreter
    from gepa.proposer.reflective_mutation.memory_renderer import TieredMemoryRenderer

    oi_lm_callable = resolve_lm(oi_lm) or reflection_lm_callable  # reuse existing pattern
    memory_tree = MemoryTree(outcome_eviction_k=outcome_eviction_k)
    outcome_interpreter = OutcomeInterpreter(lm=oi_lm_callable)
    memory_renderer = TieredMemoryRenderer(ring1_char_budget=..., ...)
    reflection_memory = None  # disable V2
```

Pass `memory_tree`, `outcome_interpreter`, `memory_renderer` to `ReflectiveMutationProposer`.

---

### Step 6: Modify experiment runners

**`experiments/aime_memory/run.py`** — add CLI flags:

```python
parser.add_argument("--memory-version", choices=["v2", "v0"], default=None)
parser.add_argument("--oi-lm", type=str, default=None)
parser.add_argument("--outcome-eviction-k", type=int, default=10)
```

Thread through to `optimize()`:
```python
use_reflection_memory=args.memory and args.memory_version != "v0",
memory_version=args.memory_version if args.memory else None,
oi_lm=args.oi_lm,
outcome_eviction_k=args.outcome_eviction_k,
```

**`experiments/aime_memory/run_light.py`** and **`run_verbose.py`** — same flags added.

---

### Step 7: Tests

**`tests/test_memory_tree.py`** — pure data structure tests, no mocks:
- add_root, add_child, get_ancestry, get_siblings, get_rejected_children
- add_outcomes accumulation, eviction trigger at 2k, evict_oldest correctness
- get_branches_not_in_ancestry, set_val_score, to_dict JSON-serializable

**`tests/test_outcome_interpreter.py`** — mock LLM callable:
- Parses well-formed 3-problem response → 3 OutcomeDescriptions
- Handles malformed response → fallback descriptions
- Handles LLM exception → fallback descriptions
- Prompt contains candidate text
- summarize_for_eviction works + handles failure

**`tests/test_memory_renderer.py`** — build trees manually:
- Empty tree → `""`
- Root only → `""`
- Ring 1: parent + diff shown, siblings listed, rejected children listed with OI outcomes
- Ring 2: ancestry chain format
- Ring 3: other branches summarized
- Ring 4: global summary shown
- _compute_diff: added/removed/changed formats

---

## Key Files Reference

| File | Action | Purpose |
|------|--------|---------|
| `src/gepa/proposer/reflective_mutation/memory_tree.py` | CREATE | Tree data structure |
| `src/gepa/proposer/reflective_mutation/outcome_interpreter.py` | CREATE | OI LLM call + eviction summarization |
| `src/gepa/proposer/reflective_mutation/memory_renderer.py` | CREATE | Tiered rendering (4 rings) |
| `src/gepa/proposer/reflective_mutation/reflective_mutation.py` | MODIFY | Wire OI, tree, renderer into propose() |
| `src/gepa/api.py` | MODIFY | Add memory_version param + MemV0 wiring |
| `experiments/aime_memory/run.py` | MODIFY | CLI flags |
| `experiments/aime_memory/run_light.py` | MODIFY | CLI flags |
| `experiments/aime_memory/run_verbose.py` | MODIFY | CLI flags |
| `src/gepa/proposer/reflective_mutation/memory.py` | REFERENCE | Reuse `summarize_change` diff logic (lines 255-301) |
| `src/gepa/proposer/reflective_mutation/base.py` | REFERENCE | `LanguageModel` protocol (line 28) |
| `tests/test_reflection_memory.py` | REFERENCE | Existing test patterns (mock LM, direct construction) |

## Verification

1. `uv run pytest tests/test_memory_tree.py tests/test_outcome_interpreter.py tests/test_memory_renderer.py -v`
2. `uv run ruff check src/gepa/proposer/reflective_mutation/memory_tree.py src/gepa/proposer/reflective_mutation/outcome_interpreter.py src/gepa/proposer/reflective_mutation/memory_renderer.py`
3. `uv run pyright src/gepa/proposer/reflective_mutation/`
4. Smoke test: `uv run python -m experiments.aime_memory.run_light --seed 0 --memory --memory-version v0` (requires API key)
