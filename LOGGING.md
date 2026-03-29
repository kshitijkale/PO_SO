# GEPA Logging and Experiment Tracking Documentation

GEPA implements a multi-layered logging and experiment tracking system designed for both production monitoring and deep research analysis. The system is built on an event-driven architecture where the core engine and proposers fire fine-grained events to a set of observers (callbacks).

---

## 1. High-Level Architecture

The logging process is orchestrated by three primary components:

1.  **Loggers (`src/gepa/logging/logger.py`)**: Handle text-based output to the console and log files.
2.  **Experiment Trackers (`src/gepa/logging/experiment_tracker.py`)**: Manage integrations with external platforms like Weights & Biases (WandB) and MLflow.
3.  **Callbacks (`src/gepa/core/callbacks.py` and `src/gepa/callbacks/`)**: Provide an event-driven interface for monitoring every step of the optimization loop.

---

## 2. Standard Logging (`Logger` & `Tee`)

The `Logger` class provides a context manager that intercepts `sys.stdout` and `sys.stderr` to redirect them to both the console and a file.

- **`Tee` Class**: A helper that duplicates a write operation to multiple file-like objects.
- **`StdOutLogger`**: A simple implementation that just prints to the console.
- **`Logger`**: 
    - Creates `run_log.txt` (stdout) and `run_log_stderr.txt` (stderr).
    - Automatically flushes after each log entry to ensure data persistence during long runs.

---

## 3. Experiment Tracking (`ExperimentTracker`)

The `ExperimentTracker` provides a unified interface for logging numeric metrics and metadata to external backends.

- **Supported Backends**: Weights & Biases (WandB) and MLflow.
- **Lifecycle Management**: Handles initialization, starting runs, logging metrics with step numbers, and gracefully ending runs.
- **Context Manager**: Can be used with a `with` statement to ensure runs are finished even if an error occurs.
- **Filtering**: Automatically filters out non-numeric metrics for MLflow compatibility.

---

## 4. Event-Driven Callbacks (`GEPACallback`)

The core of GEPA's observability is its callback system. The `GEPAEngine` and `ReflectiveMutationProposer` fire specific events throughout the optimization lifecycle.

### Key Event Types:
- **Lifecycle**: `OptimizationStartEvent`, `IterationStartEvent`, `IterationEndEvent`, `OptimizationEndEvent`.
- **Search**: `CandidateSelectedEvent`, `MinibatchSampledEvent`, `CandidateAcceptedEvent`, `CandidateRejectedEvent`.
- **Evaluation**: `EvaluationStartEvent`, `EvaluationEndEvent`, `ValsetEvaluatedEvent`, `BudgetUpdatedEvent`.
- **Reflection**: `ReflectiveDatasetBuiltEvent`, `ProposalStartEvent`, `ProposalEndEvent`, `ProposalTraceEvent`.
- **Memory**: `MemoryEntryAddedEvent`, `MemoryQueriedEvent`, `MemoryStateSnapshotEvent`, `LessonGeneratedEvent`.

---

## 5. Research observability (`ResearchLogger`)

`ResearchLogger` is a specialized callback designed for exhaustive, zero-truncation logging. It is enabled via `research_mode=True` in `gepa.optimize()`.

When `research_mode=True`, GEPA also enables `MetricsRollupLogger`, which writes run-level rollups to:
- **`metrics.jsonl`**: append-only stream of aggregate counters over time.
- **`metrics_summary.json`**: final summarized metrics at optimization end.

### Output Structure (`run_dir/`):
- **`log.jsonl`**: Complete, machine-readable stream of every event fired during the run.
- **`summary.jsonl`**: One JSON line per iteration containing key aggregate metrics (best score, elapsed time, etc.).
- **`index.md`**: A human-readable Markdown index with a timeline of the entire run.
- **`iterations/`**: Detailed Markdown reports for every single iteration, including:
    - Candidate before/after diffs.
    - Full LLM prompts and raw responses.
    - Per-example evaluation scores and outputs.
    - Reflection memory snapshots and injected lessons.
- **`candidates/`**: Full JSON records of every accepted candidate in the population.
- **`llm_calls/`**: Verbatim records of every LLM interaction, including separate `.txt` files for prompts and responses.
- **`memory/`**: Detailed logs of reflection memory events, snapshots, and generated lessons.
- **`metrics.jsonl` / `metrics_summary.json`**: Acceptance/rejection, evaluation correctness counts, OI/eviction counts, proposal latency rollups, and budget usage summaries.

---

## 6. Real-time Visualization

Several callbacks provide immediate feedback during the optimization:

- **`LiveDisplay`**: Uses `rich` to show a live-updating dashboard with the current best score, iteration progress, and a live log of recent events.
- **`VerboseDisplay`**: Prints detailed per-iteration summaries to the console.
- **`LineageTracker`**: Generates a Mermaid.js lineage graph and a Markdown tree to visualize the evolutionary history of candidates.
- **`MemV0Observer`** (when `memory_version="v0"`): Prints and logs Memory V0 internals in real time (`memv0_trace.log`, `memv0_trace.jsonl`) with moderate Q/A redaction.

### Redaction policy

GEPA callback logging uses shared redaction helpers for question/answer-bearing fields (`Inputs`, `Generated Outputs`, `Problem`, `Answer`, `Question`) in high-risk text payloads.
This keeps diagnostic metadata visible while reducing sensitive content exposure in terminal and trace logs.

### Event correlation metadata

All callback events are enriched at dispatch with `_meta` fields for cross-sink correlation:
- `event_id`
- `event_type`
- `callback_method`
- `timestamp_ms`
- `dispatch_sequence`
- `callback_stream_id`

When present, `_meta.iteration` mirrors the event iteration for easier joins across `log.jsonl`, `states/`, and memory traces.

### Event correlation index

GEPA also writes a run-level index:
- `event_correlation_index.jsonl` — append-only mapping of `event_id` to artifact paths generated by `ResearchLogger` and `StateLogger`.

This enables one-hop lookup from an event ID in terminal or JSON logs to the relevant detailed artifacts in `states/` and `memory/`.

You can query this index using:

`uv run python tools/find_event_artifacts.py --run-dir <run_dir> --event-id <event_id>`

Example:

`uv run python tools/find_event_artifacts.py --run-dir outputs/smoke_event_index/seed0_memv0 --event-id 4900ccf2969a4a5eb8e51a269a62dab9-23`

### MemV0 sink parity artifacts

For `memory_version="v0"`, deep MemV0 events are now persisted beyond terminal traces:
- `memory/memv0_events.jsonl` via `ResearchLogger` (OI calls, eviction summaries, tree updates, memory renders).
- `states/iter_*_13_memv0_*.json` via `StateLogger` (ordered per-event MemV0 snapshots).

---

## 7. Configuration and Usage

Logging behavior is configured via arguments to `gepa.optimize()`:

```python
result = gepa.optimize(
    ...,
    run_dir="my_experiment",      # Directory for logs and checkpoints
    research_mode=True,           # Enable ResearchLogger (exhaustive logs)
    verbose=True,                 # Enable VerboseDisplay
    use_wandb=True,               # Log metrics to WandB
    use_mlflow=True,              # Log metrics to MLflow
    callbacks=[MyCustomCallback()] # Add user-defined callbacks
)
```

By combining these components, GEPA ensures that every decision made by the optimizer is transparent, reproducible, and easily analyzable.
