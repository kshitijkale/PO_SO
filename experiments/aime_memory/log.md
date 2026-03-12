# Reflection Memory v1 — Implementation Log

## Step 1: Create memory data structure

**File**: `src/gepa/proposer/reflective_mutation/memory.py` (NEW)

Created `ReflectionMemoryEntry` dataclass and `ReflectionMemory` class.

- `ReflectionMemoryEntry` stores: iteration, component_name, change_summary, score_before, score_after, accepted, failure_modes
- `ReflectionMemory` has: entries list, max_entries (default 10), FIFO eviction
- `add()` appends and evicts oldest if over capacity
- `get_recent(n, component_name)` filters by component and returns last n
- `format_for_prompt(component_name)` renders filtered entries as a text block for the reflection prompt
- `summarize_change(old_text, new_text)` uses difflib.SequenceMatcher to produce a one-sentence description of the mutation

## Step 2: Modify ReflectiveMutationProposer

**File**: `src/gepa/proposer/reflective_mutation/reflective_mutation.py` (MODIFIED)

Three changes:

1. **Constructor** — added `reflection_memory: ReflectionMemory | None = None` parameter, stored as `self.reflection_memory`
2. **`propose_new_texts()`** — before calling `InstructionProposalSignature.run()`, if memory is enabled and has entries for the current component, appends the rendered memory text to the prompt template. If memory is empty or None, prompt is identical to baseline.
3. **`propose()`** — after evaluating the new candidate on the minibatch (after line 374), records a `ReflectionMemoryEntry` for each updated component. Extracts failure_modes from the reflective dataset by filtering for examples where score < perfect_score. Records entry regardless of acceptance (both accepted and rejected outcomes are useful).

## Step 3: Wire memory into public APIs

### `src/gepa/api.py` (MODIFIED)

- Added import: `from gepa.proposer.reflective_mutation.memory import ReflectionMemory`
- Added two parameters to `optimize()`: `use_reflection_memory: bool = False`, `reflection_memory_max_entries: int = 10`
- If `use_reflection_memory=True`, auto-creates `ReflectionMemory(max_entries=reflection_memory_max_entries)` and passes to `ReflectiveMutationProposer`

### `src/gepa/optimize_anything.py` (MODIFIED)

- Added import: `from gepa.proposer.reflective_mutation.memory import ReflectionMemory`
- Added two fields to `ReflectionConfig` dataclass: `use_reflection_memory: bool = False`, `reflection_memory_max_entries: int = 10`
- In the proposer construction block (~line 1405), creates `ReflectionMemory` if enabled and passes to `ReflectiveMutationProposer`

## Step 4: Write unit tests

**File**: `tests/test_reflection_memory.py` (NEW)

17 tests covering:

- `TestReflectionMemoryEntry`: construction, failure_modes storage
- `TestReflectionMemory`: add/retrieve, FIFO eviction (add 8 to capacity-5 → oldest 3 evicted), get_recent with/without component filter, format_for_prompt (empty, no-match, content rendering, failure mode limit of 2)
- `TestSummarizeChange`: no-change, insertion, deletion, replacement, truncation, empty↔content

All 17 tests pass.

## Step 5: Integration test

Ran full test suite (`uv run pytest -x -q`): **345 passed, 1 skipped, 0 failures**. Existing behavior is unaffected — memory is opt-in and defaults to `False`.
