# Diff Diary — Full Accepted Diffs, No Length Signals, No Rejected Edits

## What Changed (from OPTIMIZATION_DIARY.md v1)

The original diary tracked prompt lengths, rejected edits, bloat warnings, and length efficiency metrics. It injected one-line heuristic summaries of changes (from `summarize_change()`). The smoke test revealed two problems:

1. **Summaries lose the actual change.** `"Added: 'step-by-step reasoning that rigorously justifi...'"` tells the reflector that *something* about step-by-step reasoning was added but truncates the actual content. The reflector can't learn from a diff it can't read.
2. **Rejected diffs and length metrics are noise.** The reflector ignored length warnings and bloat data. Rejected diffs steered toward avoidance rather than extension of working strategies.

## The Diff Diary (v2)

The diary injects **full unified diffs of accepted edits only** into the reflection prompt. No prompt length. No rejected edits. No summaries.

### What the reflector sees

```
== OPTIMIZATION CONTEXT ==
Progress: 10 iterations | 5 accepted (50%) | Best batch: 3/3

Edits that improved scores:
  --- iter 2 (+1.0 score) ---
--- before
+++ after
@@ -1 +1,5 @@
 Solve this math problem.
+Your solution must include clear, step-by-step reasoning that rigorously
+defines every variable, justifies every algebraic manipulation, and
+verifies the final answer by substitution or independent method.
+Show all intermediate steps.
  --- iter 9 (+1.0 score) ---
--- before
+++ after
@@ -4,3 +4,5 @@
 verifies the final answer by substitution or independent method.
 Show all intermediate steps.
+Fully simplify radicals and expressions before concluding.
+Confirm that the final answer matches the required format.

Strategy patterns:
  Precise verification steps and format-matching instructions improve acceptance.
```

### What was removed

- Prompt length trajectory, efficiency metric, bloat warning
- Rejected edits section
- Heuristic diff summaries — replaced with full unified diffs

### What stays

- Iteration count, acceptance count and rate
- Best batch score
- Last 5 accepted edits as full unified diffs with score deltas
- Layer 2 strategy notes (optional, LLM-curated, no length references)

## Why Full Diffs

The reflector is an LLM. It can read diffs. A unified diff tells it *exactly* what was added, removed, or changed in a successful mutation. This gives it concrete patterns to extend:

- "Adding verification steps worked" (from seeing `+Fully simplify radicals...`)
- "Adding format-matching instructions worked" (from seeing `+Confirm that the final answer...`)

With summaries, the reflector saw `"Added: 'Fully simplify radicals and express...'"` — truncated, decontextualized, useless for knowing *where* in the prompt it was added or what surrounded it.

## Hypothesis

**H1: Full diffs produce more targeted proposals than summaries.**

The reflector can see exactly what text was added and where. It can extend the pattern (add another verification step in the same style) rather than guessing from a truncated summary. This should produce proposals that are more consistent with the successful trajectory.

*Measure:* Compare the semantic coherence of proposed mutations — do they extend the patterns visible in the diffs, or do they add unrelated content? Also: acceptance rate >= 40%.

**H2: Removing length signals does not increase prompt bloat.**

The v1 smoke test showed length signals didn't prevent bloat (L1 alone grew to 5022 chars / 58×). Removing them costs nothing. The reflector won't waste tokens reasoning about prompt length.

*Expected:* Final prompt length comparable to v1 runs (~2000-5000 chars). Falsified if length exceeds 8000 chars.

**H3: Accepted-only context is sufficient for Layer 2 to learn useful strategy patterns.**

Layer 2 still sees both accepted and rejected iterations in its internal history (all entries are recorded). The *injected text* only shows accepted diffs. Layer 2 gets the full picture to curate notes from; the reflector gets only the positive signal.

*Expected:* Strategy notes are specific and directional, not generic. Verify by reading final diary snapshot.

**H4: Full diffs + no noise → better signal-to-noise ratio in the reflection prompt.**

v1 injected ~1200 chars of mixed signal (stats + summaries + rejections + bloat warning). v2 injects full diffs which are larger per-entry but contain only actionable signal. The reflector sees *what actually worked* in full detail rather than a noisy mix of truncated successes and failures.

*Expected:* Fewer "add everything" proposals, more surgical edits that extend the last successful diff.

## What Could Go Wrong

1. **Full diffs make injection too large.** If 5 accepted edits each have 500-char diffs, that's 2500+ chars injected. This could crowd out the actual task context. Mitigation: the window is 5 entries max; unified diffs are compact; the reflection prompt is already ~2000 chars before injection.

2. **Without negative signal, the reflector re-proposes rejected strategies.** Mitigation: Layer 2 strategy notes still capture failed patterns from its internal view of all entries (including rejected ones).

3. **Diffs anchor the reflector to incremental additions.** If all successful diffs are additions (`+` lines), the reflector may never try restructuring or removing content. Mitigation: this is arguably correct behavior — the reflector should follow what works.

## Experiment Design

Same 3 conditions:

1. **Baseline** — no memory (`--memory-version` omitted)
2. **Diary L1** — full accepted diffs only, no Layer 2 (`--memory-version diary`)
3. **Diary L1+L2** — full accepted diffs + LLM strategy notes (`--memory-version diary_full`)

All runs: seed 0, 500 max-calls, val-size 30, same solver/reflection LMs.

## Success Criteria

- At least one diary condition achieves val score > 0.60 (exceeds baseline range of 0.53-0.56)
- Acceptance rate >= 35% for diary conditions (vs baseline ~20%)
- No condition produces prompts > 8000 chars
- Layer 2 strategy notes are specific (not "try different approaches")
- Accepted diffs in the diary snapshot are full unified diffs, not truncated summaries
