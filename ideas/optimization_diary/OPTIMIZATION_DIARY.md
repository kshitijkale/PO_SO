# Optimization Diary — Making the Reflector Stateful

## Motivation

GEPA's reflector LLM is stateless: each iteration, it sees only the current prompt and a 3-example minibatch with feedback. It has no awareness of what has been tried before, what worked, what failed, or how the prompt has evolved over time. Three prior attempts to add statefulness all failed:

| Approach | What went wrong | Core mechanism |
|---|---|---|
| **ReflectionMemory (V1)** | Syntactic diffs gave no semantic signal | Text diffs of edits |
| **Lesson-based (V2)** | LLM only saw thin feedback, produced generic lessons | LLM-generated lessons |
| **MemV0 (Tree + OI)** | 10K char injection caused 3× prompt bloat, crashed acceptance rate from 40% → 11% | Full trace analysis + tree + renderer |
| **Rejection Ledger** | Negative-only signal, per-parent keying resets on accept, no bloat awareness | Per-parent rejection log |

### What the Ledger Experiments Showed

We ran 4 experiments (2 conditions × 2 feedback variants):

| | No solutions in feedback | With solutions in feedback |
|---|---|---|
| **Baseline** | val=0.5556, 20.6% accept | val=0.5333, 19.4% accept |
| **Ledger** | val=0.5333, 16.2% accept | val=0.5111, 15.0% accept |

The ledger consistently underperformed baseline by ~0.02 val and ~4pp acceptance rate. Three structural flaws caused this:

1. **Negative-only signal.** The ledger records what failed but never what succeeded. The reflector has no positive direction — it knows 8 things that didn't work but not which of the 3 things that *did* work are worth extending.

2. **Per-parent keying.** When a candidate is accepted, it gets a new hash. Its ledger is empty. All accumulated knowledge is lost. The reflector starts from scratch after every acceptance. This caused the ledger to plateau early (best found at iter 1-4, vs baseline's iter 26-27).

3. **No bloat awareness.** The dominant failure mode across ALL memory experiments is prompt bloat. The reflector's default behavior (from the template: "include all niche and domain specific factual information") is to add content. Prompts grew from 86 chars to 2000-3700 chars. The ledger didn't address this — it just told the reflector which specific additions failed, so it added *different* content instead.

---

## The Optimization Diary

A **global, fixed-size context block** that persists across the entire optimization run. Two layers:

### Layer 1: Heuristic Scaffold (no LLM calls)

Pure data computed from internal state. Always accurate, zero cost.

**What it tracks:**
- Iteration count, acceptance count and rate
- Prompt length trajectory: initial → current → peak
- Length efficiency: score gain per 100 chars of growth
- Last 5 accepted edits with diff summaries, score deltas, and length deltas
- Last 3 rejected edits with scores and length deltas
- A heuristic bloat warning when length > 3× initial with no recent improvement

### Layer 2: LLM Strategy Notes (one call per iteration, optional)

After each iteration, a summarizer LM reads the previous notes + latest outcome and produces updated notes (hard-capped at 400 chars).

**What it captures that heuristics can't:**
- Semantic categorization: "domain-specific formulas have been tried 4× without improvement"
- Pattern detection: "structural changes score better than content additions"
- Failure mode identification: "current prompt handles algebra well but fails on combinatorics"

---

## What Gets Injected

```
== OPTIMIZATION CONTEXT ==
Progress: 15 iterations | 4 accepted (27%) | Best batch: 8/3
Prompt: 86 chars (start) → 342 chars (now) — 4.0× growth
Efficiency: each +100 chars has yielded ~0.027 score gain

Edits that improved scores:
  • iter 3: "Added step-by-step instruction" → +1.0 batch score, 86→150 chars
  • iter 7: "Numbered reasoning steps" → +1.0 batch score, 150→280 chars
  • iter 11: "Added verify check" → +1.0 batch score, 280→342 chars

Recent failed edits:
  • iter 13: "Added geometry formulas" — scored 0/3, needed >2/3, 342→1140 chars
  • iter 14: "Rewrote as bullet list" — scored 1/3, needed >2/3, 342→320 chars
  • iter 15: "Added modular arithmetic tips" — scored 1/3, needed >2/3, 342→740 chars

⚠ Prompt has grown 4.0× with no recent improvement. Consider restructuring
  existing text rather than adding more content.

Strategy patterns:                               ← Layer 2 only
  Domain-specific formulas tried 4× without improvement. Structural changes
  (step-by-step, verify) have been more effective. Failures cluster on
  geometry and combinatorics.
```

Total: ~800-1000 chars, fixed upper bound regardless of iteration count.

---

## How It Differs From the Ledger

| | Rejection Ledger | Optimization Diary |
|---|---|---|
| **Signal** | Negative only (rejections) | Positive + negative (accepted edits + rejections) |
| **Scope** | Per-parent hash (resets on accept) | Global across entire run |
| **Size** | Unbounded (grows with rejections) | Fixed budget (~1000 chars max) |
| **Bloat awareness** | None | Explicit length trajectory + efficiency + warning |
| **Update cost** | 1 LLM call per rejection (summarizer) | Layer 1: zero. Layer 2: 1 LLM call per iteration |
| **Knowledge retention** | Lost on every acceptance | Persists for the full run |

---

## Research Questions

### RQ1: Is prompt bloat the primary damage mechanism of reflector statefulness?

**Answer: Yes.** All four memory approaches (V1, V2, MemV0, Ledger) produced longer prompts than baseline. MemV0 had median proposal length 2329 chars vs baseline's 727. The ledger experiments showed prompts growing from 86 to 2000-3700 chars. In every case, the longer prompts did not score proportionally better — the extra content was not actionable by the solver.

The causal chain: memory injection → reflector sees more context → reflector generates longer proposals (mimicking the verbosity of its input) → longer system prompts eat solver context window → solver performance degrades or stays flat → mutation rejected or marginal.

**Implication for design:** Any effective memory system must include explicit bloat countermeasures. Injecting information without controlling how the reflector uses it will always trigger the bloat feedback loop.

### RQ2: Does the reflector need positive signal (what worked) or is negative signal (what failed) sufficient?

**Answer: Negative signal alone is insufficient.** The ledger provided detailed rejection histories and the reflector still couldn't find productive directions. With batch size 3, a single rejection is noisy evidence. And "don't do X" doesn't tell you what to do instead — the search space is too large.

Positive signal (accepted edits with score deltas) gives the reflector a generative direction: "step-by-step instructions helped → maybe I should extend that pattern" vs "domain formulas failed → but what should I try?"

**Implication for design:** The diary must record both accepted AND rejected mutations, with clear labeling of what worked.

### RQ3: Should memory be per-parent or global?

**Answer: Global.** Per-parent keying was theoretically clean (rejection from parent A says nothing about parent B) but practically catastrophic — it meant every accepted candidate started with a blank slate. In a 40-iteration run with 7 acceptances, the ledger's useful content was reset 7 times.

Global memory trades some precision (an edit that failed on one parent might work on another) for continuity (the reflector sees the full optimization trajectory). The diary mitigates the precision loss by showing only recent entries and using soft framing.

**Implication for design:** The diary is a single global instance, not keyed by parent hash.

### RQ4: Can heuristic data alone (no LLM) provide enough signal, or are LLM-generated summaries necessary?

**This is an open question.** The experiment tests this directly by comparing Layer 1 (heuristic only) vs Layer 1+2 (heuristic + LLM notes). Heuristics can show "prompt grew 4×, each +100 chars yielded +0.02 score" — that's powerful. But they can't say "geometry problems are the main failure mode" or "structural changes work better than content additions."

**Hypothesis:** Layer 1 alone addresses the bloat problem (the #1 issue). Layer 2 adds value for directing the reflector toward productive strategies (a secondary issue). The marginal benefit of Layer 2 may be small if the optimization ceiling is low.

### RQ5: What is the optimization ceiling for system prompt optimization on AIME with gpt-4.1-mini?

**Answer: Approximately 0.53-0.56 val accuracy.** Across all experiments (baseline, ledger, MemV0), the best val scores clustered in this range. The seed prompt (86 chars) scores 0.47-0.53 depending on the random evaluation. The improvement from optimization is +0.02-0.07 — a narrow band.

**Implication:** No memory system can exceed the model's capability ceiling. The goal is not "dramatically better scores" but "matching baseline while being shorter" and "not degrading performance like previous memory systems did."

---

## Hypotheses

### H1: Bloat control — Diary prompts will be shorter than baseline

**Statement:** The best candidate's prompt length under the Diary L1 condition will be less than 1500 characters, compared to the baseline's typical 3000-3700 characters.

**Mechanism:** The diary's length trajectory line ("86 chars → 342 chars, 4.0× growth"), efficiency metric ("each +100 chars yielded +0.027 score"), and bloat warning ("prompt has grown 4× with no recent improvement") give the reflector explicit evidence that adding content has diminishing returns. This counterbalances the template's instruction to "include all niche domain info."

**Measurement:** `len(best_candidate["system_prompt"])` at optimization end.

**Falsification:** If Diary L1's best prompt ≥ 2000 chars, H1 is false — the bloat signal was not strong enough to overcome the reflector's default behavior.

**Expected outcome:** Best prompt in the 300-1200 char range. The reflector will still grow the prompt somewhat (it's instructed to add domain info), but the length awareness will moderate growth.

### H2: Acceptance rate recovery — Diary will match or exceed baseline

**Statement:** The Diary L1 condition will achieve an acceptance rate ≥ 18%, reversing the ledger's depression to 15-16% and approaching baseline's 19-21%.

**Mechanism:** Positive signal (accepted edits) gives the reflector a productive direction to build on, leading to more relevant mutations. Shorter prompts (from H1) mean fewer solver truncation events, so proposed prompts perform closer to expectation.

**Measurement:** `accepted_count / iteration_count` from summary.jsonl.

**Falsification:** If Diary L1 acceptance rate < 16%, H2 is false — the diary is constraining the reflector similarly to the ledger.

**Expected outcome:** 18-22% acceptance rate, comparable to baseline.

### H3: Sustained exploration — Diary will find improvements later in the run

**Statement:** The Diary conditions will produce their best candidate after iteration 10, unlike the ledger which peaked at iteration 1-4.

**Mechanism:** The ledger plateaued because per-parent keying reset knowledge on acceptance, making the reflector conservative after a few rejections. The diary's global scope means the reflector always has the full trajectory available, enabling it to build incrementally rather than making one early leap and stalling.

**Measurement:** Iteration number at which the best val score was first achieved, from pareto_timeline.jsonl.

**Falsification:** If Diary's best is found before iteration 5, H3 is false — the global diary doesn't improve exploration dynamics.

**Expected outcome:** Best found between iteration 10-30, similar to baseline (which found best at iter 26-27).

### H4: Val score parity — Diary will not degrade optimization quality

**Statement:** The Diary L1 condition will achieve a best val score ≥ 0.49 (within 0.04 of baseline's 0.53-0.56 range).

**Mechanism:** The diary is designed to be informational, not prescriptive. Its soft framing and bounded injection size avoid the problems that caused previous memory systems to hurt performance. If the bloat is controlled (H1) and acceptance rate is maintained (H2), the optimization trajectory should be at least as good as baseline.

**Measurement:** Best val score from pareto_timeline.jsonl.

**Falsification:** If Diary L1 best val < 0.47 (below typical seed scores), H4 is false — the diary is actively harmful.

**Expected outcome:** Val score in the 0.49-0.56 range. The diary's main contribution is not higher scores (the ceiling is too low) but achieving baseline-equivalent scores with shorter prompts.

### H5: Layer 2 additive value — LLM notes provide marginal benefit over heuristics alone

**Statement:** The Diary Full (L1+L2) condition will achieve a best val score ≥ Diary L1's best val score.

**Mechanism:** LLM strategy notes can identify semantic patterns (failure mode clustering, strategy categorization) that pure heuristics miss. This should help the reflector make more targeted mutations.

**Measurement:** Compare best val scores between Diary L1 and Diary Full conditions.

**Falsification:** If Diary Full's best val < Diary L1's best val - 0.02, H5 is false — the strategy notes are adding noise rather than signal.

**Expected outcome:** Diary Full ≥ Diary L1, but the delta may be small (< 0.02). The heuristic scaffold addresses the dominant problem (bloat); the LLM notes address a secondary problem (strategy direction). At the AIME optimization ceiling, there may not be enough room for L2 to show a clear effect.

---

## Experiment Design

### Conditions

| Condition | Flag | Memory | LLM calls/iter |
|---|---|---|---|
| Baseline | (none) | None | 0 |
| Diary L1 | `--memory-version diary` | Heuristic scaffold only | 0 |
| Diary Full | `--memory-version diary_full` | Heuristic + LLM strategy notes | +1 per iter |

### Shared parameters

```
--seed 0
--solver-temperature 1.0
--max-calls 500
--workers 32
--solver-lm openai/gpt-4.1-mini
--reflection-lm openai/gpt-4.1-mini
--train-size 45
--val-size 45
--minibatch-size 3
```

### Commands

```bash
# Condition 1: Baseline
uv run python -m experiments.aime_memory.run \
  --seed 0 --solver-temperature 1.0 --max-calls 500 --workers 32 \
  --output-dir outputs/aime_diary_exp

# Condition 2: Diary L1 (heuristic only)
uv run python -m experiments.aime_memory.run \
  --seed 0 --solver-temperature 1.0 --max-calls 500 --workers 32 \
  --memory-version diary \
  --output-dir outputs/aime_diary_exp

# Condition 3: Diary Full (heuristic + LLM notes)
uv run python -m experiments.aime_memory.run \
  --seed 0 --solver-temperature 1.0 --max-calls 500 --workers 32 \
  --memory-version diary_full \
  --output-dir outputs/aime_diary_exp
```

### Primary metrics (per condition)

| Metric | Source | Measures |
|---|---|---|
| Best val score | `pareto_timeline.jsonl` | Optimization quality (H4, H5) |
| Best prompt length (chars) | `candidates/` | Bloat control (H1) |
| Acceptance rate | `summary.jsonl` | Mutation quality (H2) |
| Iteration of best | `pareto_timeline.jsonl` | Exploration dynamics (H3) |
| Prompt length trajectory | `candidates/` over iterations | Bloat over time |
| Total iterations | `summary.jsonl` | Efficiency |

### Secondary diagnostics

| Diagnostic | Source | What it reveals |
|---|---|---|
| Diary injection text per iteration | `diary/iter_NNN_*.txt` | What the reflector actually saw |
| Proposed prompt lengths over time | `llm_calls/` | Whether bloat warning is working |
| Strategy notes evolution (Full only) | `diary/` snapshots | Whether L2 captures useful patterns |
| Solver truncation events | `max_tokens_exceeded.jsonl` | Whether shorter prompts reduce truncations |

### Success criteria

The experiment succeeds (diary is worth keeping) if **any** of:
1. Diary L1 best val ≥ baseline best val AND best prompt < 1500 chars (same quality, less bloat)
2. Diary L1 best val > baseline best val (better quality at any length)
3. Diary L1 acceptance rate > ledger acceptance rate by ≥ 3pp (measurable improvement over previous memory approach)

The experiment succeeds for Layer 2 specifically if:
4. Diary Full best val > Diary L1 best val (LLM notes add value)

### Failure modes to watch for

1. **Diary itself causes bloat.** The ~1000 char diary injection could trigger the same problem it's trying to solve — the reflector sees more input and generates longer output. Monitor proposed prompt lengths in iter 1-5 vs baseline.

2. **Bloat warning is too aggressive.** If the warning fires early and the reflector over-corrects (making prompts too short/simple), scores could drop below baseline. Watch for prompts that shrink below 50 chars.

3. **Global scope confuses the reflector.** An accepted edit from parent A may not apply to parent B. The reflector might try to replicate an edit that worked in a different context. Watch for acceptance rate dropping after parent switches.

4. **Layer 2 strategy notes hallucinate.** The summarizer LM might claim patterns that don't exist ("geometry is the main failure mode" when the last batch was all algebra). Watch for strategy notes that contradict the heuristic data.

---

## Implementation Summary

| File | Change | Lines |
|---|---|---|
| `src/gepa/core/callbacks.py` | Add `DiaryInjectedEvent`, `on_diary_injected` | ~15 |
| `src/gepa/proposer/reflective_mutation/optimization_diary.py` | New file: `DiaryEntry`, `OptimizationDiary` | ~200 |
| `src/gepa/proposer/reflective_mutation/reflective_mutation.py` | Add `optimization_diary` param, injection + recording | ~40 |
| `src/gepa/api.py` | Expand `memory_version`, construct diary | ~15 |
| `experiments/aime_memory/run.py` | Add `diary`, `diary_full` to CLI choices | ~2 |
| Observability callbacks | Add `on_diary_injected` handlers | ~30 |
| `tests/test_optimization_diary.py` | New test file, 14 tests | ~200 |


 It's a reasonable trade-off but has a specific downside for this experiment.

  The math with val-size=30:

  With 30-example val set and ~20% acceptance rate:
  - Rejected iteration: ~6 calls (3 eval current + 3 eval proposed)
  - Accepted iteration: ~36 calls (3 + 3 + 30 val)

  At 250 calls: roughly 18-20 iterations total.
  At 500 calls: roughly 38-40 iterations total.

  What 250 calls can and can't test:

  ┌──────────────────────────────┬──────────────────────────┬────────────────────────────────────────────────────────────┐
  │          Hypothesis          │  250 calls sufficient?   │                            Why                             │
  ├──────────────────────────────┼──────────────────────────┼────────────────────────────────────────────────────────────┤
  │ H1: Diary prompts shorter    │ ✅ Visible by iter 5-10  │ Bloat happens fast                                         │
  ├──────────────────────────────┼──────────────────────────┼────────────────────────────────────────────────────────────┤
  │ H2: Acceptance rate ≥ 18%    │ ✅ Noisy but directional │ 20 iterations gives ~15-20% precision                      │
  ├──────────────────────────────┼──────────────────────────┼────────────────────────────────────────────────────────────┤
  │ H3: Best found after iter 10 │ ❌ Barely                │ With ~20 iterations, "after iter 10" is only the back half │
  ├──────────────────────────────┼──────────────────────────┼────────────────────────────────────────────────────────────┤
  │ H4: Val score not degraded   │ ⚠️  Weak signal           │ Prior baseline found best at iter 26-27                    │
  ├──────────────────────────────┼──────────────────────────┼────────────────────────────────────────────────────────────┤
  │ H5: L2 adds over L1          │ ❌ Insufficient          │ Need the full trajectory to separate                       │
  └──────────────────────────────┴──────────────────────────┴────────────────────────────────────────────────────────────┘

  The core problem: The baseline previously found its best prompt at iteration 26-27. At 250 calls, you'd cut off at ~iteration 20. You'd be comparing an
  incomplete baseline run against an incomplete diary run — both potentially before they've found their best.

  Recommendation: Run at 250 as a smoke test first — verify the diary injection is appearing correctly, check prompt lengths are shorter by iter 5, confirm no
   crashes. Then run at 500 for the real comparison. The 250-call smoke test catches bugs cheaply before you commit the full budget.
