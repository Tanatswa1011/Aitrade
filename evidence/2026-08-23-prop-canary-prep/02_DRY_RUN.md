# Structural dry-run (no broker transmission)

Captured from a test fixture after operator-arm semantics, with a genuine `phase54_live` signal newer than `armed_at`.

This is **not** a live-market dry-run and **not** a broker acceptance.

```
verdict: PROP_CANARY_DRY_RUN_PASS
submitted: false
transmitted: false
broker_ack: null
PROP_EXECUTION: false
account: FNFTCHTANATSWAPHILMU92044
quantity: 1
instrument: MNQ 09-26
entry_line: PLACE;FNFTCHTANATSWAPHILMU92044;MNQ SEP26;BUY;1;MARKET;0;0;DAY;;AITRADE_FN_CANARY_fixture-live_ENTRY;NQ_DRIFT_VWAP_PULLBACK;AITRADE_FN_CANARY_fixture-live
sim101_in_line: false
stop_account: FNFTCHTANATSWAPHILMU92044
stop_qty: 1
route: NINJATRADER_ATI_FUNDEDNEXT_CANARY
order_submission: NINJATRADER_ATI_OIF
account_data_source: FUNDEDNEXT_MCP
```

Protective-order configuration is planned as fill-then-OCO children on the same FundedNext account / MNQ SEP26 / qty 1. Child prices are not faked in dry-run.

A closed-market live snapshot dry-run must return `PROP_CANARY_DRY_RUN_FAIL` until `PHASE_55B_0_PASS`.
