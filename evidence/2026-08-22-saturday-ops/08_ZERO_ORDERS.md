# 08 — Zero orders

Saturday validation transmitted **no** broker orders.

| Proof | Evidence |
|---|---|
| AddOn `orders_transmitted` | **0** on A1, A4, A7, B-polls, and pack capture (`03_TELEMETRY_SCHEMA.json`) |
| Incoming OIF | `Documents\NinjaTrader 8\incoming` empty (pack capture `incoming_files=[]`) |
| Outgoing OIF | `outgoing\*.oif` empty |
| Desk `incoming_oif` | `[]` |
| Sim101 arm | DISARMED; `AITRADE_SIM_ONLY_EXECUTION` unset; config `SIM_ONLY_EXECUTION: false` |
| PROP_EXECUTION | false (module + dump + snapshot) |
| FundedNext | READ_ONLY; MCP read allowlist only; never `/order/placeorder`; never OIF allowlist |
| Engine | STOPPED entire Saturday gate sequence after planned stop |
| AddOn | read-only snapshot writer; tests: no PLACE / CLOSEPOSITION |

No Sim101 ATI submit was armed or called on the live path. Gate tests prove `drop_oif` is not reached when unarmed.

If a command had been sent, incoming OIF and/or `orders_transmitted` would change. Both stayed empty/zero.
