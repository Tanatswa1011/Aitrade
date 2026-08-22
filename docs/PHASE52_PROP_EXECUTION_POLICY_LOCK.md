# Phase 52 — Prop Execution Policy Lock

## Executive summary

Phase 52 builds a **machine-enforceable prop execution policy** between the frozen NQ Drift VWAP Pullback signals and order intent. It does not retune the strategy. Execution remains **DRY_RUN**.

**Verdict: `PROP_EXECUTION_POLICY_LOCKED`**

Selected FundedNext Flex 50K policy: **GOV2_DEMOTE_NEAR** — E preserves C pass/breach/speed and adds demotion+near-target

| Metric | Selected policy |
| --- | --- |
| FAST quantity | 2 MNQ |
| SAFE quantity | 1 MNQ |
| Daily governor | `daily_loss >= 0.35 * remaining_dd_at_session_open` |
| Baseline P(pass) | 55.9% |
| Baseline P(breach) (firm MLL) | 0.0% |
| Baseline P(fail) = 1−P(pass) | 44.1% (timeout / stall; policy blocks the last-unit trade that Phase 49B counted as breach) |
| Median pass days | 14 |
| P(pass ≤14d) among passers | 52.2% |
| Expectancy −10% P(pass) | 56.0% (median 17d) |
| 10% winner→loser flip P(pass) | 15.1% |

FAST is a privilege. Degradation demotes `EVAL_FAST → EVAL_PROTECTED` (1 MNQ). Quantity never increases after losses, to catch up, or to recover drawdown. 3 MNQ is rejected.

## 1. Freeze integrity

- GC hash: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ hash: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- assert_frozen: `True`
- Paper journals empty: `True`
- Prior Phase 49/50/51 report fingerprints unchanged: `True`
- execution_default: `DRY_RUN`

## 2. Account state machine

States: `EVAL_SAFE`, `EVAL_FAST`, `EVAL_PROTECTED`, `EVAL_NEAR_TARGET`, `EVAL_DAILY_STOPPED`, `EVAL_BREACHED`, `EVAL_PASSED`, `FUNDED_SAFE`, `FUNDED_PROTECTED`, `PAUSED`.

FundedNext candidate starts `EVAL_FAST`. Transitions are functions of equity, MLL, daily governor, near-target remainder, degradation flag, and integrity kills — no discretionary interpretation. See `reports/phase52_state_machine/`.

## 3. FundedNext Flex 50K rule engine

Canonical catalog: `reports/phase52_rule_engine/fn_flex_50k_catalog.json`.

Material evaluation-survival rules are sourced from `config/PROP_RULES_V1.json`. Firm daily loss limit is **NONE**; the 35% remaining-DD stop is an **AITRADE internal governor**. Firm news trading is **ALLOWED**; AITRADE still enforces ±5 minutes and fail-closed calendar handling.

## 4. Daily governor equation

At each Chicago Globex session open (17:00 CT):

```
remaining_drawdown_open = session_open_equity - mll
daily_stop_threshold    = 0.35 * remaining_drawdown_open
daily_loss              = session_open_equity - marked_equity
```

`marked_equity` includes realized P&L, unrealized P&L, and fees already in fills. Threshold is frozen for the session (EOD trailing MLL does not move intra-day). On trigger: block new entries, cancel pending entries, **do not flatten** (protective stops may remain) unless the firm mandatory-flat window requires it. Gap-through clips the fill to the remaining daily room in simulation, then `EVAL_DAILY_STOPPED` until the next 17:00 CT session.

## 5. Position sizing

```
allowed_qty = min(strategy_qty_cap, prop_contract_cap, drawdown_based_qty_cap, daily_governor_qty_cap, state_based_qty_cap)
```

- FAST = 2 MNQ, SAFE/PROTECTED/NEAR_TARGET = 1 MNQ, max = 2, reject 3.
- Not 1% of $50,000. `risk_per_trade.mode = PROP_CONTRACT_QTY`, unit risk $160 / MNQ at the frozen 80-pt stop.
- `propose_size` returns `PROP_QTY_LOCKED` in DRY_RUN.

## 6. FAST → PROTECTED demotion

Transparent rolling window (min 20 trades, roll 30):

- Warning: E[R] < 50% frozen, WR −8pp, loss streak 4, winner/loser degradation.
- Hard demote: E[R] < 0 **and** (WR −8pp or streak 4); or WR −12pp with E[R] < 50% frozen; or streak 5; or WR collapse ≥18pp.
- Recovery requires 40 subsequent qualifying trades and **does not restore FAST during the evaluation**.

## 7. Kill switches

See `reports/phase52_prop_policy/kill_switches.json`. Safety blocks/cancels/pauses; flatten only when position integrity, hash mismatch, unknown equity, invalid DD, max position, or imminent breach require it. Daily stop does **not** flatten.

## 8. News blackout

Internal AITRADE lock: **±5 minutes** around restricted events (`nq_post_news_models` defaults) plus family-port clock window **08:25–08:35 ET**. Missing/stale calendar → fail closed. Existing protective stops may remain.

## 9. Near-target

Selected near-target rule: `PCT_95` (remaining profit ≤ $125, 5% of the $2,500 target). Screened against 1 FAST R ($320), 1 SAFE R ($160), and 80/90/95% of target. Execution size only; frozen signal logic unchanged. 2 MNQ → 1 MNQ in `EVAL_NEAR_TARGET`.

## 10–11. Stress and Pareto

Baseline variants A–E: `reports/phase52_pareto/`. Degradation and execution stress: `reports/phase52_stress/`. Policy is **not** optimized for sub-14-day passing.

## 12. Machine-readable policy

- `config/aitrade_prop_execution_policy_v1.json`
- `config/aitrade_operating_policy_v1.json` (`execution_default=DRY_RUN`, `broker_execution=false`)

## 13. Tests

`tests_phase52.py` returncode `0` — `True`.

## 14. Remaining REQUIRES_CONFIRMATION (not eval-survival)

[
  {
    "canonical_rule": "CME_PRODUCT_LIMIT_PCT",
    "source": "PROP_RULES_V1.general_compliance.cme_product_limit_pct",
    "threshold": "REQUIRES_CONFIRMATION",
    "applies": "EVALUATION",
    "trigger_event": "limit_pct",
    "machine_calculation": "unknown exact product limit %",
    "enforcement": "fail closed if in zone already",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  },
  {
    "canonical_rule": "AUTOMATION_ALLOWED",
    "source": "PROP_RULES_V1.general_compliance.automation_allowed",
    "threshold": "REQUIRES_CONFIRMATION",
    "applies": "LIVE_ENABLEMENT",
    "trigger_event": "go_live",
    "machine_calculation": "unconfirmed",
    "enforcement": "DRY_RUN only until confirmed",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  },
  {
    "canonical_rule": "COPY_TRADING",
    "source": "PROP_RULES_V1.general_compliance.copy_trading",
    "threshold": "REQUIRES_CONFIRMATION",
    "applies": "MULTI_ACCOUNT",
    "trigger_event": "replication",
    "machine_calculation": "independent accounts",
    "enforcement": "not used in single-eval policy",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  },
  {
    "canonical_rule": "PAYOUT_FREQUENCY",
    "source": "PROP_RULES_V1.payout.payout_frequency",
    "threshold": "REQUIRES_CONFIRMATION",
    "applies": "FUNDED",
    "trigger_event": "payout",
    "machine_calculation": "unconfirmed",
    "enforcement": "not eval survival",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  },
  {
    "canonical_rule": "FIRST_PAYOUT_BUFFER",
    "source": "PROP_RULES_V1.payout.first_payout_required_buffer",
    "threshold": "REQUIRES_CONFIRMATION",
    "applies": "FUNDED",
    "trigger_event": "payout",
    "machine_calculation": "unconfirmed",
    "enforcement": "not eval survival",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  },
  {
    "canonical_rule": "MAX_FUNDED_ACCOUNTS",
    "source": "PROP_RULES_V1.funded.max_funded_accounts",
    "threshold": "not set on FN funded",
    "applies": "FUNDED",
    "trigger_event": "purchase",
    "machine_calculation": "REQUIRES_CONFIRMATION from Phase 51",
    "enforcement": "not eval survival",
    "reset": "n/a",
    "material_eval_survival": false,
    "status": "REQUIRES_CONFIRMATION"
  }
]

## 15. What this phase did not do

No live trading. No evaluation account purchase. No frozen-strategy edit. No overwrite of Phase 49/49B/50/51 research reports. No sub-14-day speed search.
