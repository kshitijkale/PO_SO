# Plan: Making GEPA's Optimization Process Fully Observable

## Problem

GEPA has a rich callback system (26 event types) and experiment tracker integration, but for a
researcher who wants to *watch* and *reconstruct* what's happening during optimization, critical
information is either not captured, buried in freeform logs, or only accessible via external tools
(W&B/MLflow). The optimization process must be completely transparent: every iteration, every
intermediate state, every memory operation, every LLM call, every decision — all logged, all
recoverable.

## Current State

**What exists:**
- 26 callback event types (TypedDicts) fired synchronously during optimization
- Experiment tracker (W&B/MLflow) logging ~20 numeric metrics per iteration
- `StdOutLogger` printing freeform strings
- `full_program_trace` list in GEPAState (free-form dicts, only accessible post-hoc)
- `GEPAResult` returned at end with candidates, scores, lineage
- `ReflectionMemory` — rolling episodic memory (max 10 entries) storing past reflection attempts,
  injected into reflection prompts. Currently operates as a black box: entries are added and
  consumed but never logged externally.

**What's missing (the gaps this plan addresses):**

| Gap | Impact |
|-----|--------|
| LLM reflection prompts and responses are not logged | Can't see *why* the LLM proposed what it did |
| No before/after candidate text diffs | Can't see *what changed* between iterations |
| No structured per-iteration summary | Hard to follow the optimization narrative |
| Reflective dataset (failure analysis) not surfaced to files | Can't see what failures drove the mutation |
| Pareto front evolution not tracked over time | Can't see how the frontier grows |
| No local file-based structured output | Must use W&B/MLflow or parse freeform logs |
| Acceptance/rejection reasoning not detailed | Can't understand selection pressure |
| No timeline of candidate lineage | Can't trace how a good candidate evolved |
| **ReflectionMemory is a black box** | Can't see what memory was injected into which prompt, what was evicted, or how memory influenced the LLM's proposal |
| **No memory state snapshots** | Can't reconstruct what the LLM "knew" about past attempts at any given iteration |
| **No memory effectiveness analysis** | Can't measure whether memory actually helps (does injecting history of rejected strategies prevent the LLM from repeating them?) |
| **Intermediate state between events not captured** | Can't see the full data flow within a single iteration step-by-step |

---

## Plan

### Phase 1: Exhaustive Callback Logger (`ResearchLogger`)

**What:** A `GEPACallback` implementation that writes structured, human-readable iteration reports
AND machine-readable JSON logs to a run directory. Logs *everything* — every event, every
intermediate state, every data structure. Zero external dependencies.

**File:** `src/gepa/callbacks/research_logger.py`

**What it captures per iteration (one markdown file per iteration: `iteration_NNN.md`):**

1. **Iteration header** — iteration number, wall-clock timestamp, elapsed time since start, metric
   budget used/remaining/total
2. **Full state snapshot** — number of candidates in population, current best score, Pareto front
   size, coverage percentage
3. **Candidate selection** — which candidate index was selected, its full text (all components), its
   per-example val scores, its aggregate score, selection strategy used
4. **Minibatch details** — which example IDs were sampled, total trainset size, sampling strategy
5. **Current candidate evaluation (pre-mutation):**
   - Per-example scores on the minibatch (table: example_id | score | pass/fail)
   - Which examples have trajectories
   - Raw outputs per example
   - Objective scores breakdown (if multi-objective)
   - Whether any results came from cache
6. **Skip decision** (if applicable) — reason (no trajectories, all perfect), cached scores used
7. **Reflective dataset construction:**
   - Which components are being updated and why
   - For each component, the full reflective dataset: every record with all fields (Input, Output,
     Expected, Feedback, any custom fields)
   - Number of failed vs passed examples feeding into reflection
8. **Memory state BEFORE proposal** (Phase 2 — see below for full detail):
   - Full `ReflectionMemory` snapshot: every entry currently in memory
   - Which entries are relevant to each component being updated
   - The exact `format_for_prompt()` output that will be injected
   - Entries that were evicted since last iteration
9. **LLM proposal (Phase 3 — prompt capture):**
   - The complete prompt template (base + memory injection)
   - The full system prompt sent to the reflection LLM
   - The full user prompt (with reflective dataset rendered)
   - The raw LLM response text
   - The extracted new instruction text
   - Token usage (prompt tokens, completion tokens, total)
   - LLM model identifier
   - Latency of the LLM call
10. **Before/after diff** — unified diff of old vs new candidate text for each component, plus
    `summarize_change()` one-liner
11. **New candidate evaluation (post-mutation):**
    - Same detail as step 5 but for the proposed candidate
    - Per-example score comparison table: example_id | old_score | new_score | delta
12. **Acceptance decision:**
    - Old aggregate score vs new aggregate score
    - Accepted or rejected
    - Rejection reason (if rejected)
    - Score delta and percentage improvement
13. **Memory state AFTER decision** (Phase 2):
    - The `ReflectionMemoryEntry` that was just added (all fields)
    - Full memory snapshot after addition
    - Whether any entry was evicted (and which one)
    - Memory utilization (entries used / max_entries)
14. **Pareto front snapshot:**
    - Current front members (candidate indices)
    - Per-member aggregate scores
    - Newly added members (if any)
    - Displaced members (if any)
    - Per-example Pareto front mapping (which candidate is best for each example)
15. **Merge attempt** (if any):
    - Which parents were selected and why
    - Parent scores and component texts
    - Merged candidate text
    - Merge evaluation scores
    - Accepted/rejected and reason
16. **Validation set evaluation** (if triggered):
    - Full per-example scores table
    - Coverage stats
    - Whether this is a new best
    - Outputs per example (if tracked)
17. **Budget update:**
    - Metric calls used this iteration
    - Cumulative metric calls
    - Remaining budget
18. **State save event** — run directory path, what was persisted

**Machine-readable outputs (written continuously, not just at end):**

- `log.jsonl` — **one JSON line per event** (not per iteration). Every callback event is serialized
  as `{timestamp, event_type, iteration, ...event_data}`. This is the complete event stream — a
  researcher can reconstruct the entire optimization from this file alone.
- `summary.jsonl` — one JSON line per iteration with aggregated metrics:
  `{iteration, timestamp, best_score, pareto_size, acceptance_rate, budget_used, memory_size,
  memory_utilization, components_updated, accepted}`
- `candidates/candidate_NNN.json` — full text of every candidate that enters the population, plus
  metadata: `{index, parent_indices, iteration_created, operation, component_texts, aggregate_score,
  per_example_scores}`
- `pareto_timeline.jsonl` — Pareto front membership after each iteration:
  `{iteration, front_members, front_scores, displaced, new_members}`
- `memory/` — dedicated directory for memory state (see Phase 2)

**Implementation approach:**
- Implements the existing `GEPACallback` protocol — no changes to core engine needed (except for
  new memory events in Phase 2)
- Each `on_*` method: (1) appends to `log.jsonl` immediately, (2) accumulates data into a
  per-iteration buffer
- `on_iteration_end` flushes the iteration buffer to the markdown file and `summary.jsonl` line
- All writes are append-only and flushed immediately (crash-safe)

---

### Phase 2: Full Memory Observability

**What:** Make every aspect of `ReflectionMemory` observable — every read, every write, every
eviction, every prompt injection. A researcher must be able to reconstruct what the LLM "knew" about
past optimization history at any point during the run.

#### 2a. New Callback Events for Memory

**File modified:** `src/gepa/core/callbacks.py`

Add three new event types:

```python
class MemoryEntryAddedEvent(TypedDict):
    """Fired when a ReflectionMemoryEntry is added to memory."""
    iteration: int
    component_name: str
    entry: dict  # Serialized ReflectionMemoryEntry (all fields)
    memory_size_before: int
    memory_size_after: int
    evicted_entry: dict | None  # The entry that was evicted, or None
    memory_utilization: float  # entries / max_entries

class MemoryQueriedEvent(TypedDict):
    """Fired when memory is queried during prompt construction."""
    iteration: int
    component_name: str
    query_n: int  # how many entries were requested
    entries_returned: list[dict]  # serialized entries returned
    formatted_text: str  # the exact text injected into the prompt
    formatted_text_length: int  # character count

class MemoryStateSnapshotEvent(TypedDict):
    """Fired at the start/end of each iteration with full memory state."""
    iteration: int
    phase: str  # "iteration_start" or "iteration_end"
    all_entries: list[dict]  # every entry currently in memory
    total_entries: int
    max_entries: int
    entries_by_component: dict[str, int]  # count per component
    accepted_ratio: float  # fraction of entries that are accepted
    rejected_ratio: float  # fraction that are rejected
```

Add corresponding methods to `GEPACallback` protocol and `CompositeCallback`.

#### 2b. Instrument `ReflectionMemory`

**File modified:** `src/gepa/proposer/reflective_mutation/memory.py`

Add optional callback support to `ReflectionMemory`:

- `ReflectionMemory.__init__` gains `callbacks: list[GEPACallback] | None = None` and
  `current_iteration: int = 0`
- `ReflectionMemory.add()` fires `MemoryEntryAddedEvent` after adding, including the evicted entry
  if capacity was exceeded
- `ReflectionMemory.format_for_prompt()` fires `MemoryQueriedEvent` with the exact text that will
  be injected
- Add `ReflectionMemory.snapshot()` method that returns a serializable dict of full state
- Add `ReflectionMemory.set_iteration(i)` for the proposer to call at iteration start

#### 2c. Instrument `ReflectiveMutationProposer` Memory Integration

**File modified:** `src/gepa/proposer/reflective_mutation/reflective_mutation.py`

At the points where memory is used:

1. **Before `_propose_new_texts()`**: Fire `MemoryStateSnapshotEvent` with phase="before_proposal"
2. **Inside `_propose_new_texts()`** (where `format_for_prompt` is called): the event fires
   automatically from the instrumented `ReflectionMemory`
3. **After adding entry to memory** (in `__call__` after acceptance/rejection): the event fires
   automatically from the instrumented `ReflectionMemory.add()`
4. **After all memory updates**: Fire `MemoryStateSnapshotEvent` with phase="after_proposal"

#### 2d. Memory Log Files

**Written by `ResearchLogger`:**

- `memory/memory_state_iter_NNN.json` — full memory snapshot at each iteration:
  ```json
  {
    "iteration": 5,
    "max_entries": 10,
    "entries": [
      {
        "iteration": 1,
        "component_name": "system_prompt",
        "change_summary": "Added: 'Think step by step'",
        "score_before": 2.0,
        "score_after": 3.0,
        "accepted": true,
        "failure_modes": ["Model gave direct answer"]
      },
      ...
    ],
    "stats": {
      "total_entries": 5,
      "accepted_count": 3,
      "rejected_count": 2,
      "components": {"system_prompt": 3, "user_prompt": 2},
      "avg_score_delta_accepted": 0.8,
      "avg_score_delta_rejected": -0.3
    }
  }
  ```

- `memory/memory_events.jsonl` — every memory operation as a JSON line:
  ```json
  {"timestamp": "...", "event": "add", "iteration": 5, "component": "system_prompt", "accepted": true, "score_delta": 1.0, "evicted": null, "size_after": 5}
  {"timestamp": "...", "event": "query", "iteration": 6, "component": "system_prompt", "entries_returned": 3, "formatted_length": 450}
  ```

- `memory/prompts_with_memory/iter_NNN_component_NAME.txt` — the exact prompt template with memory
  text injected, for each component at each iteration. This is the literal string sent to the LLM.
  Researchers can diff these across iterations to see how memory shapes the prompt over time.

#### 2e. Memory Section in Iteration Markdown Reports

Each `iteration_NNN.md` includes a dedicated `## Reflection Memory` section:

```markdown
## Reflection Memory

### Memory State (5/10 entries, 50% utilization)

| # | Iter | Component | Change | Score | Status |
|---|------|-----------|--------|-------|--------|
| 1 | 1 | system_prompt | Added: 'Think step by step' | 2.00 → 3.00 | ACCEPTED |
| 2 | 2 | system_prompt | Changed: 'step by step' → 'chain of thought' | 3.00 → 2.50 | REJECTED |
| 3 | 3 | user_prompt | Added: 'Format as JSON' | 2.00 → 3.50 | ACCEPTED |
| 4 | 4 | system_prompt | Added: 'Be precise' | 3.00 → 3.80 | ACCEPTED |
| 5 | 5 | user_prompt | Removed: 'JSON format' | 3.50 → 2.00 | REJECTED |

### Memory Injected into Prompt for `system_prompt`

Entries used: #1, #2, #4 (3 of 5 matched component filter)

> ## Optimization History
>
> Iter 1: Added: 'Think step by step'. Score: 2.00 → 3.00 (ACCEPTED)
>   Failures addressed: "Model gave direct answer"
>
> Iter 2: Changed: 'step by step' → 'chain of thought'. Score: 3.00 → 2.50 (REJECTED)
>
> Iter 4: Added: 'Be precise'. Score: 3.00 → 3.80 (ACCEPTED)
>   Failures addressed: "Vague output"
>
> IMPORTANT: Do not repeat strategies that were REJECTED. Build on strategies that were ACCEPTED.

### Memory Update This Iteration

- Added entry: iter=6, component=system_prompt, change="Added: 'Use examples'",
  score: 3.80 → 4.20, ACCEPTED, failures=["Missed edge case"]
- No eviction (6/10 capacity)
```

---

### Phase 3: Capture LLM Reflection Prompts and Responses

**What:** The `ReflectiveMutationProposer` calls `InstructionProposalSignature.run()` which
internally calls the LLM, but the actual prompt sent and response received are not exposed. We need
to surface them completely.

**Files modified:**
- `src/gepa/strategies/instruction_proposal.py` — add option to return the full prompt/response
  alongside the extracted instruction
- `src/gepa/proposer/reflective_mutation/reflective_mutation.py` — capture and emit via callbacks

**New callback event:**

```python
class ProposalTraceEvent(TypedDict):
    """Fired with the complete LLM interaction for a proposal."""
    iteration: int
    component_name: str
    prompt_template: str  # The template (base + memory if injected)
    system_prompt: str  # What was sent as system prompt
    user_prompt: str  # What was sent as user prompt (with dataset rendered)
    raw_response: str  # The full LLM response
    extracted_instruction: str  # The parsed new instruction
    model_id: str  # Which LLM model was used
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float
    memory_was_injected: bool  # Whether reflection memory was part of the prompt
```

**What `ResearchLogger` writes:**
- In `iteration_NNN.md`: full prompt and response under `## LLM Reflection Call`
- `llm_calls/iter_NNN_component_NAME.json` — complete LLM interaction record for programmatic access
- `llm_calls/iter_NNN_component_NAME_prompt.txt` — the raw prompt text (easy to read/diff)
- `llm_calls/iter_NNN_component_NAME_response.txt` — the raw response text

---

### Phase 4: Candidate Lineage Tracker

**What:** Build a structured lineage graph so researchers can trace how any candidate evolved from
the seed through successive mutations and merges.

**File:** `src/gepa/callbacks/lineage_tracker.py`

**What it captures:**

For every candidate that enters the population:
- `candidate_idx` — unique index
- `parent_idxs` — list of parent indices (1 for mutation, 2 for merge)
- `iteration` — when it was created
- `operation` — "seed", "mutation", or "merge"
- `components_changed` — which component keys were modified
- `score_before` / `score_after` — aggregate scores
- `per_example_score_deltas` — per-example score changes vs parent
- `change_summaries` — `summarize_change()` output per component

**What it produces:**
- `lineage.jsonl` — one JSON line per candidate with all the above
- `lineage_tree.md` — human-readable tree showing the evolution path of the best candidate(s):
  ```
  #0 (seed, score=0.40)
  └── #1 (mutation iter 1, score=0.52, Δ=+0.12) [system_prompt: Added 'Think step by step']
      └── #4 (mutation iter 4, score=0.65, Δ=+0.13) [system_prompt: Added 'Use examples']
          ├── #7 (mutation iter 7, score=0.72, Δ=+0.07) [user_prompt: Changed format]
          │   └── #8 (mutation iter 9, score=0.85, Δ=+0.13) [system_prompt: Added precision] ★ BEST
          └── #5 (merge iter 5, score=0.68, Δ=+0.03) [merged #4 + #3]
  ```
- `lineage_graph.json` — machine-readable adjacency list for visualization tools

**Implementation:**
- `GEPACallback` that listens to `CandidateAcceptedEvent`, `MergeAcceptedEvent`,
  `ParetoFrontUpdatedEvent`, `ValsetEvaluatedEvent`
- At `on_optimization_end`, writes the tree and graph files

---

### Phase 5: Complete Intermediate State Logging

**What:** Beyond callback events, log every intermediate data structure at every step within an
iteration. The goal: a researcher reading the logs can reconstruct the exact data flow as if they
had a debugger attached.

**File:** `src/gepa/callbacks/state_logger.py`

**What it logs (all written to `states/` directory):**

1. **`states/iter_NNN_01_selection.json`** — candidate pool at selection time:
   - All candidate indices, their aggregate scores, Pareto membership
   - Selection probabilities (if applicable)
   - Which candidate was selected and why

2. **`states/iter_NNN_02_minibatch.json`** — the sampled minibatch:
   - Example IDs, their data (inputs, expected outputs)
   - Sampling epoch state (which examples have been seen, which haven't)

3. **`states/iter_NNN_03_eval_current.json`** — full evaluation result of current candidate:
   - Per-example: input, output, expected, score, trajectory (if captured)
   - Aggregate: sum, mean, per-objective breakdowns
   - Cache hits vs fresh evaluations

4. **`states/iter_NNN_04_reflective_dataset.json`** — the dataset sent to the reflection LLM:
   - Per-component: the list of records with all fields
   - Which examples were included, which were excluded and why

5. **`states/iter_NNN_05_memory_snapshot.json`** — full memory state (same as Phase 2d)

6. **`states/iter_NNN_06_proposal.json`** — the LLM proposal (same as Phase 3)

7. **`states/iter_NNN_07_eval_proposed.json`** — evaluation of proposed candidate (same format as 03)

8. **`states/iter_NNN_08_decision.json`** — acceptance/rejection:
   - Score comparison, decision, reason
   - State changes: new candidate index, parent links, Pareto front changes

9. **`states/iter_NNN_09_pareto.json`** — full Pareto front state:
   - Per-example front mapping (example_id → set of best candidate indices)
   - Per-objective front mapping
   - Aggregate front scores
   - Cartesian front (if applicable)

10. **`states/iter_NNN_10_merge.json`** (if merge attempted) — merge details

11. **`states/iter_NNN_11_valset.json`** (if valset eval triggered) — full val scores

12. **`states/iter_NNN_12_budget.json`** — budget state after iteration

**Implementation:**
- A separate `GEPACallback` (`StateLogger`) that writes JSON files
- Each file is a complete, self-contained snapshot — no need to read previous files
- Files are numbered in execution order within the iteration for easy sequential reading

---

### Phase 6: Per-Iteration Score Dashboard (Terminal)

**What:** A rich terminal display that shows a live-updating summary during optimization.

**File:** `src/gepa/callbacks/live_display.py`

**What it shows (refreshed each iteration):**

```
═══════════════════════════════════════════════════════════════════════
 GEPA Optimization  [iter 12/50]  [budget: 340/1000 evals]  [03m 22s]
═══════════════════════════════════════════════════════════════════════
 Best: 0.847 (#8)  |  Pareto: 5 candidates  |  Coverage: 78.5%
 Last: ACCEPTED mutation on [system_prompt] (0.72 → 0.85, +18.1%)

 Score: 0.40 0.52 0.52 0.65 0.65 0.65 0.72 0.72 0.85 0.85 0.85 0.85
         ^    ^              ^              ^         ^
       seed  #1            #4             #7        #8

 Memory: 6/10 entries | 4 accepted, 2 rejected | last eviction: iter 8
 Accept rate: 5/12 (41.7%) | Avg improvement: +0.11
═══════════════════════════════════════════════════════════════════════
```

**Implementation:**
- Uses only stdlib (`sys.stdout.write` with ANSI codes, or optional `rich` if installed)
- Implements `GEPACallback` — updates on each relevant event
- Opt-in via `display_live_dashboard=True` in `optimize()` / `optimize_anything()`

---

### Phase 7: Post-Run HTML Report

**What:** At the end of optimization, generate a single self-contained HTML file that lets the
researcher browse the full optimization history interactively.

**File:** `src/gepa/callbacks/report_generator.py`

**What the report contains:**
- Score progression chart (inline SVG — no JS dependencies)
- Pareto front evolution (step through iterations)
- Candidate browser — click any candidate to see its full text, diff from parent, per-example scores
- Memory timeline — visual timeline of memory entries, showing accepted/rejected, evictions
- Memory effectiveness analysis — did injecting memory of rejected strategies prevent repeats?
- Failure pattern summary — most common failure modes across iterations
- Lineage tree visualization
- Iteration timeline — expandable sections for each iteration's full details
- LLM prompt/response viewer per iteration

**Implementation:**
- Generates from all the data accumulated by Phases 1-5
- Single HTML file with embedded CSS, inline SVG charts
- No external JS/CSS dependencies — works offline
- Called from `on_optimization_end` or as standalone `generate_report(run_dir)` function

---

## Integration Points

### How researchers opt in

**Option A — Single flag (recommended):**
```python
result = gepa.optimize(
    ...,
    research_mode=True,    # Enables ALL observability (Phases 1-7)
    run_dir="./runs/exp1", # Where to write reports
)
```

This internally registers all callbacks: `ResearchLogger`, `StateLogger`, `LineageTracker`,
`LiveDisplay`, and `ReportGenerator`. It also enables `ReflectionMemory` with callbacks wired in.

**Option B — Granular control:**
```python
from gepa.callbacks import ResearchLogger, StateLogger, LineageTracker, LiveDisplay, ReportGenerator

result = gepa.optimize(
    ...,
    callbacks=[
        ResearchLogger(output_dir="./runs/exp1"),
        StateLogger(output_dir="./runs/exp1"),
        LineageTracker(output_dir="./runs/exp1"),
        LiveDisplay(),
        ReportGenerator(output_dir="./runs/exp1"),
    ],
)
```

Same applies to `optimize_anything()`.

### Complete run directory structure

```
runs/exp1/
├── log.jsonl                              # Complete event stream (every event, every field)
├── summary.jsonl                          # One line per iteration (key metrics)
├── pareto_timeline.jsonl                  # Pareto front after each iteration
├── lineage.jsonl                          # Candidate ancestry records
├── lineage_tree.md                        # Human-readable lineage tree
├── lineage_graph.json                     # Machine-readable adjacency list
├── report.html                            # Interactive post-run report
│
├── iterations/                            # Human-readable per-iteration narratives
│   ├── iteration_001.md
│   ├── iteration_002.md
│   └── ...
│
├── candidates/                            # Every candidate that enters the population
│   ├── candidate_000.json                 # Seed
│   ├── candidate_001.json
│   └── ...
│
├── memory/                                # Reflection memory observability
│   ├── memory_events.jsonl                # Every add/query/eviction event
│   ├── memory_state_iter_001.json         # Full snapshot per iteration
│   ├── memory_state_iter_002.json
│   └── prompts_with_memory/               # Exact prompts with memory injected
│       ├── iter_001_system_prompt.txt
│       ├── iter_002_system_prompt.txt
│       └── ...
│
├── llm_calls/                             # Complete LLM interaction records
│   ├── iter_001_system_prompt.json        # Full metadata
│   ├── iter_001_system_prompt_prompt.txt  # Raw prompt (easy to diff)
│   ├── iter_001_system_prompt_response.txt
│   └── ...
│
└── states/                                # Complete intermediate state snapshots
    ├── iter_001_01_selection.json
    ├── iter_001_02_minibatch.json
    ├── iter_001_03_eval_current.json
    ├── iter_001_04_reflective_dataset.json
    ├── iter_001_05_memory_snapshot.json
    ├── iter_001_06_proposal.json
    ├── iter_001_07_eval_proposed.json
    ├── iter_001_08_decision.json
    ├── iter_001_09_pareto.json
    ├── iter_001_10_merge.json
    ├── iter_001_11_valset.json
    ├── iter_001_12_budget.json
    └── ...
```

---

## Implementation Order and Rationale

| Phase | What | Effort | Value | Dependencies |
|-------|------|--------|-------|-------------|
| 1 | ResearchLogger (exhaustive callback logger) | Medium | Very High | None — uses existing callback protocol |
| 2 | Memory observability (events + instrumentation) | Medium | Very High | New callback events in callbacks.py; instrument memory.py |
| 3 | LLM prompt/response capture | Small | High | Small change to InstructionProposalSignature |
| 4 | Lineage tracker | Small | Medium | None — uses existing callback protocol |
| 5 | Intermediate state logger | Medium | High | None — uses existing callback protocol |
| 6 | Live terminal display | Medium | Medium | None — standalone callback |
| 7 | HTML report | Large | High | Phases 1-5 (uses their output files) |

**Recommended order:** Phases 1 + 2 + 3 together (they're tightly coupled — memory and LLM data
feed into the iteration reports). Then Phase 4 + 5. Then Phase 6. Phase 7 last (it consumes
everything).

---

## Design Principles

1. **Log everything** — when in doubt, log it. Disk is cheap; missing data during analysis is
   expensive. Every intermediate state, every decision, every LLM interaction.
2. **Dual format** — every piece of data is available in both human-readable (markdown) and
   machine-readable (JSON/JSONL) formats. Researchers can read the markdown narratives or load the
   JSONL into pandas.
3. **Self-contained snapshots** — each JSON file is complete and self-contained. You never need to
   read file N-1 to understand file N. Any single iteration's files tell the full story.
4. **Crash-safe** — all writes are append-only and flushed immediately. If the process crashes
   mid-run, everything up to the last completed event is preserved.
5. **Zero mandatory dependencies** — all output is plain files (markdown, JSON, JSONL, HTML). No
   W&B, MLflow, or any external service required.
6. **Callback-only where possible** — Phases 1, 4, 5, 6, 7 require no core engine changes.
   Phase 2 adds new callback events and instruments `ReflectionMemory`. Phase 3 modifies
   `InstructionProposalSignature` to expose prompt/response.
7. **Opt-in** — none of this activates unless `research_mode=True` or callbacks are explicitly
   registered. Default behavior unchanged. Zero overhead when not enabled.
8. **Reconstructible** — a researcher can reconstruct the *exact* optimization run from the log
   files alone, including what the LLM saw, what it proposed, what was accepted, and how memory
   evolved over time.
