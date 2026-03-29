# MemV0 Plan

## Motivation

V2 showed that post-hoc lessons generated from thin feedback ("incorrect, answer is 321")
produce generic, unhelpful memory entries. The lesson LLM never sees the reasoning traces
and can only say "it didn't work." Meanwhile, the reflection LLM has full traces but
doesn't record what it observed. The information is available but destroyed at the wrong
stage.

MemV0 takes a step back: instead of trying to summarize what happened after the fact,
observe what happened at evaluation time, per question, with full context.

---

## Core Idea: Outcome Interpreter

The **Outcome Interpreter** is a new LLM call that runs per question after evaluation.
It sees:

- The full prompt that was used
- The question
- The model's full reasoning trace (chain-of-thought)
- The model's output (predicted answer)
- The correct answer
- The feedback (correct/incorrect)

It produces a **description of what happened** — grounded in the actual reasoning, not
inferred from score deltas. For example: "The model set up the coordinate system correctly
but made a sign error when computing the cross product in step 7, leading to an incorrect
area of 976 instead of 751."

This is observation, not interpretation. The Outcome Interpreter does not generate lessons
or advice — it records what the model actually did.

**Output format** (starting point, can be revised after first experiment):

```
Question: [brief identifier or first line]
Result: correct/incorrect
Observation: [1-3 sentences grounded in reasoning trace]
Error type (if incorrect): arithmetic | logic | setup | interpretation | none
```

**Cost optimization:** Batch the OI call — one call per evaluation with all 3 questions
passed together, not 3 separate calls. This gives 2 OI calls per iteration instead of 6.

---

## Iteration Flow

```
1. Select candidate prompt P_A from Pareto front
2. Sample minibatch M (3 questions)
3. Run P_A on M → get (question, reasoning_trace, output, correct_answer) per example
4. Outcome Interpreter (batched): takes P_A + all (Q, reasoning, output, answer, feedback)
   → produces per-question outcome descriptions → writes to P_A's node in tree
5. Feedback (correct/incorrect) → build reflective dataset
6. Reflection LLM sees: reflective dataset (current) + rendered memory (historical)
   → proposes new prompt P_B
7. Run P_B on same minibatch M
8. Compare scores → accept/reject P_B
9. Run Outcome Interpreter on P_B's results → write to P_B's node (always, regardless of accept/reject)
10. Return proposal to engine
```

Key differences from V2:
- The Outcome Interpreter runs on BOTH P_A and P_B every iteration — rejected candidates carry the most important lessons (why a direction failed at the reasoning level)
- Memory is populated with per-question observations, not per-iteration lessons
- The reflection LLM reads memory but does not write to it
- Rejected prompts are stored as nodes in the tree with full OI outcomes, marked `accepted=False`

---

## Memory Structure: A Tree

Memory is a **tree**, not a flat list. The tree stores everything verbatim — full prompts,
full outcomes, all scores. Storage is cheap. **Rendering** (what the reflection LLM sees)
is a separate concern handled by the tiered renderer.

### Nodes = Prompt Candidates

Each prompt candidate is stored **exactly once** as a node. A node contains:

- **prompt_text**: the full prompt (stored once, rendered as diff when appropriate)
- **val_score**: validation score (if accepted and evaluated on val set)
- **accepted**: whether this candidate was accepted into the population
- **outcomes**: per-question OI descriptions, grouped by minibatch
- **outcome_summary**: compressed summary of outcomes (produced by eviction)

When the same prompt is evaluated on a different minibatch (e.g., the Pareto selector
picks it again), we do not duplicate the node — we add the new per-question outcomes
to the existing node's data.

```
Node: P_A (prompt text stored once, val_score=0.52, accepted=True)
  ├── Outcomes on minibatch M1: [outcome_q1, outcome_q2, outcome_q3]
  ├── Outcomes on minibatch M2: [outcome_q4, outcome_q5, outcome_q6]
  └── outcome_summary: "Strong on algebra, weak on geometry sign errors"
```

### Edges = Transitions Between Prompts

An edge represents: "P_A was evaluated on minibatch M, the reflection LLM saw those
outcomes and the memory, and proposed P_B."

```
P_A --[minibatch M, accepted=True]--> P_B
P_A --[minibatch M, accepted=False]--> P_rejected
```

The edge connects a parent prompt to a child prompt and records which minibatch triggered
the transition and whether the child was accepted. Per-question outcomes for both P_A
and P_B on that minibatch are stored on their respective nodes (not on the edge).

### Tree Structure

Since each prompt has exactly one parent (the prompt it was mutated from), the structure
is a tree rooted at the seed prompt. Rejected prompts are leaf nodes marked as rejected:

```
P_seed (val=0.45)
├── P_1 (val=0.48, accepted)
│   ├── P_3 (val=0.51, accepted)
│   │   └── P_7 (rejected)
│   └── P_4 (val=0.50, accepted)
├── P_2 (rejected)
└── P_5 (val=0.49, accepted)
    └── P_6 (val=0.52, accepted)
```

---

## Tiered Memory Rendering

The tree stores everything. The **renderer** decides what the reflection LLM sees.
The principle: **granularity decreases with genealogical distance from the current
candidate.** Not temporal distance — a grandparent prompt is more relevant than an
unrelated prompt tried 2 iterations ago.

### Ring 0 — Current Candidate (full verbatim, ~0 extra tokens)

- P_A full prompt text
- Current minibatch evaluation results (the existing reflective dataset)
- This is what the system already provides — not part of memory rendering

### Ring 1 — Immediate Family (high detail, ~1500 token budget)

- **Parent of P_A**: full prompt text + diff that created P_A + val score
- **Siblings of P_A** (other children of same parent): diff from parent + val score
  (if accepted) or "rejected" + brief reason
- **Rejected proposals from P_A** (mutations tried from P_A that failed): diff + what
  went wrong

This is the most valuable ring. It tells the reflector what was tried nearby. Since
prompt changes are small, each diff is ~20-50 tokens — 5-6 siblings fit easily.

### Ring 2 — Ancestry Chain (medium detail, ~500 token budget)

- Each ancestor from seed to P_A's grandparent: just the **diff** and the **val score**
- No per-question outcomes, no full prompt text
- Format: `Seed (val 0.45) → "Added step-by-step" (val 0.48) → "Added verify" (val 0.51) → current`

This gives the trajectory of improvements. If the tree is 10 deep, this costs ~200-300
tokens.

### Ring 3 — Other Branches (low detail, ~500 token budget)

- One-line summary per branch not in P_A's ancestry
- Format: `"Branch from P_2: tried decomposition approach, reached val=0.50, stalled after 3 iters"`
- Prevents re-exploring dead directions

### Ring 4 — Compressed History (running summary, ~300 token budget)

- The eviction summary from old/distant outcomes
- Global patterns: "Verification steps help geometry but hurt combinatorics. Prompts
  over 500 chars tend to regress."

### Rendering Example

What the reflection LLM sees in its memory section:

```
== OPTIMIZATION HISTORY ==

[Global patterns]
Early iterations: step-by-step instructions reached val ~0.48. Adding verification
improved to ~0.51. Explicit "show your work" format had mixed results.

[How we got here]
Seed (val 0.45) → "Added step-by-step instruction" (val 0.48)
→ "Added verification step" (val 0.51) → current P_A (val 0.52)

[Parent prompt — full text]
<parent prompt text>

[What changed to get current prompt]
Added: "After solving, verify your answer by substituting back into the original equation."

[Other attempts from same parent]
• REJECTED (1/3 vs 2/3): Added "bullet points format"
  Outcomes: Model broke down steps correctly but lost track of intermediate values on multi-step problems.
• REJECTED (1/3 vs 2/3): Added "domain-specific hints"
  Outcomes: Hints helped on algebra setup but caused over-reliance — model stopped verifying own work.

[Rejected attempts from current prompt]
• REJECTED (1/3 vs 2/3): Added "worked examples"
  Outcomes: Model mimicked example structure but applied wrong method to combinatorics problems.

[Other exploration branches]
• From seed: "Persona-based prompt" branch, peaked val 0.46, abandoned
• From P_1: "Few-shot examples" branch, peaked val 0.49, 2 iterations
```

### Rendering Implementation

```python
def render_memory_for_reflection(tree, current_node_id, token_budget=3000):
    sections = []
    sections.append(render_ring4_summary(tree))                        # ~300 tokens
    sections.append(render_ring2_ancestry(tree, current_node_id))      # ~500 tokens
    sections.append(render_ring1_family(tree, current_node_id))        # ~1500 tokens
    sections.append(render_ring3_branches(tree, current_node_id))      # ~500 tokens
    # Ring 0 is the existing reflective dataset, not part of memory
    return assemble_within_budget(sections, token_budget)
```

The renderer is where granularity is tuned — the tree itself never changes. If
experiments show siblings matter more than ancestry, reallocate budget. If ring 3
is useless, shrink it.

### Key Insight: Diffs as the Native Unit

Since prompt changes are small and incremental, **diffs are the natural rendering unit**.
A 200-token prompt with a 30-token diff means 10 historical diffs cost the same as 1.5
full prompts. The tree stores full prompts (for correctness), but the renderer shows
diffs (for efficiency). Only the current prompt and its parent are shown in full.

---

## Eviction: Per-Node Outcome Summarization

Outcome descriptions accumulate on tree nodes. Eviction operates **per-node**, not
globally — this preserves the tree structure instead of creating a disconnected global
buffer.

### Per-Node Eviction

When a node's outcome list exceeds a threshold (e.g., 2k outcomes):

1. Take the **oldest k** outcome descriptions from that node.
2. Summarize them via LLM together with the node's existing `outcome_summary`:
   `old_summary + oldest_k_outcomes → new_summary`
3. Remove the oldest k outcomes from the node, store the updated summary.
4. The summary lives on the node and persists.

This means every node always has: its full prompt, a summary of all past outcomes,
and its most recent verbatim outcomes. The renderer can choose which to show based
on the node's ring.

### Global Running Summary

Separately, a **global running summary** (Ring 4) is maintained. When the per-node
eviction fires, the evicted observations also feed into the global summary update.
This captures cross-node patterns ("geometry problems are consistently hard") that
per-node summaries might miss.

---

## Relationship to Existing Reflective Dataset

The current system builds a reflective dataset (Inputs / Generated Outputs / Feedback)
that the reflection LLM sees. Memory does not replace this — they serve different roles:

- **Reflective dataset** = what's happening NOW (current iteration's failures)
- **Memory** = what happened BEFORE (historical context from the tree)

They are injected as separate sections in the reflection prompt:

```
<current_evaluation>
[existing reflective dataset — unchanged]
</current_evaluation>

<optimization_history>
[rendered memory — from tiered renderer]
</optimization_history>
```

---

## Open Research Questions

These need to be figured out through experimentation:

1. **Ring budget allocation.** The proposed split (1500/500/500/300) is a starting guess.
   Which rings actually help? Does the reflector use branch summaries or ignore them?

2. **Outcome Interpreter output format.** The structured format (observation + error type)
   is a starting point. Does free-form text work better? Does error categorization help
   the renderer aggregate outcomes, or is it noise?

3. **Diff rendering.** How should diffs be shown? Unified diff format? Natural language
   ("Added: ..., Removed: ...")? Side-by-side? The reflection LLM needs to parse these
   quickly.

4. **Sibling vs. ancestry importance.** Siblings tell you "what was tried from the same
   starting point." Ancestry tells you "how we got here." Which matters more for avoiding
   repetition and guiding the next step?

take this GEPA code and add logging and priniting. i want to see every   
  question every LLM attemp every input given to OI every output of OI      
  every state of memory every Input given to reflection LLM every memory    
  injected into reflection LLM  