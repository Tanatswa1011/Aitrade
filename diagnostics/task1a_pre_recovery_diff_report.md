# Task 1A pre-recovery diff report

Captured after the failed Task 1 suite and before recovery edits.

## Repository state

- Branch: `main`
- HEAD: `1e0a88382969bf91a405d17316e84832fa492cc8`
- Modified tracked files: 10
- Diff summary: 82 insertions, 11 deletions

## Classification

| File | Diff | Last write (local) | Classification |
|---|---:|---|---|
| `journal/phase54_ops/soak.json` | 3 lines replaced | 2026-08-22 18:23:00 | Present before failed suite; preserve |
| `journal/phase54_ops/telemetry.jsonl` | 67 lines added | 2026-08-22 18:22:37 | Present before failed suite; preserve |
| `journal/phase31_nq_dvp_sim/live_events.jsonl` | 4 lines added | 2026-08-23 17:06:05 | Failed-suite generated |
| `journal/phase31_nq_dvp_sim/runner_state.json` | 1 line replaced | 2026-08-23 17:06:05 | Failed-suite generated |
| `journal/phase53_fn_flex_shadow/audit.jsonl` | 1 line replaced | 2026-08-23 17:06:07 | Failed-suite generated |
| `state/phase54_ops.json` | 2 lines replaced | 2026-08-23 17:06:20 | Failed-suite generated |
| `strategy_frozen/gc_vwap_v2_phase26.json` | freeze timestamp only | 2026-08-23 17:05:05 | Failed-suite generated |
| `strategy_frozen/gc_vwap_v2_phase26.md` | freeze timestamp only | 2026-08-23 17:05:05 | Failed-suite generated |
| `strategy_frozen/nq_dvp_phase30.json` | freeze timestamp only | 2026-08-23 17:05:05 | Failed-suite generated |
| `strategy_frozen/nq_dvp_phase30.md` | freeze timestamp only | 2026-08-23 17:05:05 | Failed-suite generated |

## Frozen artifact diff

All four frozen-artifact diffs change only `freeze_timestamp` from the committed
2026-08-17 value to a 2026-08-23 timestamp. Canonical strategy config hashes
remain unchanged. No trading rule, parameter, semantic payload, or policy binding
changed.

Before-suite canonical file SHA-256 values:

- GC JSON: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- NQ JSON: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

After-suite file SHA-256 values:

- GC JSON: `fd5f2d7154e39e0a00c26401e9f1d860a212e65dbb047b01a32d8289f26203a0`
- NQ JSON: `b382ca93367785639b2708c6dbe39e7a1e8b5e5fae508531f462e9985eb5ea7d`

## Root-cause evidence

- `tests_phase26.FreezeImmutabilityTests.setUpClass` and
  `tests_phase26.EquivalenceTests.setUpClass` call `gc_vwap_freeze.write_frozen_files()`.
- `tests_phase30.FreezeImmutabilityTests.setUpClass` and
  `tests_phase30.EquivalenceTests.setUpClass` call `nq_dvp_freeze.write_frozen_files()`.
- Both production writers use relative `strategy_frozen/` constants and generate a
  fresh current timestamp when no document is supplied.
- `nq_dvp_live_runner` resolves Phase 31 mutable paths directly under the repository.
- `phase53_engine` resolves its audit path directly under the repository.
- `phase54_ops.STATE_PATH` resolves directly under production `state/` even in test mode.
- `fundednext_mcp_oauth.OAUTH_PATH` always resolves to the real protected OAuth file.

Runtime journal content and OAuth content are deliberately omitted from this report.
