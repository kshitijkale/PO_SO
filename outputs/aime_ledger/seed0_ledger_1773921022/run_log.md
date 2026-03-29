# Run Log — outputs/aime_ledger/seed0_ledger_1773921022

Started: 2026-03-19T17:20:29  
Trainset size: 45  
Valset size: 45  

---

## Iteration 1

- **Selected**: prompt #0 (val=0.400) → `states/iter_001_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [1, 27, 35]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 1 | 1.000 | `questions/iter_001_mb_0_1.json` |
  | 1 | 27 | 1.000 | `questions/iter_001_mb_1_27.json` |
  | 2 | 35 | 0.000 | `questions/iter_001_mb_2_35.json` |

- **Ledger injected** (system_prompt): 0 entries, 0 chars → `ledger/iter_001_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_001_system_prompt_prompt.txt` → `llm_calls/iter_001_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 2/3) → **REJECTED** | full decision: `states/iter_001_08_decision.json`

## Iteration 2

- **Selected**: prompt #0 (val=0.400) → `states/iter_002_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [0, 23, 37]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 0 | 1.000 | `questions/iter_002_mb_0_0.json` |
  | 1 | 23 | 0.000 | `questions/iter_002_mb_1_23.json` |
  | 2 | 37 | 0.000 | `questions/iter_002_mb_2_37.json` |

- **Ledger injected** (system_prompt): 1 entries, 445 chars → `ledger/iter_002_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_002_system_prompt_prompt.txt` → `llm_calls/iter_002_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 1/3) → **ACCEPTED** as #1 | full decision: `states/iter_002_08_decision.json`
- **Valset run**: avg=0.400, 45 examples → `states/iter_002_10_valset.json`

  | ID | Score | File |
  |----|-------|------|
  | 0 | 1.000 | `valset/iter_002_val_0.json` |
  | 23 | 1.000 | `valset/iter_002_val_23.json` |
  | 37 | 0.000 | `valset/iter_002_val_37.json` |
  | 1 | 1.000 | `valset/iter_002_val_1.json` |
  | 2 | 0.000 | `valset/iter_002_val_2.json` |
  | 3 | 1.000 | `valset/iter_002_val_3.json` |
  | 4 | 1.000 | `valset/iter_002_val_4.json` |
  | 5 | 1.000 | `valset/iter_002_val_5.json` |
  | 6 | 1.000 | `valset/iter_002_val_6.json` |
  | 7 | 0.000 | `valset/iter_002_val_7.json` |
  | 8 | 0.000 | `valset/iter_002_val_8.json` |
  | 9 | 0.000 | `valset/iter_002_val_9.json` |
  | 10 | 1.000 | `valset/iter_002_val_10.json` |
  | 11 | 0.000 | `valset/iter_002_val_11.json` |
  | 12 | 0.000 | `valset/iter_002_val_12.json` |
  | 13 | 0.000 | `valset/iter_002_val_13.json` |
  | 14 | 1.000 | `valset/iter_002_val_14.json` |
  | 15 | 0.000 | `valset/iter_002_val_15.json` |
  | 16 | 0.000 | `valset/iter_002_val_16.json` |
  | 17 | 1.000 | `valset/iter_002_val_17.json` |
  | 18 | 0.000 | `valset/iter_002_val_18.json` |
  | 19 | 1.000 | `valset/iter_002_val_19.json` |
  | 20 | 1.000 | `valset/iter_002_val_20.json` |
  | 21 | 1.000 | `valset/iter_002_val_21.json` |
  | 22 | 1.000 | `valset/iter_002_val_22.json` |
  | 24 | 1.000 | `valset/iter_002_val_24.json` |
  | 25 | 0.000 | `valset/iter_002_val_25.json` |
  | 26 | 1.000 | `valset/iter_002_val_26.json` |
  | 27 | 0.000 | `valset/iter_002_val_27.json` |
  | 28 | 0.000 | `valset/iter_002_val_28.json` |
  | 29 | 0.000 | `valset/iter_002_val_29.json` |
  | 30 | 0.000 | `valset/iter_002_val_30.json` |
  | 31 | 0.000 | `valset/iter_002_val_31.json` |
  | 32 | 0.000 | `valset/iter_002_val_32.json` |
  | 33 | 0.000 | `valset/iter_002_val_33.json` |
  | 34 | 0.000 | `valset/iter_002_val_34.json` |
  | 35 | 1.000 | `valset/iter_002_val_35.json` |
  | 36 | 0.000 | `valset/iter_002_val_36.json` |
  | 38 | 0.000 | `valset/iter_002_val_38.json` |
  | 39 | 0.000 | `valset/iter_002_val_39.json` |
  | 40 | 0.000 | `valset/iter_002_val_40.json` |
  | 41 | 0.000 | `valset/iter_002_val_41.json` |
  | 42 | 0.000 | `valset/iter_002_val_42.json` |
  | 43 | 1.000 | `valset/iter_002_val_43.json` |
  | 44 | 0.000 | `valset/iter_002_val_44.json` |


## Iteration 3

- **Selected**: prompt #0 (val=0.400) → `states/iter_003_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [14, 12, 7]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 14 | 1.000 | `questions/iter_003_mb_0_14.json` |
  | 1 | 12 | 0.000 | `questions/iter_003_mb_1_12.json` |
  | 2 | 7 | 1.000 | `questions/iter_003_mb_2_7.json` |

- **Ledger injected** (system_prompt): 1 entries, 445 chars → `ledger/iter_003_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_003_system_prompt_prompt.txt` → `llm_calls/iter_003_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 2/3) → **REJECTED** | full decision: `states/iter_003_08_decision.json`

## Iteration 4

- **Selected**: prompt #1 (val=0.400) → `states/iter_004_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [44, 42, 34]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 44 | 1.000 | `questions/iter_004_mb_0_44.json` |
  | 1 | 42 | 0.000 | `questions/iter_004_mb_1_42.json` |
  | 2 | 34 | 1.000 | `questions/iter_004_mb_2_34.json` |

- **Ledger injected** (system_prompt): 0 entries, 0 chars → `ledger/iter_004_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_004_system_prompt_prompt.txt` → `llm_calls/iter_004_system_prompt_response.txt`
- **New prompt score**: 2/3 (old: 2/3) → **REJECTED** | full decision: `states/iter_004_08_decision.json`

## Iteration 5

- **Selected**: prompt #0 (val=0.400) → `states/iter_005_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [21, 5, 6]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 21 | 0.000 | `questions/iter_005_mb_0_21.json` |
  | 1 | 5 | 0.000 | `questions/iter_005_mb_1_5.json` |
  | 2 | 6 | 0.000 | `questions/iter_005_mb_2_6.json` |

- **Ledger injected** (system_prompt): 2 entries, 728 chars → `ledger/iter_005_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_005_system_prompt_prompt.txt` → `llm_calls/iter_005_system_prompt_response.txt`
- **New prompt score**: 0/3 (old: 0/3) → **REJECTED** | full decision: `states/iter_005_08_decision.json`

## Iteration 6

- **Selected**: prompt #0 (val=0.400) → `states/iter_006_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [11, 20, 15]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 11 | 0.000 | `questions/iter_006_mb_0_11.json` |
  | 1 | 20 | 1.000 | `questions/iter_006_mb_1_20.json` |
  | 2 | 15 | 1.000 | `questions/iter_006_mb_2_15.json` |

- **Ledger injected** (system_prompt): 3 entries, 1025 chars → `ledger/iter_006_system_prompt.txt`
- **LLM call** (system_prompt): `llm_calls/iter_006_system_prompt_prompt.txt` → `llm_calls/iter_006_system_prompt_response.txt`
- **New prompt score**: 3/3 (old: 2/3) → **ACCEPTED** as #2 | full decision: `states/iter_006_08_decision.json`
- **Valset run**: avg=0.422, 45 examples → `states/iter_006_10_valset.json`

  | ID | Score | File |
  |----|-------|------|
  | 11 | 1.000 | `valset/iter_006_val_11.json` |
  | 15 | 1.000 | `valset/iter_006_val_15.json` |
  | 20 | 1.000 | `valset/iter_006_val_20.json` |
  | 0 | 0.000 | `valset/iter_006_val_0.json` |
  | 1 | 0.000 | `valset/iter_006_val_1.json` |
  | 2 | 0.000 | `valset/iter_006_val_2.json` |
  | 3 | 1.000 | `valset/iter_006_val_3.json` |
  | 4 | 1.000 | `valset/iter_006_val_4.json` |
  | 5 | 0.000 | `valset/iter_006_val_5.json` |
  | 6 | 0.000 | `valset/iter_006_val_6.json` |
  | 7 | 1.000 | `valset/iter_006_val_7.json` |
  | 8 | 0.000 | `valset/iter_006_val_8.json` |
  | 9 | 0.000 | `valset/iter_006_val_9.json` |
  | 10 | 0.000 | `valset/iter_006_val_10.json` |
  | 12 | 0.000 | `valset/iter_006_val_12.json` |
  | 13 | 0.000 | `valset/iter_006_val_13.json` |
  | 14 | 1.000 | `valset/iter_006_val_14.json` |
  | 16 | 0.000 | `valset/iter_006_val_16.json` |
  | 17 | 1.000 | `valset/iter_006_val_17.json` |
  | 18 | 0.000 | `valset/iter_006_val_18.json` |
  | 19 | 1.000 | `valset/iter_006_val_19.json` |
  | 21 | 0.000 | `valset/iter_006_val_21.json` |
  | 22 | 1.000 | `valset/iter_006_val_22.json` |
  | 23 | 0.000 | `valset/iter_006_val_23.json` |
  | 24 | 1.000 | `valset/iter_006_val_24.json` |
  | 25 | 1.000 | `valset/iter_006_val_25.json` |
  | 26 | 1.000 | `valset/iter_006_val_26.json` |
  | 27 | 0.000 | `valset/iter_006_val_27.json` |
  | 28 | 0.000 | `valset/iter_006_val_28.json` |
  | 29 | 0.000 | `valset/iter_006_val_29.json` |
  | 30 | 0.000 | `valset/iter_006_val_30.json` |
  | 31 | 0.000 | `valset/iter_006_val_31.json` |
  | 32 | 0.000 | `valset/iter_006_val_32.json` |
  | 33 | 1.000 | `valset/iter_006_val_33.json` |
  | 34 | 1.000 | `valset/iter_006_val_34.json` |
  | 35 | 1.000 | `valset/iter_006_val_35.json` |
  | 36 | 0.000 | `valset/iter_006_val_36.json` |
  | 37 | 1.000 | `valset/iter_006_val_37.json` |
  | 38 | 1.000 | `valset/iter_006_val_38.json` |
  | 39 | 0.000 | `valset/iter_006_val_39.json` |
  | 40 | 0.000 | `valset/iter_006_val_40.json` |
  | 41 | 0.000 | `valset/iter_006_val_41.json` |
  | 42 | 0.000 | `valset/iter_006_val_42.json` |
  | 43 | 1.000 | `valset/iter_006_val_43.json` |
  | 44 | 0.000 | `valset/iter_006_val_44.json` |


## Iteration 7

- **Selected**: prompt #2 (val=0.422) → `states/iter_007_02_selection_and_minibatch.json`
- **Minibatch** (3 examples): IDs [10, 43, 29]

  | # | ID | Score | File |
  |---|-----|-------|------|
  | 0 | 10 | N/A | `questions/iter_007_mb_0_10.json` |
  | 1 | 43 | N/A | `questions/iter_007_mb_1_43.json` |
  | 2 | 29 | N/A | `questions/iter_007_mb_2_29.json` |

- **Ledger injected**: 0 entries (first iteration or no prior rejections)

