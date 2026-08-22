# 05 — NQ bar-structure plumbing

Verdict: **`NQ_BAR_PLUMBING_PASS_CLOSED_MARKET`**

Timezone used by the live path: `America/New_York` (`nq_drift_vwap_models.OR_TIMEZONE`, `nq_dvp_live_feed.NY`).

## Runtime (closed CME)

From `03_TELEMETRY_SCHEMA.json` and desk `telemetry_dump`:

- NQ recognised: `diagnostics.nq_found=true`, `nq.instrument=NQ 09-26`, `diagnostics.nq_name=NQ 09-26`
- MNQ recognised: `mnq.instrument=MNQ 09-26`
- `nq_bars_1m` present (list)
- `nq_bars_1m_count=0`
- `nq_bars_1m_status=WAITING`
- `diagnostics.nq_1m_bars_request=true`
- last NQ bar ts: null

Zero bars on Saturday is **WAITING / WARMING_UP**, not a live signal. Desk `decision.signal_source=NONE`, `last_live_signal=null`. Shadow `phase53_shadow` remains non-executable.

## Python 1m consumer

`nq_dvp_live_feed.load_nt_1m_bars` reads `nq_bars_1m`, drops `finalized=false` and malformed rows (`_as_bar` returns None).

## Fixture results (`tests_phase55b.LiveBarTests`, all ok in `07_TEST_RESULTS.txt`)

| Test | Proof |
|---|---|
| `test_5m_and_15m_finalization` | 15×1m → 3×5m and 1×15m via `aggregate_1m_to_ny` |
| `test_timezone_and_dst` | winter `-05:00`, summer `-04:00`; ids are NY not Berlin/UTC |
| `test_partial_bar_excluded` | `finalized=false` dropped; 1 of 2 rows kept |
| `test_session_closed_and_open` | closed → `SESSION_CLOSED` / not executable |
| `test_warmup_boundary_not_executable` | warmup merge cannot become executable live |
| `test_unique_signal_ids` | `NQ:2026-08-21T10:45:00-04:00:5m` |

Saturday live Task 3 also ran the aggregator on fixtures: 1m→5m (3 bars), 1m→15m (1 bar), NY ids (example `…T10:45:00-04:00:5m`).

## Not proven until Globex live

Advancing 1m timestamps, live 5m/15m growth, duplicate/gap behaviour on the real feed.
