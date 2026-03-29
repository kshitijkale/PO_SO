# Run Log — outputs/aime_diary_smoke/seed0_diary

Started: 2026-03-21T10:28:32  
Trainset size: 45  
Valset size: 5  

---

## Iteration 1

- **Selected**: prompt #0 (val=0.400) → `states/iter_001_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [1, 27, 35]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 1 | 1.000 | `questions/iter_001_mb_0_1.json` |
  | 1 | 27 | 1.000 | `questions/iter_001_mb_1_27.json` |
  | 2 | 35 | 0.000 | `questions/iter_001_mb_2_35.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)
- **LLM call** (system_prompt): `llm_calls/iter_001_system_prompt_prompt.txt` → `llm_calls/iter_001_system_prompt_response.txt`
- **New prompt score**: 3/3 (old: 2/3) → **ACCEPTED** as #1 | full decision: `states/iter_001_08_decision.json`
- **Valset run**: avg=0.600, 5 examples → `states/iter_001_10_valset.json`

  | ID | Score | File |
  |----|-------|------|
  | 1 | 1.000 | `valset/iter_001_val_1.json` |
  | 0 | 0.000 | `valset/iter_001_val_0.json` |
  | 2 | 0.000 | `valset/iter_001_val_2.json` |
  | 3 | 1.000 | `valset/iter_001_val_3.json` |
  | 4 | 1.000 | `valset/iter_001_val_4.json` |


## Iteration 2

- **Selected**: prompt #1 (val=0.600) → `states/iter_002_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [0, 23, 37]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 0 | 0.000 | `questions/iter_002_mb_0_0.json` |
  | 1 | 23 | 1.000 | `questions/iter_002_mb_1_23.json` |
  | 2 | 37 | 0.000 | `questions/iter_002_mb_2_37.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)
- **LLM call** (system_prompt): `llm_calls/iter_002_system_prompt_prompt.txt` → `llm_calls/iter_002_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 1/3) → **ACCEPTED** as #2 | full decision: `states/iter_002_08_decision.json`
- **Valset run**: avg=0.800, 5 examples → `states/iter_002_10_valset.json`

  | ID | Score | File |
  |----|-------|------|
  | 0 | 1.000 | `valset/iter_002_val_0.json` |
  | 1 | 1.000 | `valset/iter_002_val_1.json` |
  | 2 | 0.000 | `valset/iter_002_val_2.json` |
  | 3 | 1.000 | `valset/iter_002_val_3.json` |
  | 4 | 1.000 | `valset/iter_002_val_4.json` |


## Iteration 3

- **Selected**: prompt #2 (val=0.800) → `states/iter_003_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [14, 12, 7]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 14 | 1.000 | `questions/iter_003_mb_0_14.json` |
  | 1 | 12 | 0.000 | `questions/iter_003_mb_1_12.json` |
  | 2 | 7 | 1.000 | `questions/iter_003_mb_2_7.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)
- **LLM call** (system_prompt): `llm_calls/iter_003_system_prompt_prompt.txt` → `llm_calls/iter_003_system_prompt_response.txt`
- **New prompt score**: 1/3 (old: 2/3) → **REJECTED** | full decision: `states/iter_003_08_decision.json`

## Iteration 4

- **Selected**: prompt #2 (val=0.800) → `states/iter_004_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [44, 42, 34]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 44 | 1.000 | `questions/iter_004_mb_0_44.json` |
  | 1 | 42 | 0.000 | `questions/iter_004_mb_1_42.json` |
  | 2 | 34 | 0.000 | `questions/iter_004_mb_2_34.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)
- **LLM call** (system_prompt): `llm_calls/iter_004_system_prompt_prompt.txt` → `llm_calls/iter_004_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 1/3) → **ACCEPTED** as #3 | full decision: `states/iter_004_08_decision.json`
- **Valset run**: avg=0.600, 5 examples → `states/iter_004_10_valset.json`

  | ID | Score | File |
  |----|-------|------|
  | 0 | 0.000 | `valset/iter_004_val_0.json` |
  | 1 | 1.000 | `valset/iter_004_val_1.json` |
  | 2 | 0.000 | `valset/iter_004_val_2.json` |
  | 3 | 1.000 | `valset/iter_004_val_3.json` |
  | 4 | 1.000 | `valset/iter_004_val_4.json` |


---

**Total iterations:** 3  
**Total metric calls:** 42  
**Best candidate:** #2 (val=0.800)  
