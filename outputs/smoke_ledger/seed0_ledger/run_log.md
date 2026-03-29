# Run Log — outputs/smoke_ledger/seed0_ledger

Started: 2026-03-19T16:37:43  
Trainset size: 9  
Valset size: 9  

---

## Iteration 1

- **Selected**: prompt #0 (val=0.556) → `states/iter_001_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [7, 8, 1]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 7 | 1.000 | `questions/iter_001_mb_0_7.json` |
  | 1 | 8 | 1.000 | `questions/iter_001_mb_1_8.json` |
  | 2 | 1 | 1.000 | `questions/iter_001_mb_2_1.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)

## Iteration 2

- **Selected**: prompt #0 (val=0.556) → `states/iter_002_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [5, 3, 4]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 5 | 0.000 | `questions/iter_002_mb_0_5.json` |
  | 1 | 3 | 0.000 | `questions/iter_002_mb_1_3.json` |
  | 2 | 4 | 1.000 | `questions/iter_002_mb_2_4.json` |

- **Ledger injected** (system_prompt): 0 entries, 0 chars → `ledger/iter_002_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_002_system_prompt_prompt.txt` → `llm_calls/iter_002_system_prompt_response.txt`
- **New prompt score**: 0/3 (old: 1/3) → **REJECTED** | full decision: `states/iter_002_08_decision.json`

## Iteration 3

- **Selected**: prompt #0 (val=0.556) → `states/iter_003_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [2, 0, 6]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 2 | 0.000 | `questions/iter_003_mb_0_2.json` |
  | 1 | 0 | 1.000 | `questions/iter_003_mb_1_0.json` |
  | 2 | 6 | 1.000 | `questions/iter_003_mb_2_6.json` |

- **Ledger injected** (system_prompt): 1 entries, 348 chars → `ledger/iter_003_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_003_system_prompt_prompt.txt` → `llm_calls/iter_003_system_prompt_response.txt`
- **New prompt score**: 1/3 (old: 2/3) → **REJECTED** | full decision: `states/iter_003_08_decision.json`

## Iteration 4

- **Selected**: prompt #0 (val=0.556) → `states/iter_004_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [6, 5, 3]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 6 | 1.000 | `questions/iter_004_mb_0_6.json` |
  | 1 | 5 | 0.000 | `questions/iter_004_mb_1_5.json` |
  | 2 | 3 | 0.000 | `questions/iter_004_mb_2_3.json` |

- **Ledger injected** (system_prompt): 2 entries, 504 chars → `ledger/iter_004_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_004_system_prompt_prompt.txt` → `llm_calls/iter_004_system_prompt_response.txt`
- **New prompt score**: 1/3 (old: 1/3) → **REJECTED** | full decision: `states/iter_004_08_decision.json`

---

**Total iterations:** 3  
**Total metric calls:** 30  
**Best candidate:** #0 (val=0.556)  
