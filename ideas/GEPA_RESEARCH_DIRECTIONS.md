# Research Directions for Improving GEPA

This document identifies concrete limitations in GEPA's current design and proposes original research directions grounded in the actual implementation. Each section names the specific code mechanism that creates the bottleneck, explains *why* it limits performance, and outlines what an improved version would look like.

---

## 1. Reflection Has No Memory

**The bottleneck.** Every call to the reflection LLM (`InstructionProposalSignature.run()` in `strategies/instruction_proposal.py`) starts from scratch. The prompt contains the current component text and a single minibatch of examples with feedback, but it has *no knowledge* of:

- What the reflection LLM proposed in previous iterations
- Which kinds of mutations succeeded or failed
- What failure patterns have already been diagnosed and addressed

The same reflection LLM may diagnose the same failure repeatedly, or oscillate between two strategies it keeps re-discovering.

**Direction: Reflection with episodic memory.** Maintain a compressed log of past reflections: `(iteration, diagnosis, proposed change, outcome)`. Append a summary of recent history to the reflection prompt so the LLM can avoid repeating failed strategies and build on successful ones. This is distinct from the evaluation cache (which caches *scores*); this caches *reasoning*.

Research questions:
- How much history can you include before it degrades reflection quality (context window saturation)?
- Can you learn a compressor that summarizes past reflections into a shorter "optimization memoir"?
- Does adding history improve sample efficiency (fewer iterations to reach the same score)?

---

## 2. Merge Is Syntactic, Not Semantic

**The bottleneck.** `MergeProposer.propose()` in `proposer/merge.py` (lines 155-184) combines candidates by choosing *which parent's text* to copy for each component. For each component, it checks if one descendant kept the ancestor's text while the other changed it, then takes the changed one. If both changed, it picks the higher-scoring parent's version.

This is a cut-and-paste crossover. It never asks: "Candidate A added chain-of-thought reasoning and B added domain-specific terminology — can you write a component that does *both*?"

**Direction: LLM-mediated semantic merge.** Instead of syntactic selection per component, feed the reflection LLM *both* parent candidates, their respective strengths (which examples each excels on, what kind of feedback each gets), and ask it to synthesize a new candidate that combines the insights. This turns crossover from a discrete selection problem into a creative generation problem.

Research questions:
- Does semantic merge improve over syntactic merge when both parents changed the same component in different ways?
- What's the right prompt structure to show two parent candidates and their complementary strengths?
- Can you identify *when* to use semantic merge vs. syntactic merge (e.g., based on edit distance between parent components)?

---

## 3. Minibatch Sampling Ignores Difficulty

**The bottleneck.** `EpochShuffledBatchSampler` (`strategies/batch_sampler.py`) shuffles training IDs uniformly each epoch and pads with least-frequently-evaluated examples. It does not consider:

- Which examples the current best candidate *fails on*
- Which examples have the highest variance across candidates (indicating they're "informative")
- Which examples are "stuck" (no candidate has scored well on them)

This means the reflection LLM wastes iterations reflecting on already-solved examples when there are unsolved ones that could drive more improvement.

**Direction: Failure-aware curriculum sampling.** Use the Pareto front scores in `GEPAState.pareto_front_valset` to identify examples where the best-known score is low. Oversample these hard examples in the training minibatch. Concretely, weight the sampling probability of example `i` by `(1 - pareto_score[i])^alpha` for a temperature parameter alpha.

Variants:
- **Adversarial sampling**: Always include the K hardest unsolved examples
- **Diversity sampling**: Select examples that span different failure modes (cluster by feedback text, sample one per cluster)
- **Uncertainty sampling**: Prioritize examples with high score variance across Pareto-front candidates (disagreement indicates room for improvement)

Research questions:
- Does curriculum sampling reduce the number of iterations needed to reach a target score?
- Does it risk overfitting the candidate to hard examples at the expense of easy ones (catastrophic forgetting)?
- What is the right schedule for alpha — should it increase over time (start uniform, shift toward hard examples as easy ones are solved)?

---

## 4. Acceptance Is Greedy on the Minibatch Sum

**The bottleneck.** In `ReflectiveMutationProposer.propose()` (reflective_mutation.py), the acceptance check is:

```python
if sum(new_scores) > sum(old_scores):
    return CandidateProposal(...)
```

This rejects candidates that trade off: improving on 3 examples but regressing on 1 (if the regression exceeds the gains). But these "differently-specialized" candidates could be valuable Pareto-front members — they might solve examples that no existing candidate handles.

**Direction: Pareto-aware minibatch acceptance.** Instead of requiring strict sum-improvement, accept a candidate if it achieves a *new Pareto-optimal score vector* on the minibatch. Formally: accept if there exists no existing candidate that dominates the new one on all minibatch examples simultaneously.

Alternatively, use a softer criterion:
- Accept if `sum(new_scores) > sum(old_scores) - epsilon` (relaxed threshold) AND the new candidate scores >0 on at least one example where the old candidate scored 0 (diversity bonus)
- Accept with probability proportional to the improvement (Metropolis-Hastings style), allowing occasional lateral moves

Research questions:
- Does Pareto-aware acceptance increase the diversity of the candidate pool?
- Does it slow convergence on average score but improve the Pareto front coverage?
- What's the right trade-off between exploration (accepting diverse candidates) and exploitation (only accepting strict improvements)?

---

## 5. Component Selection Doesn't Know What's Broken

**The bottleneck.** `RoundRobinReflectionComponentSelector` (`strategies/component_selector.py`) cycles through components blindly. If a multi-component candidate has a broken system prompt but a fine user template, round-robin will waste half its iterations reflecting on the user template.

**Direction: Feedback-driven component attribution.** Before selecting which component to update, run a lightweight attribution step: for each failed example in the minibatch, ask the reflection LLM (or use a heuristic) "which component is most responsible for this failure?" Then select the component most frequently attributed.

Concretely:
1. After `adapter.evaluate()` with traces, scan the feedback strings for references to each component's behavior
2. Count how many failures implicate each component
3. Select the component with the most failures

More sophisticated: maintain a running score per component of "improvement potential" based on how much score gain was achieved when that component was last updated.

Research questions:
- How accurately can you attribute failures to specific components from trace data?
- Does attribution-based selection converge faster than round-robin for 3+ component candidates?
- Can you train a small classifier on (trace, responsible_component) pairs from early iterations to speed up later attribution?

---

## 6. No Meta-Learning Across the Optimization Trajectory

**The bottleneck.** GEPA treats each iteration independently. It doesn't learn patterns like "adding chain-of-thought instructions tends to improve math scores" or "this reflection LLM tends to propose verbose candidates that hurt performance." The strategies (candidate selection, batch sampling, component selection) are all static.

**Direction: Bandit-based strategy adaptation.** Treat the choice of strategy at each iteration as a multi-armed bandit problem:

- **Candidate selector bandit**: Maintain Thompson Sampling posteriors for each selector (Pareto, CurrentBest, EpsilonGreedy). Update the posterior based on whether the selected candidate's mutation was accepted. Over time, learn which selection strategy works best for this particular optimization landscape.

- **Minibatch size bandit**: The current minibatch size is fixed. But early in optimization (when candidates are bad), small minibatches may be more informative (faster iteration). Late in optimization (when refinements are subtle), larger minibatches give more reliable acceptance decisions. Adapt the minibatch size using a contextual bandit conditioned on iteration number and recent acceptance rate.

- **Reflection temperature bandit**: If using an LLM with temperature control, adapt the temperature based on whether recent proposals were accepted. High temperature when stuck (more exploration), low temperature when making progress (exploit the current direction).

Research questions:
- Does bandit-based strategy adaptation measurably improve sample efficiency over static strategies?
- What's the right time horizon for updating bandit posteriors (per-iteration is noisy; per-epoch may be too slow)?
- Can you transfer learned bandit policies across optimization runs on similar tasks?

---

## 7. Full Valset Evaluation After Every Accepted Candidate

**The bottleneck.** In `GEPAEngine._run_full_eval_and_add()` (engine.py line 145), every accepted candidate triggers a full valset evaluation via `_evaluate_on_valset()`. With `FullEvaluationPolicy`, this evaluates on *every* valset example. For a valset of size 200 and a task LM that costs $0.01/call, each accepted candidate costs $2.00 just for validation — and GEPA might accept 50+ candidates per run.

The evaluation cache (`EvaluationCache`) mitigates this when the same candidate is re-evaluated, but newly proposed candidates are by definition unseen.

**Direction: Progressive evaluation with early stopping.** Instead of evaluating every accepted candidate on the full valset, use a sequential testing approach:

1. Start by evaluating on a small random subset (e.g., 20% of valset)
2. Compute a confidence interval for the candidate's true average score
3. If the lower bound exceeds the current best score → accept (don't need more data)
4. If the upper bound is below the current best score → reject early (save evaluations)
5. Otherwise, evaluate on more examples and repeat

This is a classic sequential hypothesis testing setup (Wald's SPRT or confidence-bound methods). It can save 30-70% of valset evaluations for clearly good or clearly bad candidates.

Research questions:
- What confidence level balances evaluation savings against the risk of accepting inferior candidates?
- How does progressive evaluation interact with Pareto front tracking (which needs per-example scores)?
- Can you use the evaluation cache to warm-start confidence intervals (if similar candidates have been evaluated on some examples)?

---

## 8. Reflection Prompt Doesn't Decompose the Problem

**The bottleneck.** `InstructionProposalSignature` (instruction_proposal.py) sends the reflection LLM a single monolithic prompt: "here's the current instruction, here are some examples with feedback, write a better instruction." For complex tasks with multiple failure modes, the LLM must simultaneously diagnose *all* failure patterns and propose a single fix that addresses all of them.

**Direction: Decomposed reflection with explicit diagnosis and prescription.**

Split the single reflection call into a pipeline:

1. **Diagnose**: "Here are the failing examples with traces. Categorize them into distinct failure modes. For each failure mode, explain what went wrong."
2. **Prioritize**: "Which failure mode is most impactful? Which is easiest to fix without breaking the passing examples?"
3. **Prescribe**: "Given the current instruction and the prioritized failure mode, write a targeted fix."
4. **Verify**: "Here's the proposed new instruction. Do you expect it will break any of the currently-passing examples? If so, adjust."

This is more expensive per iteration (3-4 LLM calls instead of 1) but each call is simpler and more focused. The hypothesis is that the total number of iterations drops enough to offset the per-iteration cost.

Research questions:
- Does decomposed reflection produce higher-quality mutations (higher acceptance rate)?
- What's the optimal decomposition — is diagnosis+prescription sufficient, or is verification essential?
- Can you amortize the diagnosis step across iterations (cache failure mode classifications)?

---

## 9. No Diversity Pressure in the Population

**The bottleneck.** GEPA's Pareto front tracks the *score-wise* best candidates per example, but there's no pressure to maintain *semantic* diversity. Two candidates with identical scores but very different approaches (e.g., chain-of-thought vs. few-shot examples) would both sit on the Pareto front, which is fine. But the candidate selector doesn't prefer the underexplored approach — `ParetoCandidateSelector` weights by score, not by how different a candidate is from others.

This means the population can converge to a cluster of similar candidates that all exploit the same strategy, missing entirely different (and potentially better) strategies.

**Direction: Quality-diversity search (MAP-Elites style).** Assign each candidate to a behavioral niche based on *which* examples it solves, not just its average score. Maintain one representative per niche (the highest-scoring candidate that solves that particular subset). When selecting candidates for mutation, preferentially select from under-populated or low-scoring niches.

Concretely:
1. Define a behavior descriptor for each candidate: the binary vector of which valset examples it scores > threshold on
2. Cluster candidates by behavior descriptor (e.g., by Hamming distance)
3. Maintain a MAP-Elites grid where each cell is a cluster and stores the best-scoring candidate
4. Select candidates for mutation from cells with low max scores (room for improvement) or few occupants (under-explored)

Research questions:
- Does quality-diversity search discover more diverse Pareto-front candidates?
- What's the right granularity for behavior descriptors (per-example binary vector, or cluster into coarser categories)?
- Does MAP-Elites interact well with the merge operator (merging candidates from different niches)?

---

## 10. The Reflection LLM Can't See What Changed

**The bottleneck.** The reflection prompt shows the current candidate and examples with feedback, but it doesn't show *what was tried before*. In particular, it doesn't show:

- The parent candidate (what was the instruction *before* the last mutation?)
- What the last mutation changed and whether it helped
- The diff between the current candidate and its ancestors

Without this context, the reflection LLM can't reason about the optimization trajectory — it can't say "the last change added chain-of-thought and that helped on math examples, so let's keep that and also address the formatting issues."

**Direction: Diff-aware reflection.** Augment the reflection prompt with:

1. The diff between the current candidate and its parent
2. The score change that resulted from the last mutation
3. A one-line summary of the last 3 mutations (e.g., "iter 5: added step-by-step reasoning (+8%), iter 6: added output format specification (+3%), iter 7: shortened to reduce token count (-2%)")

This gives the reflection LLM a sense of direction and momentum, allowing it to build on successful changes and avoid reverting helpful ones.

Research questions:
- Does diff-aware reflection improve the acceptance rate of proposed mutations?
- How far back should the history go (1 parent, 3 ancestors, full lineage)?
- Does this help prevent "forgetting" — where a mutation inadvertently removes a useful instruction that a previous mutation added?

---

## 11. Single Reflection LLM, Single Perspective

**The bottleneck.** The reflection step uses a single LLM call to propose one new candidate. If the LLM misdiagnoses the problem or proposes a suboptimal fix, the iteration is wasted — there's no second opinion or verification.

**Direction: Ensemble reflection with selection.**

Generate K candidate mutations per iteration using either:
- The same LLM at different temperatures
- Different LLMs (e.g., a strong reasoner and a creative generalist)
- The same LLM with different decomposition prompts

Then select the best among the K proposals by evaluating all of them on the same minibatch and keeping the highest-scoring one. This costs K times as many LLM calls for reflection, but K times fewer wasted iterations.

More sophisticated variant: **debate-based reflection**. Two LLMs each propose a mutation, then each critiques the other's proposal. A final arbitrator selects or synthesizes. This adds verification without full minibatch evaluation of all K proposals.

Research questions:
- What K gives the best cost-quality tradeoff (K=3 vs K=5 vs K=10)?
- Is it better to use temperature diversity or prompt diversity?
- Does debate-based reflection produce higher-quality mutations than independent sampling?

---

## 12. Transfer Learning Across Optimization Runs

**The bottleneck.** Each call to `optimize()` or `optimize_anything()` starts from a single `seed_candidate`. Even if you've previously optimized a similar prompt on a similar task, that knowledge isn't reused. The only way to transfer is to manually set `seed_candidate` to a previous run's best result.

**Direction: Warm-starting from a library of past runs.** Maintain a persistent store of (task_description, best_candidates, reflection_logs) from past optimization runs. When starting a new run:

1. Embed the new task description and retrieve similar past tasks
2. Initialize the population not from a single seed, but from a diverse set of candidates from similar past runs
3. Optionally, seed the reflection memory (Direction 1) with reflection logs from similar tasks

This is analogous to how meta-learning works in gradient-based optimization — transfer the initial point and optimizer state from related tasks.

Research questions:
- How do you measure task similarity for retrieval (embedding distance of task descriptions, or structural similarity of training data)?
- Does warm-starting from past candidates actually help, or do domain differences make past candidates harmful (negative transfer)?
- Can you transfer the reflection *strategy* (which kinds of mutations worked) even when the specific candidate text doesn't transfer?

---

## Summary Table

| # | Direction | Bottleneck Location | Key Hypothesis |
|---|---|---|---|
| 1 | Reflection memory | `InstructionProposalSignature` | History-aware reflection avoids redundant diagnoses |
| 2 | Semantic merge | `MergeProposer.propose()` | LLM-mediated merge outperforms syntactic crossover |
| 3 | Curriculum sampling | `EpochShuffledBatchSampler` | Focusing on hard examples accelerates improvement |
| 4 | Pareto-aware acceptance | `sum(new) > sum(old)` check | Accepting diverse candidates improves frontier coverage |
| 5 | Feedback-driven component selection | `RoundRobinComponentSelector` | Attribution-based selection reduces wasted iterations |
| 6 | Bandit-based strategy adaptation | All strategy classes | Adaptive strategies outperform static ones |
| 7 | Progressive evaluation | `_evaluate_on_valset()` | Sequential testing saves 30-70% of valset evaluations |
| 8 | Decomposed reflection | `InstructionProposalSignature` | Structured diagnosis+prescription beats monolithic prompts |
| 9 | Quality-diversity search | `ParetoCandidateSelector` | Niche-based selection discovers more diverse strategies |
| 10 | Diff-aware reflection | `InstructionProposalSignature` | Showing the optimization trajectory prevents forgetting |
| 11 | Ensemble reflection | `ReflectiveMutationProposer` | Multiple proposals per iteration reduce wasted iterations |
| 12 | Transfer across runs | `initialize_gepa_state()` | Warm-starting from similar tasks accelerates convergence |
