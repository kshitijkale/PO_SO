# MemV0 Implementation Plan

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
