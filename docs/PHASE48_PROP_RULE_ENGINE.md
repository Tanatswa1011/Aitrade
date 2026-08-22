# Phase 48 — Prop Rule Engine V1

`DRY_RUN`. No broker. Frozen strategy logic was not modified.

The strategy engine still only generates signals. The prop rule engine decides whether a proposed trade is **legally permitted** under a named firm profile. Risk-per-trade remains unset.

## 1. Verdict

**`PROP_RULE_ENGINE_V1_READY`**

Execution remains paused / `DRY_RUN`. No live trades. No Monte Carlo. No strategy retune.

## 2. Frozen integrity

Verified before and after. `strategy_frozen/` was not written.

| Book | Config hash | File SHA |
|------|-------------|----------|
| GC VWAP V2 | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` | match |
| NQ DVP | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` | match |

ES DVP remains `LOCKED_FORWARD_VALIDATION_CANDIDATE` (Phase 47). Not moved into `strategy_frozen/`.

## 3. Source of truth

- Schema: JSON `PROP_RULES_V1`
- File: `config/PROP_RULES_V1.json`
- Models: `prop_rules_v1.py`
- Decision API: `prop_rule_engine.evaluate_trade(...)`
- Account states: `account_state_engine.py`
- Operating policy placeholders: `config/aitrade_operating_policy_v1.json`
- Risk manager stub: `risk_manager.py` (`SIZE_PENDING_SIMULATION`)

Primary profiles: `MFFU_RAPID_EOD_50K`, `FUNDEDNEXT_FLEX_50K`.

`MFFU_RAPID_STANDARD_50K` is an `ALTERNATIVE_RESEARCH_PROFILE` with unspecified rules. The engine fails closed (`BLOCK_UNKNOWN_RULE`) rather than guessing.

## 4. Decision codes

`ALLOW`, `BLOCK_NEWS`, `BLOCK_CONTRACT_LIMIT`, `BLOCK_DRAWDOWN`, `BLOCK_DAILY_LOSS`, `BLOCK_CONSISTENCY_GOVERNOR` (advisory unless configured to block), `BLOCK_TRADING_HOURS`, `BLOCK_OVERNIGHT`, `BLOCK_PRICE_LIMIT_ZONE`, `BLOCK_INACTIVITY`, `BLOCK_ACCOUNT_LOCKOUT`, `BLOCK_UNKNOWN_RULE`.

## 5. Account states (scaffold)

Evaluation: `EVAL_NORMAL`, `EVAL_DEFENSIVE`, `EVAL_TARGET_APPROACH`, `EVAL_LOCKOUT`, `PASSED`.

Funded: `FUNDED_BUFFER_BUILD`, `FUNDED_NORMAL`, `FUNDED_PAYOUT_APPROACH`, `FUNDED_DEFENSIVE`, `FUNDED_LOCKOUT`.

No risk percentages were assigned.

## 6. Fields still REQUIRES_CONFIRMATION

See `phase48_validation.json` → `requires_confirmation`. Do not fill these by invention.

## 7. Tests

`tests_phase48.py`: 27 ran, 0 failed.

## 8. What this phase did not do

No frozen hash/config edits. No journal rewrites. No live orders. No risk-per-trade selection. No automatic live transition for either firm.
