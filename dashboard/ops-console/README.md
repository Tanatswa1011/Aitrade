# AITRADE Operations Console · Phase 54

Integrated control API + UI. Visual base is the Phase 54 mock; live state comes from NinjaTrader (market/position) and FundedNext MCP (account/risk). Never Sim101 for eval money.

```text
python dashboard/ops-console/api.py
```

Open `http://127.0.0.1:8765/`. Bound to `127.0.0.1` only.

`PROP_EXECUTION=false`. Order execution remains DISABLED. No orders are submitted to Sim101 or FundedNext. No Enable Execution button.
