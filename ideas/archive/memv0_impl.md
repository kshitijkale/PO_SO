# MemV0 Implementation Plan

Reference design: `plans/memv0_plan.md`

---

## Decision Log

Decisions made here shape the code. Each is explained with alternatives considered.

### D1: Candidate-to-node lookup via content hash

The engine assigns candidate IDs inside `GEPAEngine` after the proposer returns.
The proposer never learns the engine's ID for a newly proposed candidate. So the
tree cannot use engine IDs as keys.

**Decision:** Hash the candidate dict content (`sorted(candidate.items())` → sha256
truncated to 16 chars). The proposer maintains `_candidate_hash → tree_node_id`.
When a candidate is selected from the Pareto front, hash it and look up its node.

**Why not engine IDs:** The proposer creates P_B and adds it to the tree before
returning to the engine. The engine assigns the ID afterward. There's no callback
from the engine back to the proposer with the assigned ID.

**Risk:** Hash collision on two different candidates with identical content. In
practice, if two candidates have identical text, they ARE the same prompt, so this
is correct behavior (they map to the same node).

### D2: P_B evaluation uses adapter.evaluate directly (not cached_evaluate_full)

Currently P_B is evaluated via `state.cached_evaluate_full(new_candidate, ...)` with
`capture_traces=False`. For OI on P_B, we need traces.

**Decision:** When MemV0 is active, evaluate P_B via `self.adapter.evaluate(minibatch,
new_candidate, capture_traces=True)` directly. Then manually update the cache and
increment eval count.

**Why not re-evaluate after acceptance:** That wastes solver calls. The AIME adapter
already computes trajectories internally — `capture_traces` only controls whether
they're returned. Zero extra cost.

**Why not always change the evaluation path:** When MemV0 is off, the existing
`cached_evaluate_full` path is preserved. The change is gated behind `if self.memory_tree`.

### D3: OI runs on ALL P_B candidates — accepted AND rejected

Rejected candidates are the most important lessons. They tell the reflector exactly
what direction failed and WHY it failed at the reasoning level. Without OI on rejected
candidates, Ring 1 can only say "scored worse: 1/3 vs 2/3" — with OI, it can say
"tried adding verification step, model used it correctly on algebra but introduced a
new sign error on geometry problems." This is the whole point of the Outcome Interpreter.

**Decision:** OI always runs on P_B, unconditionally. The `accepted` flag only
controls whether the node contributes to the Pareto front — not whether it gets outcomes.

**Cost:** 2 OI calls per iteration always (one for P_A, one for P_B). This is fixed
and predictable, not conditional.

### D4: OI is batched — one call per evaluation, not per question

3 questions batched into 1 OI call. Produces 3 structured outcome descriptions.
Cost: 2 OI calls per iteration (one for P_A, one for P_B — both always).

### D5: Seed node is created lazily on first propose() call

The proposer doesn't have the seed candidate at `__init__` time. On the first call
to `propose()`, when the tree is empty and the selected candidate has no matching
node, it's added as the root.

### D6: Memory rendering replaces V2 memory injection at the same point

Currently V2 memory is injected in `propose_new_texts()` by appending to the prompt
template. MemV0 rendering uses the exact same injection point — just the content
source changes from `reflection_memory.format_for_prompt()` to
`memory_renderer.render(tree, node_id)`.

### D7: Single LLM for OI + eviction + global summary

One LLM parameter (`oi_lm`) handles all three. Defaults to the reflection LLM.
Keeping a single LLM simplifies configuration. Can be overridden for cost optimization
(e.g., use a cheaper model for eviction summaries).

### D8: Eviction threshold is per-node outcome count, not global

A node triggers eviction when its `len(outcomes) > 2k`. This preserves tree locality —
each node manages its own compression. A node that's been selected many times (lots of
outcomes) gets summarized; a node evaluated once stays verbatim.

Default: `k=10`, so eviction triggers at 20 outcomes per node, keeps 10 verbatim +
summary. A typical node accumulates 3 outcomes per evaluation, so eviction triggers
after ~7 evaluations of the same node.

---

## New Files

### 1. `src/gepa/proposer/reflective_mutation/memory_tree.py`

Core data structure. No LLM dependency — pure data + tree operations.

```python
@dataclass
class OutcomeDescription:
    question_summary: str        # brief identifier (first ~80 chars of problem)
    result: str                  # "correct" / "incorrect"
    observation: str             # 1-3 sentences from OI
    error_type: str | None       # arithmetic, logic, setup, interpretation, none
    score: float
    iteration: int

@dataclass
class MemoryTreeNode:
    node_id: int
    prompt: dict[str, str]       # full candidate text (stored once)
    parent_id: int | None
    children_ids: list[int]
    accepted: bool
    val_score: float | None      # set by engine after full eval
    minibatch_score: float | None
    outcomes: list[OutcomeDescription]    # verbatim recent outcomes
    outcome_summary: str                  # compressed summary of evicted outcomes
    iteration_created: int
    rejection_reason: str                 # brief note for rejected nodes ("scored worse: 1/3 vs 2/3")

@dataclass
class MemoryTreeEdge:
    parent_id: int
    child_id: int
    minibatch_ids: list[DataId]  # which specific examples triggered this transition
    iteration: int

class MemoryTree:
    def __init__(self, outcome_eviction_k: int = 10):
        self.nodes: dict[int, MemoryTreeNode] = {}
        self.edges: list[MemoryTreeEdge] = []
        self.root_id: int | None = None
        self._next_node_id: int = 0
        self.outcome_eviction_k = outcome_eviction_k   # evict when outcomes > 2*k
        self.global_summary: str = ""                   # Ring 4 content

    def add_root(self, candidate: dict[str, str], iteration: int) -> int:
        """Add seed candidate as root. Returns node_id."""

    def add_child(
        self,
        parent_id: int,
        candidate: dict[str, str],
        accepted: bool,
        minibatch_score: float | None,
        minibatch_ids: list[DataId],
        iteration: int,
        rejection_reason: str = "",
    ) -> int:
        """Add a proposed candidate as child of parent. Returns node_id."""

    def add_outcomes(self, node_id: int, outcomes: list[OutcomeDescription]) -> None:
        """Add OI outcomes to a node. Triggers per-node eviction if threshold exceeded."""

    def set_val_score(self, node_id: int, val_score: float) -> None:
        """Called when engine completes full eval on accepted candidate."""

    def get_node(self, node_id: int) -> MemoryTreeNode: ...
    def get_ancestry(self, node_id: int) -> list[MemoryTreeNode]:
        """Return [root, ..., grandparent, parent, node]."""

    def get_siblings(self, node_id: int) -> list[MemoryTreeNode]:
        """Return other children of this node's parent (excludes self)."""

    def get_all_branches(self) -> list[tuple[MemoryTreeNode, int, float | None]]:
        """Return (branch_root, depth, best_val_score) for branches not in a given ancestry."""

    def needs_eviction(self, node_id: int) -> bool:
        return len(self.nodes[node_id].outcomes) > 2 * self.outcome_eviction_k

    def evict_oldest(self, node_id: int) -> list[OutcomeDescription]:
        """Remove and return the oldest k outcomes from a node."""

    def update_node_summary(self, node_id: int, new_summary: str) -> None:
        """Replace node's outcome_summary with new compressed summary."""

    def update_global_summary(self, new_summary: str) -> None:
        """Replace Ring 4 global summary."""

    def to_dict(self) -> dict:
        """Serialize entire tree for logging/debugging."""
```

Key implementation details:
- `add_root` / `add_child` handle `_next_node_id` auto-increment
- `add_outcomes` checks `needs_eviction` and returns the evicted outcomes (caller
  handles LLM summarization)
- `get_ancestry` walks parent pointers from node to root, reverses
- `get_all_branches` finds all nodes at depth 1 (children of root), returns
  subtree stats for branches not containing a given node

### 2. `src/gepa/proposer/reflective_mutation/outcome_interpreter.py`

LLM call that produces per-question outcome descriptions.

```python
class OutcomeInterpreter:
    def __init__(self, lm: LanguageModel):
        self.lm = lm

    def interpret(
        self,
        candidate: dict[str, str],
        reflective_records: list[dict[str, Any]],
        scores: list[float],
        iteration: int,
    ) -> list[OutcomeDescription]:
        """
        Batched OI call. Takes the candidate prompt, reflective dataset records
        (which contain problem text, reasoning, predicted answer, feedback),
        and per-example scores.

        Returns one OutcomeDescription per record.
        """

    def _build_prompt(
        self,
        candidate: dict[str, str],
        records: list[dict[str, Any]],
        scores: list[float],
    ) -> str:
        """Build the batched OI prompt."""

    def _parse_response(
        self, response: str, records: list[dict[str, Any]], scores: list[float], iteration: int
    ) -> list[OutcomeDescription]:
        """Parse LLM response into structured OutcomeDescriptions."""
```

**OI Prompt Template** (stored as module-level constant):

```
You are analyzing the results of running a system prompt on math problems.
For each problem below, describe what happened in the model's reasoning.
Focus on WHAT the model did, not advice on how to fix it.

System prompt that was used:
<prompt>
{candidate_text}
</prompt>

{for each record:}
## Problem {i}
{record["Inputs"]}
{record["Generated Outputs"]}
Score: {scores[i]}
{record["Feedback"]}

---

For each problem, output exactly:
### Problem {i}
Result: correct/incorrect
Observation: [1-3 sentences describing what happened in the reasoning]
Error type: arithmetic | logic | setup | interpretation | none
```

**Why use reflective dataset records as input:** The adapter already knows how to
extract problem text, reasoning, and feedback from its domain-specific trajectories
into a generic text format. The OI operates on this generic format, making it
adapter-agnostic. No need for the OI to understand `AIMETrajectory` internals.

**Important:** The OI needs records for ALL examples (correct and incorrect), not
just failures. Currently `make_reflective_dataset` returns all records in the AIME
adapter. If other adapters filter to failures only, the OI should receive the raw
trajectories instead. For V0, we handle the AIME case where all records are returned.

### 3. `src/gepa/proposer/reflective_mutation/memory_renderer.py`

Converts tree → text for reflection prompt injection.

```python
class TieredMemoryRenderer:
    def __init__(
        self,
        ring1_char_budget: int = 6000,    # ~1500 tokens
        ring2_char_budget: int = 2000,    # ~500 tokens
        ring3_char_budget: int = 2000,    # ~500 tokens
        ring4_char_budget: int = 1200,    # ~300 tokens
    ):
        self.budgets = {
            "ring1": ring1_char_budget,
            "ring2": ring2_char_budget,
            "ring3": ring3_char_budget,
            "ring4": ring4_char_budget,
        }

    def render(self, tree: MemoryTree, current_node_id: int) -> str:
        """Render full memory text for injection into reflection prompt."""
        sections = []
        r4 = self._render_ring4(tree)
        r2 = self._render_ring2(tree, current_node_id)
        r1 = self._render_ring1(tree, current_node_id)
        r3 = self._render_ring3(tree, current_node_id)
        sections = [s for s in [r4, r2, r1, r3] if s]
        if not sections:
            return ""
        return "== OPTIMIZATION HISTORY ==\n\n" + "\n\n".join(sections)

    def _render_ring1(self, tree: MemoryTree, node_id: int) -> str:
        """
        Immediate family — highest resolution. Shows:

        1. Parent node: full prompt text + diff that produced current candidate + val score
        2. Siblings (other children of same parent): diff from parent + accept/reject status
           + val score (if accepted) + OI outcome summaries (condensed to 1 sentence each)
        3. Rejected children of the CURRENT node: diff from current + why rejected + OI outcomes

        Output format:
            [Parent prompt]
            <full parent text>

            [What changed to reach current prompt]
            Added: "..."
            Removed: "..."

            [Other attempts from same parent]
            • ACCEPTED (val=0.50): Added "bullet points" → Removed "step by step"
              Outcomes: Improved algebra setup but regressed on sign handling in geometry.
            • REJECTED (1/3 vs 2/3): Added "show all work"
              Outcomes: Model wrote more but computation errors increased on harder problems.

            [Attempts from current prompt that failed]
            • REJECTED (1/3 vs 2/3): Added "verify by substitution"
              Outcomes: Verification step correctly caught 1 error but introduced loop on 2 others.
        """

    def _render_ring2(self, tree: MemoryTree, node_id: int) -> str:
        """
        Ancestry chain — medium resolution. Shows the path from seed to current node's parent.
        One line per ancestor: what changed + val score.

        Output format:
            [How we got here]
            Seed (val=0.45)
            → Added step-by-step breakdown (val=0.48)
            → Added verification step (val=0.51)
            → current prompt (val=0.52)

        If any ancestor's diff is empty (seed), show "Seed prompt".
        If val_score is None (not yet evaluated on full val set), show "minibatch≈X".
        Truncate diff descriptions to ~60 chars if needed.
        """

    def _render_ring3(self, tree: MemoryTree, node_id: int) -> str:
        """
        Other branches — low resolution. One line per branch NOT in the ancestry of
        the current node. A "branch" is defined as each child of the root not on the
        ancestry path.

        Output format:
            [Other branches explored]
            • Persona branch (from seed, 3 nodes): peaked val=0.46, last active iter 4
            • Few-shot branch (from P_1, 2 nodes): peaked val=0.49, last active iter 9

        If a branch contains more than 3 nodes, show depth + peak score only.
        If a branch has the same val score as a node on the ancestry path, flag it:
          "• (same peak as current lineage) Verification branch..."
        """

    def _render_ring4(self, tree: MemoryTree) -> str:
        """
        Global summary — lowest resolution. The running compressed summary built from
        evicted outcomes across all nodes.

        Output format:
            [Global patterns]
            <tree.global_summary>

        If global_summary is empty (early in run, no evictions yet), return "".
        """

    @staticmethod
    def _compute_diff(old_text: str, new_text: str) -> str:
        """
        Compute human-readable diff between two prompt texts.

        Uses difflib.SequenceMatcher to find changed regions. Formats as:
          Added: "<new text>"
          Removed: "<old text>"
          Changed: "<old>" → "<new>"

        For very small changes (< 10 words added/removed total), shows a single
        inline "Changed: ..." line. For larger changes, shows Added/Removed blocks.
        If texts are identical, returns "(no change)".
        """
```

**Budget enforcement:** Each `_render_ring*` method accepts a char budget and truncates
to fit. Priority within each ring:
- Ring 1: show parent always; show siblings newest-first; show rejected children newest-first
- Ring 2: always show full chain (it's compact by design); truncate diff labels to 60 chars if needed
- Ring 3: sort branches by last-active iteration descending (most recently explored first)
- Ring 4: hard truncate at budget with "..." suffix

The `render()` method assigns budgets in order [ring4, ring2, ring1, ring3]. If total
content is under budget, no truncation occurs. If over budget, ring3 is shrunk first,
then ring1 siblings (but always keep parent + diff), then ring2 detail.

---

## Modified Files

### 4. `src/gepa/proposer/reflective_mutation/reflective_mutation.py`

**New constructor parameters:**

```python
def __init__(
    self,
    ...  # all existing params unchanged
    # MemV0 additions
    memory_tree: MemoryTree | None = None,
    outcome_interpreter: OutcomeInterpreter | None = None,
    memory_renderer: TieredMemoryRenderer | None = None,
):
```

**New instance state:**

```python
self.memory_tree = memory_tree
self.outcome_interpreter = outcome_interpreter
self.memory_renderer = memory_renderer
self._candidate_to_node: dict[str, int] = {}  # content_hash → tree_node_id
```

**Helper methods:**

```python
@staticmethod
def _hash_candidate(candidate: dict[str, str]) -> str:
    """Deterministic hash of candidate content for tree lookup."""
    import hashlib
    parts = tuple(sorted(candidate.items()))
    return hashlib.sha256(str(parts).encode()).hexdigest()[:16]

def _ensure_node_exists(self, candidate: dict[str, str], iteration: int) -> int:
    """Look up or create tree node for a candidate.

    Three cases:
    - Hash found in mapping: return existing node (normal path)
    - Tree is empty: add candidate as root (seed, first iteration)
    - Tree has root but candidate unknown: candidate was created outside the
      reflective proposer (e.g., by MergeProposer). Add as disconnected node
      with no parent and log a warning. The tree degrades gracefully — the
      node has no ancestry and won't appear in Ring 1/2 rendering.
    """
    h = self._hash_candidate(candidate)
    if h in self._candidate_to_node:
        return self._candidate_to_node[h]
    if self.memory_tree.root_id is None:
        node_id = self.memory_tree.add_root(candidate, iteration)
    else:
        _logger.warning(
            f"Iteration {iteration}: candidate not found in memory tree "
            "(likely created by MergeProposer). Adding as disconnected node."
        )
        node_id = self.memory_tree.add_root(candidate, iteration)  # no parent
    self._candidate_to_node[h] = node_id
    return node_id

def _run_oi_and_maybe_evict(
    self, node_id: int, candidate: dict[str, str],
    reflective_records: list[dict], scores: list[float], iteration: int,
) -> None:
    """Run OI, add outcomes to node, handle eviction if needed."""
    outcomes = self.outcome_interpreter.interpret(candidate, reflective_records, scores, iteration)
    self.memory_tree.add_outcomes(node_id, outcomes)
    if self.memory_tree.needs_eviction(node_id):
        evicted = self.memory_tree.evict_oldest(node_id)
        self._summarize_evicted(node_id, evicted)

def _summarize_evicted(self, node_id: int, evicted: list[OutcomeDescription]) -> None:
    """LLM call to compress evicted outcomes into node summary + update global summary."""
    # Uses self.outcome_interpreter.lm for the summarization call
```

**Changes to `propose()` method** — the critical integration:

```python
def propose(self, state: GEPAState) -> CandidateProposal | None:
    i = state.i + 1

    # --- V2 memory setup (unchanged, gated on self.reflection_memory) ---
    if self.reflection_memory is not None:
        self.reflection_memory.set_iteration(i)
        self.reflection_memory.fire_snapshot_event("before_proposal")

    # --- Select candidate and evaluate (unchanged) ---
    curr_prog_id = self.candidate_selector.select_candidate_idx(state)
    curr_prog = state.program_candidates[curr_prog_id]
    ...
    eval_curr = self.adapter.evaluate(minibatch, curr_prog, capture_traces=True)
    ...

    # >>> NEW: MemV0 — ensure current candidate is in tree, run OI <<<
    curr_node_id: int | None = None
    if self.memory_tree and self.outcome_interpreter:
        curr_node_id = self._ensure_node_exists(curr_prog, i)
        # Build OI records from ALL trajectories (not just components_to_update)
        oi_records = self._build_oi_records(curr_prog, eval_curr)
        self._run_oi_and_maybe_evict(curr_node_id, curr_prog, oi_records, eval_curr.scores, i)

    # --- Build reflective dataset (unchanged) ---
    reflective_dataset = self.adapter.make_reflective_dataset(...)

    # --- Propose new texts ---
    # In propose_new_texts(), memory injection switches based on which memory is active
    new_texts = self.propose_new_texts(
        curr_prog, reflective_dataset, predictor_names_to_update,
        iteration=i, current_tree_node_id=curr_node_id,  # new param
    )

    # --- Evaluate P_B ---
    new_candidate = curr_prog.copy()
    for pname, text in new_texts.items():
        new_candidate[pname] = text

    # >>> CHANGED for MemV0: evaluate with traces <<<
    if self.memory_tree:
        eval_new = self.adapter.evaluate(minibatch, new_candidate, capture_traces=True)
        state.increment_evals(len(subsample_ids))
        new_scores = eval_new.scores
        # Update cache manually
        if state.evaluation_cache is not None:
            state.evaluation_cache.put_batch(
                new_candidate, subsample_ids, eval_new.outputs, eval_new.scores,
                list(eval_new.objective_scores) if eval_new.objective_scores else None,
            )
    else:
        # Existing path via cached_evaluate_full
        outputs_by_id, scores_by_id, ... = state.cached_evaluate_full(...)
        new_scores = [scores_by_id[eid] for eid in subsample_ids]

    # --- Accept/reject ---
    new_sum = sum(new_scores)
    old_sum = sum(eval_curr.scores)
    accepted = new_sum > old_sum

    # >>> NEW: MemV0 — add P_B to tree and always run OI <<<
    if self.memory_tree:
        rejection_reason = ""
        if not accepted:
            rejection_reason = f"scored {new_sum:.1f}/{len(new_scores)} vs {old_sum:.1f}/{len(eval_curr.scores)}"
        new_node_id = self.memory_tree.add_child(
            parent_id=curr_node_id,
            candidate=new_candidate,
            accepted=accepted,
            minibatch_score=new_sum / len(new_scores) if new_scores else None,
            minibatch_ids=list(subsample_ids),
            iteration=i,
            rejection_reason=rejection_reason,
        )
        # Note: val_score for new_node is not set here. The engine runs the full val
        # eval AFTER propose() returns. The val score is picked up lazily at the top
        # of the NEXT iteration when this candidate is re-selected (or remains None
        # in Ring 1 rendering if the candidate is never re-selected). This is a known
        # one-iteration lag. Acceptable for V0.
        self._candidate_to_node[self._hash_candidate(new_candidate)] = new_node_id

        # OI always runs on P_B — rejected candidates carry the most important lessons
        if self.outcome_interpreter:
            oi_records_new = self._build_oi_records(new_candidate, eval_new)
            self._run_oi_and_maybe_evict(new_node_id, new_candidate, oi_records_new, new_scores, i)

    # --- V2 memory recording (unchanged, gated on self.reflection_memory) ---
    if self.reflection_memory is not None:
        ... # existing lesson generation + memory entry code

    return CandidateProposal(...)
```

**Changes to `propose_new_texts()` method:**

```python
def propose_new_texts(
    self,
    candidate, reflective_dataset, components_to_update,
    iteration=0, current_tree_node_id=None,  # new param
):
    ...
    for name in components_to_update:
        ...
        # Memory injection — V2 or MemV0, not both
        effective_template = prompt_template
        memory_was_injected = False

        if self.memory_tree and self.memory_renderer and current_tree_node_id is not None:
            # MemV0 path
            memory_text = self.memory_renderer.render(self.memory_tree, current_tree_node_id)
            if memory_text:
                base_template = effective_template or InstructionProposalSignature.default_prompt_template
                effective_template = base_template + "\n\n" + memory_text
                memory_was_injected = True
        elif self.reflection_memory is not None:
            # V2 path (unchanged)
            ...
```

**New helper for building OI records from all trajectories:**

```python
def _build_oi_records(
    self, candidate: dict[str, str], eval_batch: EvaluationBatch
) -> list[dict[str, Any]]:
    """Build records for OI from all trajectories (not filtered by component).

    The OI needs records for ALL examples — both correct and incorrect. The AIME
    adapter's make_reflective_dataset returns all records. Adapters that filter to
    failures only will under-serve the OI; this is a known V0 limitation.
    """
    if not eval_batch.trajectories:
        _logger.warning("No trajectories in eval_batch — OI will receive no records for this node.")
        return []
    all_components = list(candidate.keys())
    dataset = self.adapter.make_reflective_dataset(candidate, eval_batch, all_components)
    if not dataset:
        _logger.warning("make_reflective_dataset returned empty dict — OI will receive no records.")
        return []
    # Return records from the first component. For single-component candidates,
    # all components produce identical records, so the first is sufficient.
    for comp_records in dataset.values():
        return list(comp_records)
    return []
```

### 5. `src/gepa/api.py`

**New parameters to `optimize()`:**

```python
def optimize(
    ...
    # Existing reflection memory params
    use_reflection_memory: bool = False,
    reflection_memory_max_entries: int = 10,
    lesson_lm: LanguageModel | str | None = None,
    objective: str = "",
    # >>> NEW: MemV0 params <<<
    memory_version: Literal["v2", "v0"] | None = None,
    oi_lm: LanguageModel | str | None = None,
    outcome_eviction_k: int = 10,
    memory_ring1_budget: int = 6000,
    memory_ring2_budget: int = 2000,
    memory_ring3_budget: int = 2000,
    memory_ring4_budget: int = 1200,
    ...
)
```

**Wiring logic (after existing reflection memory setup):**

```python
# MemV0 memory setup
memory_tree: MemoryTree | None = None
outcome_interpreter: OutcomeInterpreter | None = None
memory_renderer: TieredMemoryRenderer | None = None

if memory_version == "v0":
    from gepa.proposer.reflective_mutation.memory_tree import MemoryTree
    from gepa.proposer.reflective_mutation.outcome_interpreter import OutcomeInterpreter
    from gepa.proposer.reflective_mutation.memory_renderer import TieredMemoryRenderer

    # Resolve OI LLM: explicit override > reflection_lm
    oi_lm_callable = None
    if oi_lm is not None:
        if isinstance(oi_lm, str):
            from gepa.optimize_anything import make_litellm_lm
            oi_lm_callable = make_litellm_lm(oi_lm)
        else:
            oi_lm_callable = oi_lm
    else:
        oi_lm_callable = reflection_lm_callable

    memory_tree = MemoryTree(outcome_eviction_k=outcome_eviction_k)
    outcome_interpreter = OutcomeInterpreter(lm=oi_lm_callable)
    memory_renderer = TieredMemoryRenderer(
        ring1_char_budget=memory_ring1_budget,
        ring2_char_budget=memory_ring2_budget,
        ring3_char_budget=memory_ring3_budget,
        ring4_char_budget=memory_ring4_budget,
    )

    # Disable V2 memory if V0 is selected
    reflection_memory = None

# Pass to proposer
reflective_proposer = ReflectiveMutationProposer(
    ...  # existing params
    reflection_memory=reflection_memory,
    # MemV0
    memory_tree=memory_tree,
    outcome_interpreter=outcome_interpreter,
    memory_renderer=memory_renderer,
)
```

**Validation:** If `memory_version="v0"` and `use_reflection_memory=True`, raise
an error — can't use both simultaneously.

### 6. `experiments/aime_memory/run.py`

**New CLI flags:**

```python
parser.add_argument("--memory-version", choices=["v2", "v0"], default=None,
                    help="Memory version: v2 (lesson-based) or v0 (outcome interpreter + tree)")
parser.add_argument("--oi-lm", type=str, default=None,
                    help="LiteLLM model for outcome interpreter (defaults to reflection LM)")
parser.add_argument("--outcome-eviction-k", type=int, default=10)
```

**Updated optimize call:**

```python
result = optimize(
    ...
    # V2 memory (only when --memory and not --memory-version v0)
    use_reflection_memory=args.memory and args.memory_version != "v0",
    reflection_memory_max_entries=args.memory_entries,
    # MemV0
    memory_version=args.memory_version if args.memory else None,
    oi_lm=args.oi_lm,
    outcome_eviction_k=args.outcome_eviction_k,
    ...
)
```

**Usage examples in docstring:**

```
# Baseline (no memory)
uv run python -m experiments.aime_memory.run --seed 0

# V2 memory (existing)
uv run python -m experiments.aime_memory.run --seed 0 --memory

# V0 memory (new)
uv run python -m experiments.aime_memory.run --seed 0 --memory --memory-version v0
```

### 7. Val score update: `src/gepa/core/engine.py` (minor hook)

When the engine accepts a candidate and runs full eval, the val score needs to flow
back to the tree. The cleanest way: **fire a callback** with the candidate + val score,
and have the proposer listen.

BUT: the proposer doesn't currently receive callbacks from the engine about acceptance.
The engine calls `proposer.propose()` and uses the returned `CandidateProposal` — it
doesn't call back with the val score.

**Decision:** The proposer updates val scores lazily. When `propose()` is called and
selects a candidate from the Pareto front, it checks `state.program_full_scores_val_set`
for the selected candidate's val score and updates the tree node. This is one extra
dictionary lookup per iteration — negligible cost.

```python
# In propose(), after selecting curr_prog_id:
if self.memory_tree:
    curr_node_id = self._ensure_node_exists(curr_prog, i)
    val_score = state.program_full_scores_val_set.get(curr_prog_id)
    if val_score is not None:
        self.memory_tree.set_val_score(curr_node_id, val_score)
```

---

## Implementation Order

Dependencies dictate the order. Each step is independently testable.

### Step 1: `memory_tree.py` — the data structure

No dependencies on LLMs or existing code. Pure Python data structure.

- All dataclasses: `OutcomeDescription`, `MemoryTreeNode`, `MemoryTreeEdge`
- `MemoryTree` class with all methods
- `to_dict()` serialization for debugging
- Unit tests: add root, add children, get ancestry, get siblings, eviction trigger

### Step 2: `memory_renderer.py` — tree to text

Depends on Step 1 (imports MemoryTree types).

- `TieredMemoryRenderer` with all 4 ring methods
- `_compute_diff` utility
- Budget enforcement logic
- Unit tests: build a small tree manually, verify each ring's output

### Step 3: `outcome_interpreter.py` — LLM call

Depends on Step 1 (produces `OutcomeDescription`). No dependency on Step 2.

- `OutcomeInterpreter` class
- Prompt template
- Response parser (regex-based extraction of per-question blocks)
- Eviction summarization prompt + method
- Unit tests: mock the LLM, verify prompt construction and parsing

### Step 4: Integrate into `reflective_mutation.py`

Depends on Steps 1-3. This is the main integration work.

- Add constructor params and instance state
- Add `_hash_candidate`, `_ensure_node_exists`, `_run_oi_and_maybe_evict`,
  `_summarize_evicted`, `_build_oi_records` helpers
- Modify `propose()` to call OI after evaluation, add nodes, handle eviction
- Modify `propose_new_texts()` to inject MemV0 memory when active
- Change P_B evaluation to use `adapter.evaluate` directly when MemV0 is active

### Step 5: Wire up in `api.py`

Depends on Step 4.

- Add `memory_version`, `oi_lm`, `outcome_eviction_k`, ring budget params
- Add MemV0 construction logic
- Add validation (can't use V2 and V0 simultaneously)

### Step 6: Update experiment runner

Depends on Step 5.

- Add CLI flags to `run.py` (and `run_light.py`, `run_verbose.py`)
- Update docstrings with usage examples

### Step 7: Integration test

End-to-end test with mock LLM:
- Run 3-4 iterations of the AIME experiment with MemV0
- Verify tree structure (nodes, edges, outcomes)
- Verify memory rendering contains expected sections
- Verify OI was called correct number of times
- Verify eviction triggers when expected

---

## What This Plan Does NOT Include (Deferred)

1. **Callback events for MemV0** — No `TreeNodeAddedEvent`, `OutcomeInterpreterEvent`,
   etc. The existing ResearchLogger won't capture MemV0 internals. Add in a follow-up
   after the first experiment shows the system works.

2. **Persistence/serialization of the tree** — The tree lives in memory during a run
   and is lost when the process exits. `to_dict()` can dump it to JSON for post-hoc
   analysis, but there's no resume-from-checkpoint support. The engine's existing
   state saving doesn't know about the tree.

3. **Multi-component candidates** — The OI and renderer assume single-component
   candidates (like the AIME experiment). Multi-component rendering (separate trees
   per component? one tree with component-aware diffs?) is deferred.

4. **Adaptive ring budgets** — The ring budgets are fixed. An adaptive system that
   reallocates budget based on tree shape (e.g., deep tree → more ancestry budget)
   is a future optimization.

5. **Global summary updates** — The eviction summarization produces per-node summaries.
   The global summary (Ring 4) update logic is included but may be sparse until enough
   nodes trigger eviction. If Ring 4 is consistently empty in early experiments, consider
   generating it periodically (every N iterations) instead of only on eviction.
