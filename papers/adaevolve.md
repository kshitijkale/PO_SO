# AdaEvolve Methodology Artifact

## Adaptive LLM-Driven Zeroth-Order Optimization Framework

This document specifies the full methodology for implementing the AdaEvolve framework: an LLM-guided evolutionary search system with adaptive control loops. The system searches a discrete space of executable programs to maximize a fitness function using adaptive exploration intensity, dynamic resource allocation, and meta-level strategy generation.

## 1. Problem Definition

The objective is to maximize a fitness function:

\[
F : P \rightarrow \mathbb{R}
\]

Where:

- `P` = space of executable programs
- `F(p)` = evaluator function that returns a scalar fitness score

The search is subject to a fixed computational budget `B`, typically measured as:

- number of iterations
- number of LLM calls
- wall-clock compute

The system maintains parallel subpopulations called islands.

\[
D = \{D_1, D_2, ..., D_K\}
\]

Where:

- `K` = number of islands
- `D_k` = archive of programs in island `k`

Each island evolves programs asynchronously.

## 2. Core Evolutionary Cycle

For each iteration the system performs:

1. Island Selection
2. Parent Sampling
3. LLM Mutation
4. Program Evaluation
5. Archive Update
6. Adaptive State Update
7. Meta-Guidance Check

## 3. System Inputs

The system requires only:

- LLM model name `M`
- evaluator function `F`
- initial program `p₀`
- iteration budget `T`

Everything else is adaptively determined by the algorithm.

## 4. Global State Variables

For each island `k`:

```text
D_k       : archive of programs
f_k*      : best fitness in island
G_k       : accumulated improvement signal
R_k       : decayed reward for bandit
V_k       : decayed visit count
n_k       : raw visit count
```

Global state:

```text
f_global* : best fitness across all islands
Tactics   : list of meta-guidance solution tactics
K         : number of islands
```

## 5. Island Initialization

For each island:

```text
D_k = {p0}
f_k* = F(p0)

G_k = 0
R_k = 0
V_k = 0
n_k = 0
```

Global best:

```text
f_global* = F(p0)
```

## 6. Level 1: Local Adaptation (Exploration Intensity)

Each island dynamically adjusts exploration vs exploitation using the Accumulated Improvement Signal.

### 6.1 Improvement Signal

When a child program `p'` is evaluated:

\[
\delta_t^{(k)} = \max \left(\frac{f' - f_k^*}{f_k^*}, 0\right)
\]

Where:

- `f'` = fitness of new child
- `f_k*` = current island best

If the child does not improve the best score:

```text
δ = 0
```

### 6.2 Accumulated Improvement Signal

The improvement signal tracks recent productivity using an exponential moving average:

\[
G_t^{(k)} = \rho G_{t-1}^{(k)} + (1 - \rho)(\delta_t^{(k)})^2
\]

Where:

- `ρ` = decay factor

Interpretation:

- High `G_k` -> island improving rapidly
- Low `G_k` -> island stagnating

### 6.3 Exploration Intensity

Exploration probability:

\[
I_t^{(k)} =
I_{min} +
\frac{I_{max} - I_{min}}
{\sqrt{1 + G_t^{(k)} + \epsilon}}
\]

Typical parameters:

- `I_min = 0.1`
- `I_max = 0.7`
- `ε` = small constant

Interpretation:

| Signal | Behavior |
| --- | --- |
| High `G_k` | exploitation |
| Low `G_k` | exploration |

### 6.4 Mode Sampling

For each mutation step:

```python
u ~ Uniform(0,1)

if u < I_k:
    exploration
else:
    exploitation
```

## 7. Parent Selection

### Exploration Mode

```text
parent = random program from archive
inspires = most diverse programs
```

Purpose:

- Encourage novel directions.

### Exploitation Mode

```text
parent = sample from top 25% by fitness
inspires = highest performing programs
```

Purpose:

- Refine promising programs.

## 8. Prompt Construction

The mutation prompt contains:

- parent program
- inspiration programs
- metrics
- optional tactic

Structure:

```text
Improve the following program:

<parent code>

Consider these alternative approaches:

<inspiration code>

(optional)
Implement this strategy:
<tactic>
```

## 9. Mutation Operator

Mutation is performed using the LLM:

\[
p' = LLM(prompt)
\]

This acts as a semantic mutation operator rather than random syntax mutation.

## 10. Evaluation

The candidate program is executed by the evaluator:

\[
f' = F(p')
\]

The program and score are added to the island archive.

## 11. Adaptive State Update

If the child improves the island best:

```text
δ = (f_child - f_k*) / (|f_k*| + ε)

G_k = ρ G_k + (1-ρ) δ²

reward r =
(f_child - f_k*) / (|f_global*| + ε)

f_k* = f_child
```

If it improves the global best:

```text
f_global* = f_child
```

If no improvement:

```text
G_k = ρ G_k
r = 0
```

Bandit statistics update:

```text
R_k = ρ R_k + r
V_k = ρ V_k + 1
n_k += 1
```

## 12. Level 2: Global Adaptation (Island Selection)

The algorithm decides which island receives compute.

This is formulated as a multi-armed bandit problem.

### 12.1 Global Normalized Reward

Reward is normalized by the global best:

\[
r_t^{(k)} =
\frac{f' - f_k^*}{f_{global}^*}
\]

This prevents islands optimizing poor solutions from receiving excess resources.

### 12.2 Decayed Reward Tracking

To avoid stale success dominating:

\[
R_t^{(k)} = \rho R_{t-1}^{(k)} + r_t
\]

\[
V_t^{(k)} = \rho V_{t-1}^{(k)} + 1
\]

### 12.3 UCB Selection Rule

Island selected using Upper Confidence Bound:

\[
k^* =
\arg\max_k
\left(
\frac{R_k}{V_k}
+
C \sqrt{\frac{\ln N}{n_k}}
\right)
\]

Where:

- `N` = total iterations
- `C = \sqrt{2}`

Interpretation:

- First term -> exploitation
- Second term -> exploration

## 13. Migration Across Islands

Every `M` iterations:

```text
Island k sends best programs to island (k+1 mod K)
```

Effects:

- update receiving island archive
- update local best
- update improvement signal

Bandit statistics are not updated since the island did not produce the improvement.

## 14. Dynamic Island Spawning

New islands are created when global stagnation occurs.

Condition:

```text
if G_k ≤ τ_S for all islands
```

Typical threshold:

```text
τ_S = 0.02
```

Procedure:

- create new island
- seed with random program from global archive
- initialize island state

Purpose:

- Create diversity and explore new solution regions.

## 15. Level 3: Meta-Guidance

If numerical adaptation fails, the system performs conceptual strategy generation.

### Trigger Condition

Meta-guidance activates when:

```text
G_k ≤ τ_M for all islands
```

Typical threshold:

```text
τ_M = 0.12
```

## 16. Meta-Guidance Procedure

The system invokes a separate LLM to analyze:

- problem specification
- evaluator code
- global best program
- previous tactics

The LLM generates new solution tactics.

## 17. Tactic Generation Process

The tactic generator performs:

### Stage 1 - Analyze evaluator

Identify:

- objective
- constraints
- penalties
- failure modes

### Stage 2 - Analyze best solution

Determine:

- algorithmic approach
- performance bottlenecks
- limiting factors

### Stage 3 - Review past tactics

Avoid repeating failed strategies.

### Stage 4 - Generate diverse strategies

Examples:

- switch greedy -> dynamic programming
- introduce heuristic pruning
- use better data structures
- parallelize search
- use caching
- change objective surrogate

### Stage 5 - Specify implementation

Each tactic must include:

- idea
- implementation steps
- target metric
- cautions
- approach type

### Stage 6 - Sanity check

Ensure:

- feasible
- non-redundant
- aligned with evaluator

## 18. Injecting Tactics into Evolution

Tactics are inserted into mutation prompts:

```text
BREAKTHROUGH IDEA – IMPLEMENT THIS

IDEA: <idea>

HOW TO IMPLEMENT:
<implementation>

TARGET METRIC:
<metric>

CAUTIONS:
<risks>
```

The mutation step must explicitly implement the tactic.

## 19. Main Algorithm

```python
Initialize islands
Initialize global best

for t = 1 → T:

    select island via UCB

    compute exploration intensity

    sample parent and inspiration programs

    build mutation prompt

    child = LLM(prompt)

    evaluate child

    add child to archive

    update adaptive state

    if global stagnation:
        generate tactics

Return:

best program across all islands
```

## 20. Time-Scale Separation

The three adaptive levels operate at different speeds.

| Level | Function | Frequency |
| --- | --- | --- |
| Level 1 | exploration intensity | every iteration |
| Level 2 | island resource allocation | every iteration |
| Level 3 | conceptual strategy generation | only during stagnation |

## 21. Key Design Principles

1. Replace fixed hyperparameters with adaptive signals. All decisions derive from fitness improvement history.
2. Treat LLM as semantic mutation operator. LLM generates algorithmic variations, not syntax noise.
3. Maintain diverse solution populations. Islands prevent premature convergence.
4. Allocate compute dynamically. Bandit routing directs compute toward productive search regions.
5. Introduce conceptual jumps when necessary. Meta-guidance generates algorithmic breakthroughs.

## 22. Summary of Core Signals

| Signal | Meaning |
| --- | --- |
| `δ` | normalized improvement |
| `G_k` | productivity / gradient magnitude |
| `I_k` | exploration probability |
| `R_k` | decayed reward |
| `V_k` | decayed visit count |

## 23. Minimal User Configuration

The framework requires only:

- LLM model
- initial program
- fitness evaluator
- iteration budget

All other behaviors emerge from the adaptive loops.
