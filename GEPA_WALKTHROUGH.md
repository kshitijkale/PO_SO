# GEPA Codebase Walkthrough

## What is GEPA?

GEPA (Genetic-Pareto) is a framework for **optimizing any text parameter** — prompts, code, agent architectures, configurations — by using an LLM to *read* execution traces, diagnose why a candidate failed, and propose a better one. It is evolutionary: it maintains a population of candidates, iteratively mutates and merges them, and tracks a Pareto frontier to preserve diverse high-performing variants.

The core idea: instead of collapsing execution results into a single loss number (like RL does), GEPA captures the full trace — error messages, outputs, reasoning logs — and feeds that to a "reflection" LLM that produces actionable diagnoses. This text-level feedback is called **Actionable Side Information (ASI)** and is the gradient analogue in GEPA's optimization.

---

## High-Level Loop

```
seed_candidate
      │
      ▼
 [Select from Pareto front]
      │
      ▼
 [Sample minibatch from trainset]
      │
      ▼
 [Evaluate candidate, capture traces]
      │
      ▼
 [Build reflective dataset from traces]
      │
      ▼
 [LLM reflects → proposes improved candidate]
      │
      ▼
 [Evaluate new candidate on same minibatch]
      │
      ├── improved? → accept → full valset eval → update Pareto front
      │
      └── not improved? → reject, try again next iteration

 (also: periodically attempt merge of two Pareto-front candidates)
```

---

## Two Public APIs

### 1. `gepa.optimize()` — `src/gepa/api.py`

The high-level API for prompt/multi-component optimization. You supply a seed candidate, data, and optionally your own adapter or task LM. It wires everything up and runs the loop.

```python
result = gepa.optimize(
    seed_candidate={"system_prompt": "You are a helpful assistant."},
    trainset=trainset,
    valset=valset,
    task_lm="openai/gpt-4.1-mini",
    reflection_lm="openai/gpt-5",
    max_metric_calls=150,
)
```

**What it does internally:**
1. Validates `seed_candidate` (must be `dict[str, str]`)
2. Normalizes `trainset`/`valset` lists into `DataLoader` objects via `ensure_loader()`
3. Builds a `CompositeStopper` from e.g. `MaxMetricCallsStopper` and `FileStopper`
4. If no `adapter` is provided, creates a `DefaultAdapter` using `task_lm` and `evaluator`
5. Instantiates a `ReflectiveMutationProposer`
6. Optionally instantiates a `MergeProposer`
7. Creates `GEPAEngine` and calls `.run()`
8. Returns `GEPAResult`

---

### 2. `gepa.optimize_anything.optimize_anything()` — `src/gepa/optimize_anything.py`

A universal API for optimizing any text artifact: code, SVGs, configs, agent architectures. The difference from `optimize()` is that your artifact is a plain `str` (or `dict[str, str]`), and you provide a scoring function.

```python
import gepa.optimize_anything as oa
from gepa.optimize_anything import optimize_anything, GEPAConfig, EngineConfig

def evaluate(candidate: str) -> float:
    result = run_my_system(candidate)
    oa.log(f"Error: {result.error}")   # this becomes ASI
    return result.score

result = optimize_anything(
    seed_candidate="<initial code>",
    evaluator=evaluate,
    objective="Maximize throughput.",
    config=GEPAConfig(engine=EngineConfig(max_metric_calls=100)),
)
```

**Three modes determined by dataset/valset:**
- **Single-task search** (`dataset=None`): optimize one artifact (e.g. solve one math problem)
- **Multi-task search** (`dataset=<list>`, no `valset`): cross-task transfer
- **Generalization** (`dataset=<list>`, `valset=<list>`): learn a skill that transfers to unseen problems

**Seedless mode:** Pass `seed_candidate=None` with `objective` text; the reflection LM bootstraps the first candidate from scratch.

Internally this API builds an `OptimizeAnythingAdapter` that wraps your evaluator, then calls the same `GEPAEngine` used by `optimize()`.

---

## Core Engine — `src/gepa/core/engine.py`

### `GEPAEngine`

This is the main optimization loop. It is generic over `(DataId, DataInst, Trajectory, RolloutOutput)` — it doesn't know what your data looks like. Everything domain-specific lives in the adapter.

**Constructor parameters (key ones):**

| Parameter | Type | Purpose |
|---|---|---|
| `adapter` | `GEPAAdapter` | Evaluate candidates, build reflective datasets |
| `reflective_proposer` | `ReflectiveMutationProposer` | Proposes mutations |
| `merge_proposer` | `MergeProposer \| None` | Proposes merged candidates |
| `valset` | `DataLoader` | Validation set for Pareto tracking |
| `seed_candidate` | `dict[str, str]` | Starting point |
| `stop_callback` | `StopperProtocol` | When to halt |
| `frontier_type` | `FrontierType` | How to track Pareto front |
| `callbacks` | `list[GEPACallback]` | Monitoring hooks |

### `GEPAEngine.run() → GEPAState`

The main loop. Pseudocode:

```python
state = initialize_gepa_state(seed_candidate, ...)  # evaluate seed on valset

while not _should_stop(state):
    state.i += 1
    state.save(run_dir)                              # checkpoint

    # --- Attempt reflective mutation ---
    proposal = reflective_proposer.propose(state)    # → CandidateProposal | None

    if proposal is not None:
        # proposal already passed minibatch acceptance check inside proposer
        _run_full_eval_and_add(proposal.candidate, state)
        # → evaluates on full valset, updates Pareto front, logs metrics

    # --- Attempt merge (if scheduled) ---
    if merge_is_due:
        merge_proposal = merge_proposer.propose(state)
        if merge_proposal is not None:
            _run_full_eval_and_add(merge_proposal.candidate, state)

    notify_callbacks(IterationEndEvent(...))

return state
```

### `GEPAEngine._run_full_eval_and_add(new_program, state)`

Called after a proposal passes its minibatch check:
1. `_evaluate_on_valset(new_program, state)` — runs the `val_evaluation_policy` to pick which valset IDs to evaluate, calls `adapter.evaluate()` on them
2. `state.update_state_with_new_program(parent_idx, new_program, valset_evaluation)` — adds to candidates list, updates all Pareto frontiers
3. Fires `ParetoFrontUpdatedEvent`, `ValsetEvaluatedEvent`, logs detailed metrics

---

## Adapter — `src/gepa/core/adapter.py`

The `GEPAAdapter` protocol is the single integration point between GEPA and your system. Implement two methods and GEPA handles everything else.

```python
class GEPAAdapter(Protocol[DataInst, Trajectory, RolloutOutput]):

    def evaluate(
        self,
        batch: list[DataInst],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> EvaluationBatch[Trajectory, RolloutOutput]:
        """Run your system with this candidate on each example in batch.
        Return per-example outputs, scores, and optionally trajectories."""
        ...

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch,
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """Turn trajectories into a dataset the reflection LM can read.
        Recommended schema: {"component_name": [{"Inputs": ..., "Generated Outputs": ..., "Feedback": ...}]}"""
        ...
```

### `EvaluationBatch` (dataclass)

```python
@dataclass
class EvaluationBatch:
    outputs: list[RolloutOutput]          # raw per-example outputs
    scores: list[float]                   # per-example scores (0.0–1.0)
    trajectories: list[Trajectory] | None # full execution traces (if capture_traces=True)
    objective_scores: list[dict[str, float]] | None  # multi-objective metrics
```

The contract: `len(outputs) == len(scores) == len(batch)`.

### Optional: `propose_new_texts()`

Adapters can also implement `propose_new_texts()` to override GEPA's default LLM-based reflection with custom proposal logic (e.g., using DSPy signatures, domain-specific mutation).

---

## Reflective Mutation Proposer — `src/gepa/proposer/reflective_mutation/reflective_mutation.py`

### `ReflectiveMutationProposer.propose(state) → CandidateProposal | None`

This is where the core optimization logic lives. Step by step:

**Step 1: Select a candidate**
```python
candidate_idx = candidate_selector.select_candidate_idx(state)
# → ParetoCandidateSelector: random weighted sample from Pareto front
# → CurrentBestCandidateSelector: always pick highest avg-score candidate
# → EpsilonGreedyCandidateSelector: explore with prob epsilon, exploit otherwise
```

**Step 2: Sample a minibatch**
```python
minibatch_ids = batch_sampler.next_minibatch_ids(trainset, state)
# EpochShuffledBatchSampler: shuffles IDs each epoch, pads to minibatch size
# favors least-evaluated examples to avoid over-sampling
minibatch = trainset.fetch(minibatch_ids)
```

**Step 3: Evaluate current candidate with trace capture**
```python
eval_batch = adapter.evaluate(minibatch, current_candidate, capture_traces=True)
# → trajectories are full execution traces (error messages, model outputs, etc.)
```

Skips if:
- `trajectories is None` (adapter didn't provide them)
- All scores == `perfect_score` (already optimal on this batch)

**Step 4: Select which components to update**
```python
components_to_update = module_selector(state, trajectories, scores, candidate_idx, candidate)
# RoundRobinReflectionComponentSelector: cycles through components, one per iteration
# AllReflectionComponentSelector: updates all components simultaneously
```

**Step 5: Build reflective dataset**
```python
reflective_dataset = adapter.make_reflective_dataset(
    candidate, eval_batch, components_to_update
)
# Returns: {"component_name": [{"Inputs": ..., "Outputs": ..., "Feedback": ...}]}
```

**Step 6: Propose new component text**

Priority order:
1. `adapter.propose_new_texts()` if adapter implements it
2. `custom_candidate_proposer()` if user provided one
3. Default: `InstructionProposalSignature.run(reflection_lm, ...)` — an LLM call

The default reflection prompt looks like:
```
I provided an assistant with the following instructions:
<current component text>

Examples with feedback:
<reflective dataset entries>

Write a new instruction for the assistant that improves performance...
```

Output is parsed from triple-backtick blocks in the LLM response.

**Step 7: Evaluate new candidate on same minibatch**
```python
new_eval_batch = adapter.evaluate(minibatch, new_candidate, capture_traces=False)
# Uses evaluation cache to avoid re-evaluating already-seen (candidate, example) pairs
```

**Step 8: Acceptance check**
```python
if sum(new_scores) > sum(old_scores):
    return CandidateProposal(candidate=new_candidate, ...)
else:
    return None  # rejected, try again next iteration
```

---

## Merge Proposer — `src/gepa/proposer/merge.py`

### `MergeProposer.propose(state) → CandidateProposal | None`

Merging combines two Pareto-optimal candidates that each excel on different task subsets. The intuition: if candidate A does well on examples 1-50 and candidate B does well on examples 51-100, merging them should do well on both.

**Step 1: Find merge candidates**
```python
pareto_front = state.get_pareto_front_mapping()
# → dict[FrontierKey, set[ProgramIdx]] — which programs are best on which examples

dominators = find_dominator_programs(state)
# → programs that dominate others in the current population
```

**Step 2: Find a mergeable pair**

A valid merge requires:
- Programs `id1` and `id2` share a **common ancestor**
- For at least one component: `id1` differs from ancestor AND `id2` differs from ancestor (they evolved independently)
- `len(common_validation_ids) >= val_overlap_floor` (they've both been evaluated on enough shared examples)

**Step 3: Construct merged candidate**
```python
# For each component:
#   - If id1 kept ancestor's text AND id2 changed it → use id2's version
#   - If id2 kept ancestor's text AND id1 changed it → use id1's version
#   - If both changed → pick the one with better score on overlapping examples
```

**Step 4: Evaluate and accept**
```python
# Select diverse subsample from overlapping validation examples
# Split into 3 buckets: examples where id1 is better, id2 is better, tied
# Sample proportionally from each bucket

merged_score = sum(merged_scores)
parent_score = max(sum(id1_scores), sum(id2_scores))

if merged_score >= parent_score:
    return CandidateProposal(candidate=merged, tag="merge", ...)
```

---

## State — `src/gepa/core/state.py`

### `GEPAState`

Tracks everything that has happened during optimization.

**Key attributes:**

| Attribute | Type | Description |
|---|---|---|
| `program_candidates` | `list[dict[str, str]]` | All explored candidates (by index) |
| `prog_candidate_val_subscores` | `list[dict[DataId, float]]` | Score of each candidate on each val example |
| `prog_candidate_objective_scores` | `list[ObjectiveScores]` | Per-objective aggregate scores |
| `parent_program_for_candidate` | `list[list[ProgramIdx \| None]]` | Lineage tree |
| `pareto_front_valset` | `dict[DataId, set[ProgramIdx]]` | Per-example Pareto front |
| `objective_pareto_front` | `dict[str, float]` | Best score per objective metric |
| `i` | `int` | Current iteration (starts -1) |
| `total_num_evals` | `int` | Total evaluations consumed |
| `evaluation_cache` | `EvaluationCache` | Cache of (candidate, example) → score |

### Pareto Frontier Types

Controlled by `frontier_type` parameter:

| Type | Frontier Key | Meaning |
|---|---|---|
| `"instance"` | `DataId` | Best program per validation example |
| `"objective"` | `str` | Best program per objective metric |
| `"hybrid"` | `DataId` or `str` | Both instance and objective frontiers |
| `"cartesian"` | `(DataId, str)` | Best program per (example, metric) pair |

### `GEPAState.update_state_with_new_program(parent_idx, new_program, valset_evaluation)`

Called after a candidate is accepted:
1. Appends new program to `program_candidates`
2. Stores its valset scores in `prog_candidate_val_subscores`
3. Updates Pareto front: for each val example, if new program's score is best → add to front
4. Returns the new program's index

### `EvaluationCache`

Caches `(candidate_hash, DataId) → (output, score, objective_scores)`. Avoids re-evaluating the same candidate on the same example. Candidate hash is SHA-256 of the sorted JSON representation.

---

## Strategies

### Candidate Selection — `src/gepa/strategies/candidate_selector.py`

| Selector | Strategy |
|---|---|
| `ParetoCandidateSelector` | Randomly samples from the Pareto front (weighted by score) |
| `CurrentBestCandidateSelector` | Always picks the highest average-score candidate |
| `EpsilonGreedyCandidateSelector` | Explore random candidates with prob ε, exploit best with prob 1-ε |

### Batch Sampling — `src/gepa/strategies/batch_sampler.py`

`EpochShuffledBatchSampler`:
- Shuffles all training IDs at the start of each epoch
- Returns the next `minibatch_size` IDs
- Pads to minibatch size using least-frequently-evaluated examples (ensures coverage)
- Deterministic via seeded RNG

### Component Selection — `src/gepa/strategies/component_selector.py`

For multi-component candidates (e.g. `{"system_prompt": ..., "user_template": ...}`):

| Selector | Strategy |
|---|---|
| `RoundRobinReflectionComponentSelector` | Updates one component per iteration, cycling through all |
| `AllReflectionComponentSelector` | Updates all components simultaneously |

### Evaluation Policy — `src/gepa/strategies/eval_policy.py`

`FullEvaluationPolicy`: always evaluates the full validation set. The `get_best_program()` method returns the program with the highest average score (ties broken by how many val examples it has been evaluated on).

---

## Stop Conditions — `src/gepa/utils/stop_condition.py`

All implement `StopperProtocol`: `__call__(state: GEPAState) -> bool`.

| Stopper | Stops when |
|---|---|
| `MaxMetricCallsStopper(n)` | `state.total_num_evals >= n` |
| `TimeoutStopCondition(seconds)` | Wall clock time exceeded |
| `FileStopper(path)` | File at `path` exists (graceful shutdown) |
| `ScoreThresholdStopper(threshold)` | Best candidate score >= threshold |
| `NoImprovementStopper(n)` | No improvement in last n iterations |
| `SignalStopper()` | SIGINT or SIGTERM received |
| `CompositeStopper([...], mode)` | Any (mode="any") or all (mode="all") sub-stoppers trigger |

`api.optimize()` builds a `CompositeStopper` automatically from `max_metric_calls`, `perfect_score`, etc.

---

## Default Adapter — `src/gepa/adapters/default_adapter/default_adapter.py`

The built-in adapter for single-turn LLM prompt optimization.

**Data types:**
```python
DefaultDataInst = TypedDict({
    "input": str | dict,          # task input
    "additional_context": str,    # extra context (optional)
    "answer": str | list[str],    # expected answer
})

DefaultTrajectory = TypedDict({
    "data": DefaultDataInst,
    "full_assistant_response": str,
    "feedback": str,
})
```

**`evaluate(batch, candidate, capture_traces)`:**
1. Extracts the system prompt (first value in `candidate` dict)
2. Calls `litellm.batch_completion()` for all examples in parallel
3. For each response, calls `evaluator(data_inst, response)` → `(score, feedback, obj_scores)`
4. Assembles `EvaluationBatch` with outputs, scores, and trajectories

**`make_reflective_dataset(candidate, eval_batch, components_to_update)`:**
- Returns `{"system_prompt": [{"Inputs": ..., "Generated Outputs": ..., "Feedback": ...}, ...]}`
- Each entry is one training example with its output and the evaluator's feedback

**Default evaluator: `ContainsAnswerEvaluator`**
- Score 1.0 if the expected answer string appears in the response
- Configurable `failure_score` for incorrect answers

---

## Callbacks — `src/gepa/core/callbacks.py`

Callbacks let you monitor the optimization in real time. Implement `GEPACallback` and pass it to the engine.

Each callback method receives a typed `TypedDict` event. All methods are optional (default: no-op).

**Key events fired during a run:**

| Event | Fired when |
|---|---|
| `OptimizationStartEvent` | Loop begins |
| `IterationStartEvent` / `IterationEndEvent` | Each iteration |
| `CandidateSelectedEvent` | A candidate is chosen for mutation |
| `MinibatchSampledEvent` | A minibatch is drawn |
| `EvaluationStartEvent` / `EvaluationEndEvent` | Before/after `adapter.evaluate()` |
| `ReflectiveDatasetBuiltEvent` | After `make_reflective_dataset()` |
| `ProposalStartEvent` / `ProposalEndEvent` | Before/after LLM reflection call |
| `CandidateAcceptedEvent` / `CandidateRejectedEvent` | Minibatch acceptance decision |
| `MergeAttemptedEvent` / `MergeAcceptedEvent` / `MergeRejectedEvent` | Merge lifecycle |
| `ValsetEvaluatedEvent` | After full valset evaluation |
| `ParetoFrontUpdatedEvent` | When Pareto front changes |
| `BudgetUpdatedEvent` | After each evaluation count update |
| `StateSavedEvent` | After state checkpointed to disk |
| `ErrorEvent` | On exception (non-fatal) |

`CompositeCallback` aggregates multiple callbacks and dispatches to all of them.

---

## Result — `src/gepa/core/result.py`

### `GEPAResult`

Returned by `optimize()` and `optimize_anything()`.

**Key attributes:**

| Attribute | Description |
|---|---|
| `best_candidate` | The best-scoring program (`str` or `dict[str, str]`) |
| `best_idx` | Index of best candidate in `candidates` list |
| `candidates` | All explored candidates in discovery order |
| `val_aggregate_scores` | Average valset score for each candidate |
| `val_subscores` | Per-example scores for each candidate |
| `per_val_instance_best_candidates` | Pareto front: `{val_id → set of best program indices}` |
| `discovery_eval_counts` | How many metric calls had been used when each candidate was discovered |
| `total_metric_calls` | Total evaluations used |
| `run_dir` | Directory where state was checkpointed |

**Serialization:** `result.to_dict()` / `GEPAResult.from_dict()` for JSON-safe persistence.

---

## Logging — `src/gepa/logging/`

### `LoggerProtocol` / `StdOutLogger` / `Logger`

`StdOutLogger` just calls `print()`. `Logger` multiplexes stdout/stderr to files using a `Tee` class.

### `ExperimentTracker` — `src/gepa/logging/experiment_tracker.py`

Plugs into wandb and/or mlflow for experiment tracking. Created via `create_experiment_tracker()`. Methods: `log_metrics(metrics, step)`, `start_run()`, `end_run()`. Handles both backends simultaneously with graceful error handling.

---

## Data Loading — `src/gepa/core/data_loader.py`

`DataLoader[DataId, DataInst]` is a protocol:
- `all_ids() → Sequence[DataId]` — the universe of example IDs
- `fetch(ids) → list[DataInst]` — materialize payloads for those IDs
- `__len__() → int`

`ListDataLoader` wraps a plain Python list. `ensure_loader()` converts either a list or an existing `DataLoader` to a `DataLoader`.

`DataId` must be `ComparableHashable` (hashable + supports `<` ordering). This is needed to deterministically sort evaluation results.

---

## Complete Call Graph

```
gepa.optimize()
  └─ ensure_loader(trainset), ensure_loader(valset)
  └─ DefaultAdapter(task_lm, evaluator)           [if no adapter provided]
  └─ ReflectiveMutationProposer(
       adapter, candidate_selector, module_selector,
       batch_sampler, reflection_lm, ...)
  └─ MergeProposer(valset, evaluator, ...)         [if use_merge=True]
  └─ GEPAEngine(adapter, reflective_proposer, merge_proposer, ...)
       └─ .run()
            ├─ initialize_gepa_state(seed_candidate)
            │    └─ adapter.evaluate(valset, seed)  ← initial full eval
            │
            └─ loop:
                 ├─ state.save(run_dir)
                 │
                 ├─ ReflectiveMutationProposer.propose(state)
                 │    ├─ CandidateSelector.select_candidate_idx(state)
                 │    ├─ BatchSampler.next_minibatch_ids(trainset, state)
                 │    ├─ adapter.evaluate(minibatch, curr_candidate, capture_traces=True)
                 │    │    └─ [task LM calls, per-example scoring]
                 │    ├─ ComponentSelector(state, trajectories, ...) → list[str]
                 │    ├─ adapter.make_reflective_dataset(candidate, eval_batch, components)
                 │    ├─ InstructionProposalSignature.run(reflection_lm, dataset)
                 │    │    └─ [reflection LM call → new component text]
                 │    ├─ adapter.evaluate(minibatch, new_candidate)  ← acceptance check
                 │    └─ sum(new_scores) > sum(old_scores)? → return CandidateProposal
                 │
                 ├─ [if proposal accepted]:
                 │    ├─ adapter.evaluate(valset_ids, new_candidate)  ← full valset eval
                 │    ├─ state.update_state_with_new_program(...)
                 │    │    └─ update pareto_front_valset per DataId
                 │    └─ notify_callbacks(ValsetEvaluatedEvent, ParetoFrontUpdatedEvent)
                 │
                 ├─ MergeProposer.propose(state)                [if scheduled]
                 │    ├─ find_dominator_programs(state)
                 │    ├─ find_common_ancestor_pair(...)
                 │    ├─ construct merged candidate from id1 + id2 + ancestor
                 │    ├─ adapter.evaluate(subsample, merged_candidate)
                 │    └─ merged_score >= max(parent_scores)? → return CandidateProposal
                 │
                 └─ StopperProtocol(state) → bool?

  └─ GEPAResult.from_state(state)
```

---

## Key Design Choices

**Why Pareto frontier instead of single best?**
Different candidates may excel on different subsets of the validation set. Keeping the full frontier allows the merge operator to combine their strengths, and prevents premature convergence to a single solution.

**Why minibatch acceptance before full valset?**
Full valset evaluation is expensive (calls the task LM on every example). The minibatch check is a cheap filter — only candidates that improve on their own evaluation minibatch get promoted to full evaluation.

**Why capture traces?**
Raw scores tell you *that* a candidate failed. Traces (error messages, full responses, reasoning logs) tell the reflection LLM *why* it failed and what specifically to fix. This is GEPA's key advantage over scalar-feedback optimizers.

**Why the adapter pattern?**
The engine is fully decoupled from what's being optimized. The same loop works for system prompts (DefaultAdapter), entire DSPy programs (DSPyFullProgramAdapter), RAG pipelines (GenericRAGAdapter), or arbitrary code (OptimizeAnythingAdapter). You only need to implement `evaluate()` and `make_reflective_dataset()`.
