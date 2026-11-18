# PawPal+ AI Care Planner — Reliability Evaluation

This file lists the evidence that the AI layer is safe to put in front of the deterministic
Scheduler. Every result below comes from the automated test suite or from one manually
verified real API run. There are no accuracy percentages, because none were measured.

## How to reproduce

```bash
python -m pytest
```

Current result: **48 passed** (17 original PawPal+ tests + 31 AI planner tests in
`tests/test_ai_planner.py`). The AI tests use fake AI clients, so they need **no network
and no API key**, and they cost nothing.

## Validation contract under test

An AI response is accepted only if **all** of these hold. Otherwise the **whole**
recommendation is rejected and the original Scheduler runs on the unchanged Owner
(`used_ai=False`, plus a warning):

- The response is a JSON object with exactly `available_minutes`, `adjustments`, and `summary`.
- `available_minutes` is `null` or an integer from 1 up to the owner's own setting.
- Each adjustment has exactly `task_id`, `pet`, `new_priority`, and `reason`, all strings.
- `task_id` is one of the IDs sent (`t1`, `t2`, …), appears only once, and belongs to the named `pet`.
- `new_priority` is `low`, `medium`, or `high`.
- A task the owner set to `high` is never given a lower priority.

## Representative cases

| # | Case | Test | Expected | Result |
|---|------|------|----------|--------|
| 1 | Valid recommendation (medication medium → high) | `test_valid_recommendation_changes_scheduling_behavior` | Accepted, `used_ai=True`; medication now scheduled; plan still within 45 min | ✅ Pass |
| 2 | Malformed JSON (`"Sure! Here's the plan: {not json"`) | `test_malformed_json_falls_back` | Fallback to standard plan | ✅ Pass |
| 3 | Unknown task ID (`t99`) | `test_nonexistent_task_id_falls_back` | Fallback | ✅ Pass |
| 4 | Invalid priority (`"urgent"`) | `test_invalid_priority_falls_back` | Fallback | ✅ Pass |
| 5 | Lower an owner-set high task (walk high → low) | `test_lowering_owner_set_high_priority_falls_back` | Fallback | ✅ Pass |
| 6 | Increase time (owner 45 → AI 120) | `test_ai_cannot_increase_available_time_beyond_owner_setting` | Fallback; effective minutes stay 45 | ✅ Pass |
| 7 | Reduce time (owner 60 → AI 30) | `test_ai_may_reduce_available_time` | Accepted; plan fits 30 min; owner still 60 | ✅ Pass |
| 8 | API/network failure (client raises `ConnectionError`) | `test_api_client_failure_falls_back` | Fallback | ✅ Pass |
| 9 | Missing `ANTHROPIC_API_KEY` | `test_missing_api_key_falls_back_without_calling_the_api` | Fallback before any network call; warning names the missing key | ✅ Pass |
| 10 | Blank owner note | `test_blank_owner_text_skips_ai_call` | AI never called; standard plan | ✅ Pass |
| 11 | Original Owner unchanged after AI planning | `test_original_owner_and_tasks_are_unchanged_after_ai_planning` | Priorities, durations, times, completion, and minutes identical; plan uses copies | ✅ Pass |
| 12 | One good + one bad adjustment | `test_one_bad_adjustment_rejects_the_whole_recommendation` | Whole response rejected (all-or-nothing) | ✅ Pass |
| 13 | Empty adjustments list | `test_valid_empty_adjustments_are_accepted` | Accepted; same plan as standard | ✅ Pass |
| 14 | AI unavailable → identical deterministic output | `test_deterministic_behavior_unchanged_when_ai_unavailable` | Plan, skipped list, and explanation identical to `Scheduler` | ✅ Pass |

### Additional schema violations (one parametrized test, 14 cases)

`test_other_schema_violations_fall_back` covers each of these, and every case falls back:

- the response is not an object
- a top-level field is missing
- an unexpected extra field
- minutes given as a string, a boolean, or `0`
- `adjustments` is not a list
- `summary` is not a string
- an adjustment is missing a field
- an adjustment tries to add `duration_minutes`
- a nonexistent pet (`"Rex"`)
- a task assigned to the wrong pet
- a non-string `task_id`
- a duplicate `task_id`

### Supporting tests

- `test_missing_ai_sdk_or_credentials_falls_back`: the real client can't be created, so the planner falls back.
- `test_context_only_lists_pending_tasks_due_today_with_ids`: completed tasks and future tasks are never sent to the AI.
- `test_validation_rejection_and_fallback_are_logged`: the logs show the request, the rejection, and the fallback.

## Real Anthropic end-to-end run (manual, run once)

| | |
|---|---|
| Owner note | "Mochi needs her medication tonight. Biscuit has a lot of energy today, but I only have 45 minutes." |
| Claude's recommendation | Raise Mochi's Medication from **medium → high** |
| Validation | Passed (`used_ai=True`) |
| Effective time | 45 minutes (unchanged) |
| Scheduled | 1. Mochi — Medication (10 min, high) · 2. Biscuit — Feed (10 min, high) |
| Skipped | Biscuit — Walk (30 min, high): the three high tasks needed 50 min, but only 45 were available |

This single run confirms that the real path works end to end:
note → Claude → JSON → validation → deep copy → Scheduler → plan. Because it is one run,
it does **not** measure how consistent Claude's recommendations are across runs or across
different notes.
