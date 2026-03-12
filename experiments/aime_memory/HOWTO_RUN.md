# AIME Memory Experiment — How to Run

## Overview

Two conditions, paired across seeds:

| Condition | Flag | Output dir |
|-----------|------|------------|
| Baseline GEPA | *(none)* | `outputs/aime_memory/seed{N}_memory_off/` |
| GEPA + Reflection Memory | `--memory` | `outputs/aime_memory/seed{N}_memory_on/` |

Each run: optimize a system prompt on AIME train set → evaluate best prompt on AIME 2025 test set.

---

## Prerequisites

```bash
# 1. Install dependencies
uv sync --extra dev
uv pip install dspy

# 2. Set API keys
export OPENAI_API_KEY=sk-...
```

---

## Single Run (for debugging)

```bash
# Baseline
uv run python -m experiments.aime_memory.run \
  --seed 0 \
  --max-calls 100 \
  --workers 8

# With memory
uv run python -m experiments.aime_memory.run \
  --seed 0 \
  --max-calls 100 \
  --workers 8 \
  --memory
```

Outputs are written to `outputs/aime_memory/seed0_memory_off/` and
`outputs/aime_memory/seed0_memory_on/` respectively.

---

## Full 5-Seed Study (production)

Run each seed sequentially. Each seed produces one baseline run and one memory run.

```bash
for seed in 0 1 2 3 4; do
  echo "=== Seed $seed — baseline ==="
  uv run python -m experiments.aime_memory.run \
    --seed $seed \
    --max-calls 500 \
    --reflection-lm openai/gpt-4.1 \
    --solver-lm gpt-4.1-mini \
    --workers 32

  echo "=== Seed $seed — memory ==="
  uv run python -m experiments.aime_memory.run \
    --seed $seed \
    --max-calls 500 \
    --reflection-lm openai/gpt-4.1 \
    --solver-lm gpt-4.1-mini \
    --workers 32 \
    --memory
done
```

Or run both conditions for one seed in parallel (requires two terminals / tmux panes):

```bash
# Terminal 1
uv run python -m experiments.aime_memory.run --seed 0 --max-calls 500 --workers 16

# Terminal 2
uv run python -m experiments.aime_memory.run --seed 0 --max-calls 500 --workers 16 --memory
```

---

## All CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--seed` | `0` | Random seed for reproducibility |
| `--memory` | off | Enable reflection memory |
| `--max-calls` | `500` | Total LLM evaluation budget |
| `--reflection-lm` | `openai/gpt-4.1` | Model used by the reflection LLM to propose mutations |
| `--solver-lm` | `gpt-4.1-mini` | Model used to solve AIME problems |
| `--output-dir` | `outputs/aime_memory` | Root directory for all run artifacts |
| `--workers` | `32` | Parallel evaluation workers |
| `--memory-entries` | `10` | Max entries kept in the reflection memory window |

---

## Dataset

- **Train / Val**: `AI-MO/aimo-validation-aime` — shuffled (seed=0), split 50/50
- **Test**: `MathArena/aime_2025` × 5 passes (repeated for stable estimate)

Downloaded automatically from HuggingFace on first run. Cached by the `datasets` library.

---

## What Each Run Produces

```
outputs/aime_memory/seed0_memory_off/
├── best_candidate.json      # best prompt found
├── optimization_history/    # all candidates evaluated
└── ...

outputs/aime_memory/seed0_memory_on/
├── best_candidate.json
├── optimization_history/
└── ...
```

At the end of each run, the script prints:

```
Baseline  : 12.00%
Optimized : 18.00%
Delta     : +6.00%
```

---

## Collecting Results

After all 10 runs, collect the final test scores and compute mean ± std for each condition:

```python
import json, glob, numpy as np

for condition in ["memory_off", "memory_on"]:
    scores = []
    for path in sorted(glob.glob(f"outputs/aime_memory/seed*_{condition}/best_candidate.json")):
        with open(path) as f:
            scores.append(json.load(f)["test_score"])
    print(f"{condition}: {np.mean(scores):.2%} ± {np.std(scores):.2%}  (n={len(scores)})")
```

> Note: the run script prints final test scores to stdout. If you don't store them in JSON,
> simply grep the run logs: `grep "Delta" outputs/aime_memory/seed*_memory_off/run.log`

---

## Recommended Model Choices

| Role | Cheap / Fast | Better |
|------|-------------|--------|
| Solver | `gpt-4.1-mini` | `gpt-4.1` |
| Reflection | `openai/gpt-4.1` | `openai/gpt-4.1` (default is fine) |

The solver LM is called ~500 × (budget) times. Keep it cheap.
The reflection LM is called ~1× per iteration (much less frequent). Use a stronger model.
