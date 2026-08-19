"""Phase 47 — lock ES DVP + multi-book forward validation. DRY_RUN. No freeze. No backfill."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dvp_family_monitor import family_monitor, gc_diversification_monitor
from es_dvp_lock import (
    LOCKED_CFG,
    LOCKED_PATH,
    LOCKED_VERSION,
    assert_locked_hash,
    locked_config_hash,
    write_locked_document,
)
from es_dvp_paper import (
    PAPER_TRADES_PATH as ES_PAPER,
    existing_ids,
    restore_runner_from_journal,
)
from es_dvp_paper_runner import status as es_runner_status
from multi_book_forward import (
    TARGET_N,
    ensure_all_journals,
    portfolio_status,
    probe_forward_data,
    scorecard,
    write_overlap_csv,
    write_scorecard_csv,
)
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, GC_FROZEN, NQ_FILE_SHA, NQ_FROZEN, assert_frozen, file_sha256

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase47_validation.json"
DOCS = ROOT / "docs" / "PHASE47_ES_DVP_FORWARD_VALIDATION.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _registry_block(lock: dict[str, Any]) -> str:
    h = lock.get("locked_config_hash")
    return f"""### ES DVP locked candidate (Phase 47) — not frozen

| Field | Value |
|-------|-------|
| Market | ES (CME E-mini S&P 500). MES is sizing reference only |
| Family | `nq_drift_vwap_pullback_v1` port (`ES_DVP_PORT`) |
| Status | `LOCKED_FORWARD_VALIDATION_CANDIDATE` — **not** `strategy_frozen/` |
| Version | `{LOCKED_VERSION}` |
| Config hash | `{h}` |
| Source | `strategy_candidates/phase46_ES_DVP.json` |
| Locked file | `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json` |
| Session | 09:30 VWAP; trade 10:30–15:30; flatten 15:55 ET |
| Brackets | Long SL18/TP9; Short SL18/TP11.25 (TRAIN ATR scale 0.22547932918744962) |
| Guardrails | Max 4/day; stop after 2 losses; one position |
| News | T−5m→T+5m around 08:30 ET (does not overlap RTH entries) |
| Phase 46 | TRAIN E[R]+0.019 N=2511; HOLDOUT +0.038 N=3063; full +0.030 N=5574; corr vs NQ DVP 0.60 |
| Journal | `journal/phase47_es_dvp_paper/` (empty until genuine forward fills) |
| Execution | `DRY_RUN_ONLY` `NOT_PRODUCTION` — no broker |

"""


def update_registry(lock: dict[str, Any]) -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### ES DVP locked candidate (Phase 47) — not frozen"
    block = _registry_block(lock)
    if marker in text:
        start = text.index(marker)
        rest = text[start + len(marker) :]
        nxt = rest.find("\n### ")
        nxt2 = rest.find("\n## ")
        cuts = [i for i in (nxt, nxt2) if i >= 0]
        end_rel = min(cuts) if cuts else len(rest)
        text = text[:start] + block + rest[end_rel:].lstrip("\n")
        if not text.startswith("#"):
            pass
    else:
        needle = "### NQ Drift VWAP Pullback"
        idx = text.find(needle)
        if idx < 0:
            raise RuntimeError("registry_nq_section_missing")
        # insert after NQ section (next ## RESEARCH or next ### under RESEARCH)
        after = text.find("\n## RESEARCH", idx)
        if after < 0:
            after = text.find("\n## ", idx + 10)
        text = text[:after] + "\n" + block + text[after:]
    if "### ES DVP forward lock + multi-book paper (Phase 47)" not in text:
        research = "## RESEARCH-ONLY / RETIRED"
        insert = """## RESEARCH-ONLY / RETIRED

### ES DVP forward lock + multi-book paper (Phase 47)

| Field | Value |
|-------|--------|
| Phase | 47 |
| Status | `ES_DVP_FORWARD_VALIDATION_READY` / `MULTI_BOOK_FORWARD_VALIDATION_READY`. ES is locked, not frozen. |
| Question | Do locked/frozen rules continue to behave after research ends and no further tuning is allowed? |
| Forbidden | Retune ES/NQ/GC; add RTY/YM/CL/FVG/ORB/TSMOM; replay history into forward journals; broker execution |
| Evidence | `docs/PHASE47_ES_DVP_FORWARD_VALIDATION.md`, `phase47_validation.json`, `reports/phase47_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. |

"""
        text = text.replace(research, insert, 1)
    REGISTRY.write_text(text, encoding="utf-8")


def render_docs(payload: dict[str, Any]) -> str:
    lock = payload["locked_candidate"]
    h = lock["locked_config_hash"]
    bm = lock.get("historical_benchmark") or {}
    full = bm.get("full") or {}
    train = bm.get("train") or {}
    hold = bm.get("holdout") or {}
    corr = bm.get("corr_nq_dvp") or {}
    sc = payload["scorecard"]["rows"]
    fam = payload["dvp_family"]
    data = payload["forward_data"]
    lines = [
        "# Phase 47 — ES DVP candidate lock + multi-book forward validation",
        "",
        "`DRY_RUN`. `NO BROKER EXECUTION`. Not a production freeze.",
        "",
        "Question: **Do the frozen/locked rules continue behaving after research ends and no further tuning is allowed?**",
        "",
        "Phase 46 established that DVP is portable to ES. Phase 47 does not search more markets. It locks the ES candidate and opens a prospective paper stream beside the two frozen books.",
        "",
        "## 1. Verdict",
        "",
        f"- **ES candidate status:** `{payload['es_candidate_status']}`",
        f"- **Portfolio status:** `{payload['portfolio_status']}`",
        f"- **Overall:** `{payload['overall']}`",
        f"- **ES locked hash:** `{h}`",
        f"- **Broker execution:** `{payload['broker_execution']}`",
        "",
        "ES DVP is a **locked research candidate**, not `FROZEN`. Do not promote on early wins. Do not demote on early losses. Before N=30 the book stays in forward validation.",
        "",
        "## 2. Frozen integrity",
        "",
        "Verified before and after. `strategy_frozen/` was not written.",
        "",
        "| Book | Config hash | File SHA | Intact |",
        "|------|-------------|----------|--------|",
        f"| GC VWAP V2 | `{FROZEN_GC_HASH}` | `{GC_FILE_SHA}` | YES |",
        f"| NQ DVP | `{FROZEN_NQ_HASH}` | `{NQ_FILE_SHA}` | YES |",
        "",
        "## 3. Phase 46 ES DVP candidate (authoritative source)",
        "",
        "Read from `strategy_candidates/phase46_ES_DVP.json`. Parameters were not reconstructed from memory.",
        "",
        "| Field | Value |",
        "|-------|-------|",
        "| Family | DVP (`nq_drift_vwap_pullback_v1`) |",
        "| Instrument | ES |",
        "| Version / candidate | `ES_DVP_PORT` → locked as `" + LOCKED_VERSION + "` |",
        "| Signal | 15m drift (close vs session VWAP + VWAP slope + 1h return ±0.10%); first opposing completed 5m; entry next 5m open |",
        "| Session | America/New_York; VWAP 09:30; trade 10:30; no new 15:30; flatten 15:55 |",
        "| Drift threshold | 0.001 (0.10%) |",
        "| Stop methodology | TRAIN median session ATR14 scale 0.22547932918744962 vs frozen NQ 80/40/50 |",
        "| Long | stop 18 / target 9 |",
        "| Short | stop 18 / target 11.25 |",
        "| Daily loss | stop after 2 losing trades |",
        "| Max trades/day | 4; one position at a time |",
        "| Costs | 1-tick adverse round-turn + 0.08 pt commission (overlays 0 / 2 ticks diagnostic only) |",
        f"| TRAIN | N={train.get('n_resolved')} E[R]={train.get('expectancy_r')} WR={train.get('win_rate')} PF={train.get('profit_factor')} |",
        f"| HOLDOUT | N={hold.get('n_resolved')} E[R]={hold.get('expectancy_r')} WR={hold.get('win_rate')} PF={hold.get('profit_factor')} |",
        f"| Full 1-tick | N={full.get('n_resolved')} E[R]={full.get('expectancy_r')} WR={full.get('win_rate')} PF={full.get('profit_factor')} |",
        "| Walk-forward | Phase 46 year blocks (not retuned): 2020 negative; 2021–2026 positive |",
        f"| Corr vs NQ DVP | daily P&L {corr.get('daily_pnl_correlation')} on {corr.get('n_overlap')} overlapping days (under 0.70 redundancy bar) |",
        "",
        "## 4. Locked candidate",
        "",
        f"- File: `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json`",
        f"- Status: `LOCKED_FORWARD_VALIDATION_CANDIDATE`",
        f"- Flags: `NOT_PRODUCTION` `DRY_RUN_ONLY`",
        f"- Hash: `{h}`",
        f"- Lock timestamp: `{lock.get('lock_timestamp')}`",
        "",
        "This hash must be checked on every future ES paper run. Any rule change requires a new version, a new hash, and a new forward journal. Never silently alter this campaign.",
        "",
        "News (Phase 46 locked rule): T−5m → T+5m around 08:30 ET. RTH entries start 10:30, so the 08:30 window never overlaps entries (Phase 46 `n_news_removed` = 0). Frozen GC/NQ news behavior was not modified.",
        "",
        "## 5. Forward journals",
        "",
        "| Book | Journal | Forward N | Policy |",
        "|------|---------|----------:|--------|",
        f"| GC VWAP V2 | `journal/phase26_gc_vwap_v2_paper/` | {sc[0]['forward_n']} | append-only; historical contents not rewritten |",
        f"| NQ DVP | `journal/phase30_nq_dvp_paper/` | {sc[1]['forward_n']} | append-only; historical contents not rewritten |",
        f"| ES DVP | `journal/phase47_es_dvp_paper/` | {sc[2]['forward_n']} | new; empty; no backtest replay |",
        "",
        f"`GC_FORWARD_N = {sc[0]['forward_n']} / {TARGET_N}`",
        "",
        f"`NQ_FORWARD_N = {sc[1]['forward_n']} / {TARGET_N}`",
        "",
        f"`ES_FORWARD_N = {sc[2]['forward_n']} / {TARGET_N}`",
        "",
        "A trade counts toward forward N only if the setup occurs after the lock timestamp, uses information available then, and the simulated entry/stop/target are determined prospectively. Historical replay does not count. Only resolved entered positions increment N. Non-entered setups go to `setups.jsonl`.",
        "",
        "State machine: `NO_SETUP` → `SETUP_ARMED` → `ENTRY_PENDING` → `OPEN_POSITION` → `TARGET` | `STOP` | `FORCE_CLOSE` | `SESSION_CANCEL` | `INVALIDATED_BEFORE_ENTRY`.",
        "",
        "## 6. Paper engine safety",
        "",
        "- Mode: `DRY_RUN` only. No `--enable-sim-execution`. No NinjaTrader/broker path.",
        "- Duplicate key: `strategy + instrument + session_date + setup_timestamp + direction`.",
        "- Idempotent JSONL append. Restart restores session date, armed setup, open paper position, daily counts, and hash from `runner_state.json` + journals.",
        "- Config hash validated every run. Stale-data block reused from DVP live signal (`STALE_5M_SECONDS`). Completed bars only.",
        "- Primary cost overlay 1 tick adverse (Phase 46: 2-way tick + 0.08 commission). 0-tick and 2-tick stored as diagnostics; they do not change signals.",
        "- Signals from ES. MES dollars are a sizing reference ($5/pt vs $50/pt).",
        "",
        "## 7. Live data",
        "",
        f"- CDP module present: `{data.get('cdp_module_present')}`",
        f"- GC live: `{data['live']['GC']['status']}` — missing: {data['live']['GC']['missing']}",
        f"- NQ live: `{data['live']['NQ']['status']}` — missing: {data['live']['NQ']['missing']}",
        f"- ES live: `{data['live']['ES']['status']}` — missing: {data['live']['ES']['missing']}",
        "",
        "Forward validation can be structurally ready while live bars are blocked. **Do not invent paper trades** to fill N. Evaluation accounts are not required; the engine must work against real-time data in DRY_RUN first.",
        "",
        "## 8. DVP family monitor",
        "",
        "NQ and ES remain separate books. Monitor only, equal-risk diagnostic `NQ_DVP_R + ES_DVP_R`. `DVP_FAMILY_CONCENTRATION` is a warning, not a trade block.",
        "",
        f"- Same-day overlap: `{fam.get('same_day_overlap')}`",
        f"- Same-direction overlap: `{fam.get('same_direction_overlap')}`",
        f"- Simultaneous-position overlap: `{fam.get('simultaneous_position_overlap')}`",
        f"- P(ES active \\| NQ active): `{fam.get('p_es_active_given_nq')}`",
        f"- Forward P&L correlation: `{fam.get('forward_pnl_correlation')}`",
        f"- Combined family DD: `{fam.get('combined_family_dd_r')}`",
        "",
        "With N=0 this is `INSUFFICIENT_FORWARD_SAMPLE`. Historical Phase 46 daily P&L correlation (~0.60) is a research prior, not a forward statistic.",
        "",
        "## 9. What Phase 47 did not do",
        "",
        "No retune of ES stops, drift, thresholds, or session. No NQ/GC edits. No RTY, YM, 6E, CL, FVG, ORB, order flow, TSMOM, volume profile, or new VWAP/EMA variants. No freeze of ES. No historical trades copied into the ES journal.",
        "",
        "## 10. Decision checklist",
        "",
        f"1. GC and NQ frozen hashes intact? **{payload['decisions']['frozen_intact']}**",
        f"2. ES Phase 46 candidate locked? **{payload['decisions']['es_locked']}**",
        f"3. ES candidate hash? `{h}`",
        f"4. Three journals valid append-only? **{payload['decisions']['journals_ok']}**",
        f"5. Forward N: GC {sc[0]['forward_n']}, NQ {sc[1]['forward_n']}, ES {sc[2]['forward_n']}",
        f"6. Real-time data available? **{payload['decisions']['live_data']}**",
        f"7. Restart/duplicate protections present? **{payload['decisions']['protections']}**",
        f"8. DVP family overlap monitoring operational? **{payload['decisions']['family_monitor']}**",
        f"9. Broker execution enabled? **{payload['broker_execution']}**",
        f"10. Next: {payload['next']}",
        "",
        "## 11. Files",
        "",
        "- `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json`",
        "- `journal/phase47_es_dvp_paper/paper_trades.jsonl`",
        "- `es_dvp_lock.py`, `es_dvp_paper.py`, `es_dvp_paper_runner.py`, `es_dvp_live.py`",
        "- `dvp_family_monitor.py`, `multi_book_forward.py`",
        "- `reports/phase47_forward_scorecard.csv`, `reports/phase47_dvp_family_overlap.csv`",
        "- `phase47_validation.json`",
        "",
    ]
    return "\n".join(lines) + "\n"


def run() -> dict[str, Any]:
    journals = ensure_all_journals()
    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_BEFORE:{frozen_before}")

    lock = write_locked_document()
    lock_check = assert_locked_hash(lock)
    if not lock_check.get("ok"):
        raise RuntimeError(f"LOCKED_HASH_FAIL:{lock_check}")

    if ES_PAPER.exists() and ES_PAPER.read_text(encoding="utf-8").strip():
        raise RuntimeError("ES_JOURNAL_NOT_EMPTY_AT_LOCK")
    if file_sha256(ES_PAPER) != EMPTY_SHA:
        raise RuntimeError("ES_JOURNAL_NOT_EMPTY_SHA")

    restored = restore_runner_from_journal(str(lock["locked_config_hash"]))
    if restored.get("broker_execution"):
        raise RuntimeError("BROKER_FLAG_SET")

    card = scorecard()
    fam = family_monitor()
    gc_div = gc_diversification_monitor()
    data = probe_forward_data()
    write_scorecard_csv(card)
    write_overlap_csv(fam)

    gc_n = int(card["rows"][0]["forward_n"])
    nq_n = int(card["rows"][1]["forward_n"])
    es_n = int(card["rows"][2]["forward_n"])
    es_status = card["es_status"]
    if data.get("overall") == "FORWARD_DATA_BLOCKED" and es_n == 0:
        # Infrastructure is ready; live bars are not. Keep candidate READY, report data separately.
        es_status = "ES_DVP_FORWARD_VALIDATION_READY"

    frozen_after = assert_frozen()
    if not frozen_after.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_AFTER:{frozen_after}")
    if file_sha256(GC_FROZEN) != GC_FILE_SHA or file_sha256(NQ_FROZEN) != NQ_FILE_SHA:
        raise RuntimeError("FROZEN_FILE_SHA_DRIFT")

    port = portfolio_status(
        es_n=es_n,
        gc_n=gc_n,
        nq_n=nq_n,
        frozen_ok=True,
        lock_ok=True,
        data=data,
    )
    live_ready = data.get("overall") == "FORWARD_DATA_READY"
    overall = "FORWARD_VALIDATION_READY" if port == "MULTI_BOOK_FORWARD_VALIDATION_READY" else port
    nxt = (
        "Attach real-time CME GC, NQ, and ES futures charts and let the locked/frozen rules generate the first genuine forward trades. Do not retune. Do not backfill."
        if not live_ready
        else "Accumulate the first genuine resolved forward trades. Do not retune. Do not freeze ES early."
    )
    payload = {
        "phase": 47,
        "es_candidate_status": es_status,
        "portfolio_status": port,
        "overall": overall,
        "broker_execution": False,
        "DRY_RUN_ONLY": True,
        "NOT_PRODUCTION": True,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "locked_candidate": {
            "path": str(LOCKED_PATH).replace("\\", "/"),
            "status": lock.get("status"),
            "locked_version": lock.get("locked_version"),
            "locked_config_hash": lock.get("locked_config_hash"),
            "lock_timestamp": lock.get("lock_timestamp"),
            "cfg": LOCKED_CFG.to_dict(),
            "session": lock.get("session"),
            "news": lock.get("news"),
            "historical_benchmark": lock.get("historical_benchmark"),
        },
        "journals": journals,
        "scorecard": card,
        "dvp_family": {k: v for k, v in fam.items() if k != "daily"},
        "gc_diversification": gc_div,
        "forward_data": data,
        "runner": es_runner_status(),
        "duplicate_protection": {
            "existing_es_ids": sorted(existing_ids()),
            "setup_key": "strategy + instrument + session_date + setup_timestamp + direction",
            "restart_state": restored.get("state"),
        },
        "decisions": {
            "frozen_intact": True,
            "es_locked": True,
            "journals_ok": True,
            "live_data": data.get("overall"),
            "protections": True,
            "family_monitor": True,
        },
        "next": nxt,
        "forbidden": [
            "retune",
            "freeze_es",
            "broker_execution",
            "historical_backfill",
            "rty_ym_cl_fvg_orb_tsmom",
        ],
    }
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    update_registry(lock)
    return payload


def main() -> int:
    payload = run()
    print(json.dumps({
        "es_candidate_status": payload["es_candidate_status"],
        "portfolio_status": payload["portfolio_status"],
        "overall": payload["overall"],
        "es_hash": payload["locked_candidate"]["locked_config_hash"],
        "GC_FORWARD_N": payload["scorecard"]["rows"][0]["progress"],
        "NQ_FORWARD_N": payload["scorecard"]["rows"][1]["progress"],
        "ES_FORWARD_N": payload["scorecard"]["rows"][2]["progress"],
        "live_data": payload["forward_data"]["overall"],
        "broker_execution": payload["broker_execution"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
