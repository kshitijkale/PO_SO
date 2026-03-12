# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is GEPA?

GEPA (Genetic-Pareto) is a Python framework for optimizing text components (AI prompts, code, instructions, agent architectures) using LLM-based reflection and Pareto-efficient evolutionary search. The core loop: select a candidate from the Pareto frontier, execute on a minibatch capturing traces, reflect via LLM to diagnose failures, mutate to produce an improved candidate, and accept if improved.

## Setup

We use **uv** for dependency management. All Python executions must go through uv.

```bash
uv sync --extra dev
```

## Build, Lint, Test, Type Check

```bash
uv run pytest                          # run all tests
uv run pytest tests/test_state.py      # run a single test file
uv run pytest tests/test_state.py::test_name -v  # run a single test
uv run ruff check src/                 # lint
uv run ruff format src/                # format
uv run pyright                         # type check (standard mode)
uv run pyright src/gepa/strategies/    # type check a specific module
uv run pre-commit run                  # run pre-commit hooks on staged files
```

Tests use a record/replay pattern for LLM calls (see `tests/conftest.py`). By default tests run in replay mode using cached responses. Set `RECORD_TESTS=true` to make live API calls and regenerate cache files.

## Code Style

- **Linter/formatter:** ruff (line length 120, double quotes, space indent)
- **Type checking:** pyright in standard mode
- **Python target:** 3.10+
- **No relative imports** (enforced by ruff `ban-relative-imports = "all"`)
- Follows Google Python Style Guide
- Pre-commit hooks run ruff-check (with `--fix`) and ruff-format automatically on commit

## Architecture

### Entry Points

- **`gepa.optimize()`** (`src/gepa/api.py`): High-level API for prompt optimization. Constructs a `GEPAEngine` with a `DefaultAdapter` (or user-provided adapter), wires up proposers, strategies, and logging, then runs the optimization loop.
- **`gepa.optimize_anything.optimize_anything()`** (`src/gepa/optimize_anything.py`): Universal API for optimizing any text artifact (code, configs, SVGs, etc.), not just prompts. Three modes: single-task search, multi-task search, and generalization (with train/val split).

### Core Loop (`src/gepa/core/`)

- **`engine.py` — `GEPAEngine`**: Orchestrates the main optimization loop. Takes an adapter, proposers (reflective mutation + optional merge), strategies, and stop conditions. Each iteration: propose a candidate, evaluate, accept/reject, update Pareto front.
- **`adapter.py` — `GEPAAdapter` (Protocol)**: The integration point for external systems. Implement `evaluate()` (run candidate on data, return `EvaluationBatch` with scores/traces) and `make_reflective_dataset()` (convert traces into a dataset for the reflection LLM).
- **`state.py` — `GEPAState`**: Tracks all candidates, their scores, per-example Pareto frontiers, and evaluation cache. Frontier types: `instance`, `objective`, `hybrid`, `cartesian`.
- **`callbacks.py`**: Event system with typed events (e.g., `IterationStartEvent`, `CandidateAcceptedEvent`, `ParetoFrontUpdatedEvent`).

### Proposers (`src/gepa/proposer/`)

- **`reflective_mutation/reflective_mutation.py` — `ReflectiveMutationProposer`**: Core proposer. Selects a candidate, evaluates on a minibatch capturing traces, builds a reflective dataset, then calls an LLM to propose an improved candidate based on failure analysis. Accepts an optional `reflection_memory: ReflectionMemory` to inject optimization history into the reflection prompt.
- **`reflective_mutation/memory.py` — `ReflectionMemory`**: Rolling episodic memory of past reflection attempts. Stores up to `max_entries` `ReflectionMemoryEntry` records (score before/after, accepted/rejected, failure modes) and formats them into prompt text so the reflection LLM avoids repeating rejected strategies. `summarize_change()` produces a heuristic one-line diff summary without LLM calls.
- **`merge.py` — `MergeProposer`**: Combines strengths of two Pareto-optimal candidates that excel on different task subsets.

### Strategies (`src/gepa/strategies/`)

- **`candidate_selector.py`**: `ParetoCandidateSelector`, `CurrentBestCandidateSelector`, `EpsilonGreedyCandidateSelector`
- **`batch_sampler.py`**: `EpochShuffledBatchSampler` for training minibatch sampling
- **`component_selector.py`**: `RoundRobinReflectionComponentSelector`, `AllReflectionComponentSelector` — which components of a multi-component candidate to update each iteration

### Adapters (`src/gepa/adapters/`)

Built-in adapters implementing `GEPAAdapter` for different use cases: `DefaultAdapter` (system prompt optimization), `DSPyAdapter`, `DSPyFullProgramAdapter`, `GenericRAGAdapter`, `MCPAdapter`, `TerminalBenchAdapter`, `AnyMathsAdapter`. The `optimize_anything_adapter` bridges the `optimize_anything` API to the core engine.

### Research Observability (`src/gepa/callbacks/`)

Callbacks that make the optimization process fully transparent. Enable via `research_mode=True` on `optimize()` / `optimize_anything()`, or register individually.

**Opt-in:**
```python
# Single flag — registers all four callbacks automatically
result = gepa.optimize(..., research_mode=True, run_dir="./runs/exp1")

# optimize_anything equivalent
config = GEPAConfig(
    engine=EngineConfig(run_dir="./runs/exp1"),
    tracking=TrackingConfig(research_mode=True),
)
```

**Callbacks:**
- **`ResearchLogger`**: Exhaustive per-iteration markdown reports + JSONL event stream. Writes `iterations/iteration_NNN.md`, `candidates/`, `memory/`, `llm_calls/`, `log.jsonl`, `summary.jsonl`, `pareto_timeline.jsonl`. Each iteration report covers: candidate selection, minibatch, pre/post evaluation scores, full reflective dataset, memory state table + injected text, complete LLM prompt + response, before/after unified diff, acceptance decision, Pareto front snapshot.
- **`StateLogger`**: 12 numbered JSON snapshots per iteration into `states/` — one file per step: `01_iteration_start`, `02_selection_and_minibatch`, `03_eval_current`, `04_reflective_dataset`, `05_memory_before/after`, `06_proposal_<component>`, `07_eval_proposed`, `08_decision`, `09_pareto`, `10_valset`, `11_merge_result`, `12_iteration_end`. Each file is self-contained.
- **`LineageTracker`**: Candidate ancestry graph. Produces `lineage.jsonl`, `lineage_graph.json`, `lineage_tree.md` (human-readable tree of the best candidate's ancestry).
- **`LiveDisplay`**: Live terminal dashboard after each iteration — score history, memory utilization, acceptance rate.

**New callback events (added to `src/gepa/core/callbacks.py`):**
- `MemoryEntryAddedEvent` — fires on every `ReflectionMemory.add()` with before/after size and evicted entry
- `MemoryQueriedEvent` — fires on every `format_for_prompt()` with the exact injected text
- `MemoryStateSnapshotEvent` — fires before and after proposal with full memory state
- `ProposalTraceEvent` — fires per component with full LLM prompt, raw response, latency, and whether memory was injected

**Instrumented internals:**
- `ReflectionMemory` accepts `callbacks` and `current_iteration` fields; fires all four events automatically
- `Signature.run_with_trace()` (in `base.py`) captures rendered prompt, raw response, and latency alongside the extracted result
- `ReflectiveMutationProposer.propose_new_texts()` uses `run_with_trace()` and fires `ProposalTraceEvent` per component
- Memory snapshots fired at `before_proposal` and `after_proposal` phases in `propose()`

**Run directory structure produced:**
```
run_dir/
├── log.jsonl                        # every event, one JSON line each
├── summary.jsonl                    # one line per iteration (key metrics)
├── pareto_timeline.jsonl
├── lineage.jsonl / lineage_graph.json / lineage_tree.md
├── iterations/iteration_NNN.md      # full narrative per iteration
├── candidates/candidate_NNN.json
├── memory/
│   ├── memory_events.jsonl          # every add/query/eviction
│   ├── memory_state_iter_NNN_<phase>.json
│   └── prompts_with_memory/iter_NNN_<component>.txt
├── llm_calls/
│   ├── iter_NNN_<component>.json
│   ├── iter_NNN_<component>_prompt.txt
│   └── iter_NNN_<component>_response.txt
└── states/
    ├── iter_NNN_01_iteration_start.json
    ├── iter_NNN_03_eval_current.json
    └── ... (12 files per iteration)
```

### Key Type: Candidate

A candidate is `dict[str, str]` — a mapping of component names to their text values. Multi-component candidates allow optimizing multiple parts of a system simultaneously.

## Pyright Exclusions

Several adapter directories and tests are excluded from pyright checks (see `pyrightconfig.json`): `dspy_adapter`, `dspy_full_program_adapter`, `generic_rag_adapter`, `anymaths_adapter`, `terminal_bench_adapter`, `gskill`, `examples`, `tests`.
