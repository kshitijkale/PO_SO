# Project Guidelines

## Code Style
- Python: 3.10+, 4-space indent, double quotes, line length 120 (see `pyproject.toml`).
- Lint/format with Ruff; run formatting only on touched files when possible.
- No relative imports in `src/` (`ban-relative-imports = "all"`).
- Keep typed event payloads explicit using `TypedDict` patterns from `src/gepa/core/callbacks.py`.
- Follow existing naming and dataclass-first style in MemV0 modules (`memory_tree.py`, `outcome_interpreter.py`, `memory_renderer.py`).

## Architecture
- Main API: `gepa.optimize()` in `src/gepa/api.py`; this wires adapters, proposer(s), callbacks, and engine.
- Core loop lives in `src/gepa/core/engine.py`; state and cache behavior live in `src/gepa/core/state.py`.
- Reflective mutation path is implemented in `src/gepa/proposer/reflective_mutation/reflective_mutation.py`.
- Adapter boundary is strict (`src/gepa/core/adapter.py`): evaluation via `evaluate(...)`, reflection data via `make_reflective_dataset(...)`.
- MemV0 path (`memory_version="v0"`) uses `MemoryTree + OutcomeInterpreter + TieredMemoryRenderer` and emits dedicated callback events.

## Build and Test
- Always use `uv`:
  - `uv sync --extra dev`
  - `uv run pytest`
  - `uv run pytest tests/test_outcome_interpreter.py -v`
  - `RECORD_TESTS=true uv run pytest`
  - `uv run ruff check src/`
  - `uv run ruff format src/`
  - `uv run pyright`
  - `uv run pre-commit run`
- For MemV0 smoke runs, use experiment entry points in `experiments/aime_memory/run.py` and `run_verbose.py`.

## Project Conventions
- Keep this file (`AGENTS.md`) as the primary agent instruction file; treat `CLAUDE.md` as extended reference.
- Do not bypass callback plumbing: new observability should go through `notify_callbacks(...)`.
- Keep callback payloads serializable and consistent with `GEPACallback` method names.
- In `ReflectiveMutationProposer`, preserve the two evaluation paths:
  - cached non-trace path for normal mutation scoring,
  - trace-capturing path when MemV0/OI needs reflective records.
- Outcome Interpreter must never raise to callers; it falls back on parse/LM failure.
- Tests use deterministic mocks for unit behavior and record/replay for live LLM tests (`tests/conftest.py`, `RECORD_TESTS=true`).

## Integration Points
- External LLM access is via LiteLLM callables (`reflection_lm`, `lesson_lm`, `oi_lm`) and adapter-specific model clients.
- `optimize_anything` (`src/gepa/optimize_anything.py`) shares core engine/proposer wiring; keep API parity when adding cross-cutting options.
- Research callbacks are in `src/gepa/callbacks/`; MemV0 observer output files are `memv0_trace.log` and `memv0_trace.jsonl`.
- MemV0 event types are declared in `src/gepa/core/callbacks.py` (`OutcomeInterpreterCallEvent`, `MemoryTreeUpdatedEvent`, `MemoryRenderedEvent`).

## Security
- Never hardcode secrets; use environment variables (e.g., `OPENAI_API_KEY`) and local dotenv loading in experiments.
- `.claude/.env` loading exists in AIME experiment scripts; keep it confined to experiments and never library code.
- Treat logs as sensitive: MemV0 observer and proposal traces can include full prompts, model outputs, and evaluation content.
- Avoid printing/storing credentials or raw private dataset content in new callbacks.
