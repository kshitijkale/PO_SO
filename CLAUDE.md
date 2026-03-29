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
- **No relative imports** in `src/` (enforced by ruff `ban-relative-imports = "all"`)
- Follows Google Python Style Guide
- Pre-commit hooks run ruff-check (with `--fix`), ruff-format, check-yaml, check-toml, check-added-large-files (3MB max), check-merge-conflict, and debug-statements

## Architecture

### Entry Points

- **`gepa.optimize()`** (`src/gepa/api.py`): High-level API for prompt optimization. Constructs a `GEPAEngine` with a `DefaultAdapter` (or user-provided adapter), wires up proposers, strategies, and logging, then runs the optimization loop.
- **`gepa.optimize_anything.optimize_anything()`** (`src/gepa/optimize_anything.py`): Universal API for optimizing any text artifact (code, configs, SVGs, etc.), not just prompts. Three modes: single-task search, multi-task search, and generalization (with train/val split).

### Core Loop (`src/gepa/core/`)

- **`engine.py` — `GEPAEngine`**: Orchestrates the main optimization loop. Takes an adapter, proposers (reflective mutation + optional merge), strategies, and stop conditions. Each iteration: propose a candidate, evaluate, accept/reject, update Pareto front.
- **`adapter.py` — `GEPAAdapter` (Protocol)**: The integration point for external systems. Implement `evaluate()` (run candidate on data, return `EvaluationBatch` with scores/traces) and `make_reflective_dataset()` (convert traces into a dataset for the reflection LLM).
- **`state.py` — `GEPAState`**: Tracks all candidates, their scores, per-example Pareto frontiers, and evaluation cache. Frontier types: `instance`, `objective`, `hybrid`, `cartesian`.
- **`callbacks.py`**: Event system with typed events (e.g., `IterationStartEvent`, `CandidateAcceptedEvent`, `ParetoFrontUpdatedEvent`). New observability should go through `notify_callbacks(...)` — do not bypass callback plumbing. Keep callback payloads serializable.

### Proposers (`src/gepa/proposer/`)

- **`reflective_mutation/reflective_mutation.py` — `ReflectiveMutationProposer`**: Core proposer. Selects a candidate, evaluates on a minibatch capturing traces, builds a reflective dataset, then calls an LLM to propose an improved candidate based on failure analysis. Contains two evaluation paths that must be preserved: (1) cached non-trace path for normal mutation scoring, and (2) trace-capturing path when MemV0/OI needs reflective records.
- **`reflective_mutation/memory.py` — `ReflectionMemory`**: Rolling episodic memory of past reflection attempts. Stores up to `max_entries` `ReflectionMemoryEntry` records (score before/after, accepted/rejected, failure modes) and formats them into prompt text so the reflection LLM avoids repeating rejected strategies. `summarize_change()` produces a heuristic one-line diff summary without LLM calls.
- **`reflective_mutation/memory_tree.py` — `MemoryTree`** (MemV0): Structured hierarchical memory used when `memory_version="v0"`. Organizes lessons by failure mode.
- **`reflective_mutation/outcome_interpreter.py` — `OutcomeInterpreter`** (MemV0): LLM-powered component that interprets evaluation traces and extracts structured lessons. Must never raise to callers — falls back on parse/LM failure.
- **`reflective_mutation/memory_renderer.py` — `TieredMemoryRenderer`** (MemV0): Renders memory tree into tiered prompt text for injection into the reflection LLM.
- **`merge.py` — `MergeProposer`**: Combines strengths of two Pareto-optimal candidates that excel on different task subsets.

### Strategies (`src/gepa/strategies/`)

- **`candidate_selector.py`**: `ParetoCandidateSelector`, `CurrentBestCandidateSelector`, `EpsilonGreedyCandidateSelector`
- **`batch_sampler.py`**: `EpochShuffledBatchSampler` for training minibatch sampling
- **`component_selector.py`**: `RoundRobinReflectionComponentSelector`, `AllReflectionComponentSelector` — which components of a multi-component candidate to update each iteration

### Adapters (`src/gepa/adapters/`)

Built-in adapters implementing `GEPAAdapter` for different use cases: `DefaultAdapter` (system prompt optimization), `DSPyAdapter`, `DSPyFullProgramAdapter`, `GenericRAGAdapter`, `MCPAdapter`, `TerminalBenchAdapter`, `AnyMathsAdapter`. The `optimize_anything_adapter` bridges the `optimize_anything` API to the core engine.

### Research Observability (`src/gepa/callbacks/`)

Callbacks that make the optimization process fully transparent. Enable via `research_mode=True` on `optimize()` / `optimize_anything()`, or register individually. Key callbacks: `ResearchLogger` (per-iteration markdown reports + JSONL event stream), `StateLogger` (numbered JSON snapshots per iteration), `LineageTracker` (candidate ancestry graph), `LiveDisplay` (terminal dashboard), `VerboseDisplay` (structured console output), `MemV0Observer` (MemV0 internals tracing), `MetricsRollupLogger` (aggregate counters). See `LOGGING.md` for full documentation of events, run directory structure, redaction policy, and event correlation.

### Key Conventions

- **Candidate type:** `dict[str, str]` — a mapping of component names to their text values. Multi-component candidates allow optimizing multiple parts of a system simultaneously.
- **LLM access:** External LLM calls go through LiteLLM callables (`reflection_lm`, `lesson_lm`, `oi_lm`) and adapter-specific model clients.
- **MemV0 style:** Follow existing naming and dataclass-first style in MemV0 modules. Keep typed event payloads explicit using `TypedDict` patterns from `callbacks.py`.

## Running Experiments

The `experiments/aime_memory/` directory validates the reflection memory feature on AIME math problems.

```bash
# Full experiment (with reflection memory V2)
uv run python -m experiments.aime_memory.run \
  --seed 0 \
  --memory \
  --max-calls 500 \
  --reflection-lm openai/gpt-4.1-mini \
  --solver-lm gpt-4.1-mini \
  --workers 32 \
  --memory-entries 10

# Lightweight variant for quick iteration
uv run python -m experiments.aime_memory.run_light --seed 0 --memory

# Verbose variant (MemV0 with console tracing)
uv run python -m experiments.aime_memory.run_verbose --seed 0 --memory
```

## Key Reference Documents

- **`GEPA_WALKTHROUGH.md`**: Deep-dive walkthrough of the full architecture — covers the engine loop, adapter contract, state machine, and proposer internals in detail. Read this before making structural changes.
- **`AGENTS.md`**: Agent-focused quick reference — code style, architecture pointers, MemV0 conventions, integration points, and security guidelines.
- **`LOGGING.md`**: Full documentation of the logging and observability system — event types, callback behaviors, redaction policy, event correlation index, and MemV0 sink parity.

## Pyright Exclusions

Several adapter directories and tests are excluded from pyright checks (see `pyrightconfig.json`): `dspy_adapter`, `dspy_full_program_adapter`, `generic_rag_adapter`, `anymaths_adapter`, `terminal_bench_adapter`, `gskill`, `examples`, `tests`.
