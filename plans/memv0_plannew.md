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

---

## Iteration Flow

```
1. Select candidate prompt P_A from Pareto front
2. Sample minibatch M (3 questions)
3. Run P_A on M → get (question, reasoning_trace, output, correct_answer) per example
4. Outcome Interpreter (per question): takes P_A, Q, reasoning, output, answer, feedback
   → produces per-question outcome description → writes to memory
5. Feedback (correct/incorrect) → build reflective dataset
6. Reflection LLM (has access to memory) → proposes new prompt P_B
7. Run P_B on same minibatch M
8. Outcome Interpreter again (per question) on P_B's results → writes to memory
9. Compare scores → accept/reject P_B
10. Return proposal to engine
```

Key differences from V2:
- The Outcome Interpreter runs TWICE per iteration (on both old and new prompt evaluations)
- It runs AFTER feedback so it has the correctness signal
- Memory is populated with per-question observations, not per-iteration lessons
- The reflection LLM reads memory but does not write to it

---

## Memory Structure: A Tree

Memory is a **tree**, not a flat list.

### Nodes = Prompt Candidates

Each prompt candidate is stored **exactly once** as a node. When the same prompt is
evaluated on a different minibatch (e.g., the Pareto selector picks it again), we do
not duplicate the node — we add the new per-question outcomes to the existing node's
data.

So a prompt node accumulates observations across multiple minibatches over time:

```
Node: P_A (prompt text stored once)
  ├── Outcomes on minibatch M1: [outcome_q1, outcome_q2, outcome_q3]
  ├── Outcomes on minibatch M2: [outcome_q4, outcome_q5, outcome_q6]
  └── ...
```

### Edges = Transitions Between Prompts

An edge represents: "P_A was evaluated on minibatch M, the reflection LLM saw those
outcomes and the memory, and proposed P_B."

```
P_A --[minibatch M]--> P_B
```

The edge connects a parent prompt to a child prompt and records which minibatch triggered
the transition. The per-question outcomes for both P_A and P_B on that minibatch are
stored on their respective nodes (not on the edge).

### Tree Structure

Since each prompt has exactly one parent (the prompt it was mutated from), the structure
is a tree rooted at the seed prompt:

```
P_seed
├── P_1 (proposed when P_seed was evaluated on M1)
│   ├── P_3 (proposed when P_1 was evaluated on M4)
│   └── P_4 (proposed when P_1 was evaluated on M6)
├── P_2 (proposed when P_seed was evaluated on M2)
└── P_5 (proposed when P_seed was evaluated on M8)
    └── P_6 (proposed when P_5 was evaluated on M9)
```

This captures the full lineage: how prompt A went to B when minibatch X was present,
and how prompt A went to C when minibatch Y was present.

---

## Eviction: 2k → k Summarization

Per-question outcome descriptions accumulate fast (6 per iteration: 3 questions x 2
evaluations). The buffer uses a **2k → k** strategy:

1. A buffer of size 2k fills with outcome descriptions.
2. When full, take the **oldest k** entries.
3. Summarize them via an LLM call together with the existing running summary:
   `old_summary + oldest_k_entries → new_summary`
4. Remove the oldest k entries from the buffer, freeing k slots.
5. The summary is stored **separately** from the buffer and persists.

The summary accumulates the distilled wisdom from all evicted observations. Each
consolidation integrates new observations into the existing summary, so nothing is
fully lost — just compressed.

---

## Open Research Questions

These need to be figured out through experimentation:

1. **What lives in the 2k buffer?** The per-question outcome descriptions? The full
   node data? Just the outcomes from recent iterations? The granularity of what we
   evict vs. keep verbatim affects how much signal is preserved.

2. **What does the reflection LLM see from memory?** Options include: the full tree,
   just the ancestry path of the current candidate, the running summary + recent buffer,
   or some formatted subset. This determines how memory is rendered into the prompt.

3. **How much of the tree does the reflection LLM need?** The full ancestry of the
   candidate it's currently mutating? Just the immediate parent edge? All siblings
   (to see what was tried from the same parent)? The answer likely depends on how much
   prompt budget we can afford.

4. **What is the right output format for the Outcome Interpreter?** Free-form text?
   Structured JSON with fields (error_type, error_location, problem_domain)? A mix?
   This affects both storage efficiency and how useful the observations are downstream.

5. **Should rejected prompts (proposals that scored worse) still be nodes in the tree?**
   They carry useful negative signal ("this direction didn't work") but could clutter
   the tree. Possibly: store them as nodes but mark them, so the reflection LLM can
   see failed branches.
