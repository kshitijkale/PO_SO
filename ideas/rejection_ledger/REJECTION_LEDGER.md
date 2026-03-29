# Rejection Ledger — Design and Implementation

## Context: What We Tried and Why It Failed

**V1 (ReflectionMemory)** stored text diffs of past edits with scores. It was syntactic noise — the reflection LLM saw *what words changed* but not *why*, so it couldn't extract useful lessons from terse diff lines.

**V2 (Lesson-based memory)** added an LLM call to generate structured lessons after each step. The lesson LLM only received thin feedback strings ("incorrect, answer is 321"), never saw reasoning traces, and produced generic lessons. Underperformed baseline (50% vs 53.3%).

**MemV0 (Tree + Outcome Interpreter + Tiered Renderer)** went in the opposite direction: observe everything. An Outcome Interpreter LLM analyzed full reasoning traces per question, a tree stored the full prompt lineage with per-question observations, and a tiered renderer organized 10K+ chars of history by genealogical distance. Result: **worse than baseline** (0.5111 vs 0.5556 val, 11% vs 40% acceptance rate).

### Why MemV0 Failed (the experiment evidence)

The post-mortem revealed one dominant damage mechanism: **prompt bloat**.

| Metric | Baseline (no memory) | MemV0 |
|--------|---------------------|-------|
| Proposal length (median) | 727 chars | 2329 chars |
| Proposals > 2K chars | 1/20 (5%) | 24/38 (63%) |
| Acceptance rate | 40% | 11% |
| Best val score | 0.5556 | 0.5111 |
| max_tokens_exceeded (solver) | 0 | 32 |

The 10K-char memory injection, filled with detailed OI observations and rejection histories, primed the reflection LLM to write 3x longer system prompts. Longer prompts ate solver context window (32 solver truncation events), and the added instructions ("be careful about sign errors in geometry") were not actionable — they didn't change model behavior.

Secondary causes:
- OI observations were grounded in traces but at the wrong level of abstraction for prompt editing
- The tree + renderer complexity added cost (83 OI LLM calls) without ROI
- The rendered memory overwhelmed the reflection LLM, shifting its generation distribution toward verbose, cautious proposals

---

## The Rejection Ledger: Design Principles

The only information the reflection LLM needs to avoid repetition is: **what changes were already tried from this specific candidate, and how they scored**. Everything else is either not actionable or actively harmful.

### Per-parent, not global

A rejection recorded from candidate A says nothing about whether the same strategy would work from candidate B. Different base text, different minibatch, different failure modes in the reflective dataset. The ledger is keyed by SHA-256 hash of the parent candidate, so each prompt's history is isolated.

### Informational, not prescriptive

With batch size 3, a single rejection is weak evidence — a genuinely better prompt gets rejected ~40% of the time from minibatch noise. The ledger is injected as **soft context**, not hard rules. The header reads:

```
== PAST ATTEMPTS FROM THIS PROMPT (context only — use your judgment) ==
```

And the footer:

```
These attempts didn't help on past minibatches. They may or may not be relevant to the current one.
```

The reflection LLM sees the scores and judges the evidence strength itself:
- "scored 2/3, needed >2/3" = a near miss, maybe worth a variant
- "scored 0/3, needed >2/3" = clear signal to avoid this direction

This avoids **premature strategy rejection**, where a good direction gets blacklisted after one unlucky evaluation.

### Unbounded — no eviction

The ledger is unbounded per parent. Because it is per-parent (not global), growth is naturally contained — a candidate only accumulates entries while it is the selected parent, which is bounded by the evaluation budget. Dropping old entries would discard signal that cost real evaluation calls.

### LLM-generated summaries with full context

Each rejected entry is summarized by a **dedicated summarizer LM call** (separate from the reflection LLM). It receives:

- The old prompt text
- The new (rejected) prompt text
- The minibatch outputs the new prompt produced on each problem (answer + reasoning)
- The scores and threshold

This gives the summarizer the same signal a human reviewer would use: what changed, what the solver actually did with the new prompt, and how it scored. The output is a single sentence (max 150 chars), e.g.:

> "Tried adding step-by-step verification instructions but solver still made algebra errors on all three problems."

Falls back to a heuristic `difflib`-based summary if the LM call fails.

---

## What It Looks Like at Injection Time

```
== PAST ATTEMPTS FROM THIS PROMPT (context only — use your judgment) ==
• "Tried adding step-by-step verification — solver still made sign errors" — scored 1/3, needed >2/3
• "Restructured as numbered list — solver output format improved but answers unchanged" — scored 2/3, needed >2/3
• "Added geometry-specific hints — no improvement on algebra problems in minibatch" — scored 0/3, needed >2/3

These attempts didn't help on past minibatches. They may or may not be relevant to the current one.
```

Roughly 400–600 chars of injection — compare to MemV0's 10,358 chars.

### Natural exhaustion signal

When a candidate accumulates many entries that all scored 1–2/3, the reflection LLM sees the pattern — "many incremental changes have been tried, none broke through" — and can decide to try something more radical on its own. No need to hard-code strategy-switching logic. The ledger IS the signal.

---

## What It Deliberately Excludes

- **No per-question observations.** OI traces like "sign error in step 7" are accurate but not actionable via system prompt edits.
- **No tree structure.** Only the current candidate's rejection history matters.
- **No global summary.** "The model is bad at geometry" is not something the reflection LLM can act on by editing prompt text.
- **No written solutions in feedback.** The feedback passed to the reflector contains only the correct answer — never a step-by-step solution. The reflector must diagnose failure from the outcome signal and the solver's own reasoning trace.

---

## Implementation

| Component | File | What it does |
|---|---|---|
| `LedgerEntry` | `rejection_ledger.py` | Dataclass: `change_summary`, `score`, `threshold`, `batch_size`, `iteration` |
| `RejectionLedger` | `rejection_ledger.py` | `record()` appends (no eviction); `format_for_prompt()` renders bullet list with soft framing |
| `_summarize_rejection()` | `reflective_mutation.py` | Calls summarizer LM with old/new prompt + minibatch outputs; falls back to `summarize_change()` |
| `summarize_change()` | `memory.py` | Heuristic difflib fallback, no LLM call |
| `summarizer_lm` param | `ReflectiveMutationProposer` | Separate callable from `reflection_lm`; defaults to same model, can be overridden |

Activated via `--memory-version ledger` flag. When not set, no ledger is constructed and no summarizer calls are made.

---

## Open Questions

1. **Optimal injection size.** Currently unbounded per parent. If a single parent accumulates 20+ entries, the injection could grow large. May want a display cap (show last N) while keeping all entries stored.

2. **Should accepted changes be shown?** A note like "previously accepted: 'Added step-by-step structure'" gives positive signal. Not currently implemented.

3. **Summarizer model choice.** Currently defaults to the same model as the reflector. A cheaper/faster model (e.g. gpt-4.1-nano) could reduce cost — the summarization task is simple.

4. **Validation plan.** Run on same AIME setup (seed 0, 45/45 split, gpt-4.1-mini, 500 metric calls). Compare val score and acceptance rate against baseline (no memory) and MemV0. Bar: acceptance rate close to baseline (~40%), val score ≥ baseline (0.5556).
