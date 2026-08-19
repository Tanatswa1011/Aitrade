"""Phase 43 — small-cap gap-up short data gate.

DRY_RUN. No broker. No freeze. No universe download. Primary
SMALLCAP_GAP50_OR5_BREAKDOWN was declared in phase43_spec.json but is not
tested because the feasibility gate fails.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase43_validation.json"
SPEC_PATH = ROOT / "phase43_spec.json"
FEAS = REPORTS / "phase43_data_feasibility.json"
DOCS = ROOT / "docs" / "PHASE43_SMALLCAP_GAP_UP_SHORT_RESEARCH.md"

VERDICT = "SMALLCAP_DATA_QUALITY_BLOCKED"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def _cost(feas: dict[str, Any], key: str) -> Any:
    row = (feas.get("costs") or {}).get(key) or {}
    if row.get("ok"):
        return row.get("value")
    return row.get("error")


def capability_rows() -> list[dict[str, Any]]:
    return [
        {"requirement": "US equity session 09:30-16:00 ET", "in_repo": "PATTERN_ONLY", "note": "Futures RTH helpers exist; no equity calendar/halts."},
        {"requirement": "Historical stock universe incl. delisted", "in_repo": "NO", "note": "Zero local equity files."},
        {"requirement": "Survivorship-safe security master", "in_repo": "NO", "note": "Tiingo/OpenBB BBBY daily is empty."},
        {"requirement": "Ticker changes / acquisitions", "in_repo": "NO", "note": ""},
        {"requirement": "Corporate actions / reverse splits PIT", "in_repo": "NO", "note": "Would fake +500% gaps if ignored."},
        {"requirement": "Intraday 1m US equity OHLCV", "in_repo": "NO", "note": "Only ES/NQ/GC futures 1m on disk."},
        {"requirement": "Premarket tape", "in_repo": "NO", "note": ""},
        {"requirement": "PIT market cap", "in_repo": "NO", "note": "Do not use today's cap."},
        {"requirement": "PIT float / shares outstanding", "in_repo": "NO", "note": "FLOAT_DATA_UNAVAILABLE"},
        {"requirement": "Halt / LULD timestamps", "in_repo": "NO", "note": "HALT_MODEL_DEGRADED. EQUS.MINI has no status schema."},
        {"requirement": "Historical SSR flag", "in_repo": "NO", "note": "Can be inferred from prior close vs prior-prior close, not from locates."},
        {"requirement": "Borrow / locate / HTB fee history", "in_repo": "NO", "note": "No IBKR/iBorrowDesk/Polygon locate feed."},
        {"requirement": "Equity commissions / locate minimums", "in_repo": "NO", "note": "Repo costs are ES/NQ/GC ticks."},
        {"requirement": "Equity broker / scanner / locate workflow", "in_repo": "NO", "note": "NT Sim101 is MNQ. Cannot operationalize."},
        {"requirement": "News/catalyst PIT", "in_repo": "NO", "note": "Diagnostic only; not required to fail the gate."},
    ]


def source_rows(feas: dict[str, Any]) -> list[dict[str, Any]]:
    ds = feas.get("datasets") or {}
    def _start(name):
        rng = ((ds.get(name) or {}).get("range") or {}).get("value") or {}
        if isinstance(rng, dict):
            return rng.get("start")
        return None
    return [
        {"source": "Local AITRADE disk", "coverage": "none", "intraday": "no", "survivorship": "n/a", "float": "no", "halts": "no", "borrow": "no", "cost_now": 0, "note": "No equity files."},
        {"source": "OpenBB+Tiingo equity daily", "coverage": "live names (AAPL ok)", "intraday": "not used", "survivorship": "FAIL (BBBY empty)", "float": "no", "halts": "no", "borrow": "no", "cost_now": "existing TIINGO_TOKEN", "note": "Cannot be the research universe."},
        {"source": "Databento EQUS.MINI ohlcv-1m ALL", "coverage": _start("EQUS.MINI"), "intraday": "yes from 2023-03-28", "survivorship": "unknown/incomplete vs CRSP", "float": "no", "halts": "NO status schema", "borrow": "no", "cost_now": _cost(feas, "equs_mini_1m_all_2023_2026"), "note": "Train through 2022 would be empty. Not purchased."},
        {"source": "Databento EQUS.MINI ohlcv-1d ALL", "coverage": _start("EQUS.MINI"), "intraday": "daily only", "float": "no", "halts": "no", "borrow": "no", "survivorship": "unknown", "cost_now": _cost(feas, "equs_mini_1d_all_2023_2026"), "note": "~$12. Still no OR5, float, borrow."},
        {"source": "Databento IEXG.TOPS", "coverage": _start("IEXG.TOPS"), "intraday": "IEX-only from 2023-03-28", "survivorship": "IEX-listed prints only", "float": "no", "halts": "status schema exists", "borrow": "no", "cost_now": _cost(feas, "iex_1m_all_1day"), "note": "Small-cap volume on IEX is not NMS volume. History too short."},
        {"source": "Databento XNAS.ITCH ohlcv-1m ALL", "coverage": _start("XNAS.ITCH"), "intraday": "Nasdaq 2018-05+", "survivorship": "Nasdaq tape; not NYSE American", "float": "no", "halts": "status schema", "borrow": "no", "cost_now": _cost(feas, "xnas_itch_1m_2018_2026"), "note": "~$1502. Not purchased. Still missing PIT cap/float/borrow and AMEX names."},
        {"source": "Databento DBEQ.BASIC ohlcv-1m ALL", "coverage": _start("DBEQ.BASIC"), "intraday": "from 2023-03-28", "survivorship": "unknown", "float": "no", "halts": "status schema", "borrow": "no", "cost_now": _cost(feas, "dbeq_1m_2023_2026"), "note": "~$1907. Not purchased. History too short for TRAIN<=2022."},
        {"source": "Polygon", "coverage": "not credentialed", "intraday": "possible if subscribed", "survivorship": "spotty delisted", "float": "not native PIT float", "halts": "paid market events", "borrow": "no", "cost_now": "no POLYGON_API_KEY", "note": "Do not subscribe in this phase."},
        {"source": "Alpaca", "coverage": "not credentialed", "intraday": "SIP/IEX by plan", "survivorship": "generally live-tradable names", "float": "no", "halts": "limited", "borrow": "locates live, not 2018 history", "cost_now": "no ALPACA_API_KEY", "note": ""},
        {"source": "FMP", "coverage": "FMP_API_KEY absent", "intraday": "limited", "survivorship": "mixed", "float": "current-leaning", "halts": "no", "borrow": "no", "cost_now": "absent", "note": ""},
        {"source": "CRSP / WRDS", "coverage": "gold-standard daily PIT", "intraday": "no 1m", "survivorship": "yes", "float": "shares outstanding, not true float", "halts": "no", "borrow": "no", "cost_now": "not licensed", "note": "Would still need a 1m tape for OR5."},
        {"source": "Nasdaq Data Link / Sharadar", "coverage": "not credentialed", "intraday": "no", "survivorship": "Sharadar SF1 better than Yahoo", "float": "shares outstanding", "halts": "no", "borrow": "no", "cost_now": "no NASDAQ_DATA_LINK_API_KEY", "note": ""},
    ]


def write_markdown(payload: dict[str, Any]) -> None:
    feas = payload.get("feasibility") or {}
    lines = [
        "# Phase 43 — Small-cap gap-up short (data feasibility)",
        "",
        "Research only. `DRY_RUN`. No broker. Nothing frozen. No equity data was purchased.",
        "",
        "Primary locked before P&L: `SMALLCAP_GAP50_OR5_BREAKDOWN` (US common, gap ≥ +50%, 09:30–09:35 OR, 1m close below OR low, short next 1m open, stop = OR high, 1R, cover 15:50). **Not tested.** The data gate failed first.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **FLOAT_DATA_UNAVAILABLE:** `{payload.get('FLOAT_DATA_UNAVAILABLE')}`",
        f"- **HALT_MODEL_DEGRADED:** `{payload.get('HALT_MODEL_DEGRADED')}`",
        f"- **BORROW_HISTORY_UNAVAILABLE:** `{payload.get('BORROW_HISTORY_UNAVAILABLE')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        "",
        "This is not `SMALLCAP_GAP_SHORT_EDGE_REJECTED`. The market effect was not measured. A later phase can test Gap-Up Short, First Red Day, or Bounce Short only after a survivorship-safe 1m universe plus halt and borrow assumptions exist.",
        "",
        "## 2. Frozen futures integrity",
        "",
        "Verified before and after. Frozen files were not modified. `strategy_frozen/` was not written.",
        "",
        f"- GC VWAP V2: `{FROZEN_GC_HASH}`",
        f"- NQ DVP: `{FROZEN_NQ_HASH}`",
        f"- File SHA GC: `{payload.get('file_sha', {}).get('gc')}`",
        f"- File SHA NQ: `{payload.get('file_sha', {}).get('nq')}`",
        "",
        "## 3. Data feasibility",
        "",
        "AITRADE on disk is futures (ES/NQ/GC) plus XAUUSD. There are **zero** local US equity bar files, security masters, delist files, or corporate-action tables.",
        "",
        "Tiingo via OpenBB `equity.price.historical` returns AAPL daily bars and returns an empty response for bankrupt BBBY. That is a survivorship gap on the only equity route already credentialed. A backtest on today's listed names would be invalid.",
        "",
        "Declared TRAIN end is 2022-12-30. Databento EQUS.MINI / IEXG.TOPS / DBEQ.BASIC history starts **2023-03-28**. Even a paid Mini 1m tape would leave TRAIN empty.",
        "",
        "## 4. Data sources",
        "",
        "Metadata/`get_cost` only. No timeseries download. Credentials present: Databento yes, Tiingo yes, FMP/Polygon/Alpaca/Nasdaq Data Link no.",
        "",
        "| Source | History start | 1m ALL cost (quoted, not bought) | Halt schema | Notes |",
        "|---|---|---:|---|---|",
        f"| EQUS.MINI | 2023-03-28 | ${_cost(feas, 'equs_mini_1m_all_2023_2026')} (2023–2026) | no | Cheapest NMS-like 1m; too short; no float/borrow |",
        f"| EQUS.MINI daily | 2023-03-28 | ${_cost(feas, 'equs_mini_1d_all_2023_2026')} | no | Cannot test OR5 |",
        f"| IEXG.TOPS 1m 1-day | 2023-03-28 | ${_cost(feas, 'iex_1m_all_1day')} | yes | IEX volume ≠ small-cap NMS volume |",
        f"| XNAS.ITCH 1m | 2018-05-01 | ${_cost(feas, 'xnas_itch_1m_2018_2026')} | yes | Nasdaq only; ~$1502; still no cap/float/borrow |",
        f"| DBEQ.BASIC 1m | 2023-03-28 | ${_cost(feas, 'dbeq_1m_2023_2026')} | yes | ~$1907; TRAIN empty |",
        "",
        "Polygon/Alpaca/FMP/CRSP/Sharadar are not in this repo and were not subscribed.",
        "",
        "## 5. Historical universe",
        "",
        "None constructed. Included: n/a. Excluded: n/a. Building a Yahoo/Tiingo surviving-name list was refused.",
        "",
        "## 6. Market cap / float quality",
        "",
        "`FLOAT_DATA_UNAVAILABLE`. Point-in-time market cap is also unavailable. Today's float must not be applied historically. A degraded cap-from-price×shares model was not invented.",
        "",
        "## 7. Gap distributions",
        "",
        "Not computed. No valid universe.",
        "",
        "## 8. Structural behavior",
        "",
        "Not computed. Open-to-close, HOD timing, P(close < open), halt probability: n/a.",
        "",
        "## 9. OR5 breakdown",
        "",
        "Not computed. Candidate A requires 1m bars on a PIT small-cap universe.",
        "",
        "## 10. VWAP loss",
        "",
        "Not computed.",
        "",
        "## 11. Primary candidate",
        "",
        "`SMALLCAP_GAP50_OR5_BREAKDOWN` remains the locked definition for a future data phase. Phase 43 entered **zero** trades. Status: not tested.",
        "",
        "## 12. Target matrix",
        "",
        "n/a",
        "",
        "## 13. Gap-size analysis",
        "",
        "n/a",
        "",
        "## 14. Market-cap / float analysis",
        "",
        "n/a (`FLOAT_DATA_UNAVAILABLE`)",
        "",
        "## 15. Price analysis",
        "",
        "n/a",
        "",
        "## 16. Volume / float rotation",
        "",
        "n/a",
        "",
        "## 17. Halts",
        "",
        "`HALT_MODEL_DEGRADED`. EQUS.MINI does not support `status`. XNAS.ITCH and DBEQ.BASIC do, but were not purchased. No halt timestamps are on disk. A continuous-tradability assumption would be invalid for this strategy family.",
        "",
        "## 18. SSR",
        "",
        "Not reconstructed. Rule 201 can be inferred from a prior-day ≥10% decline only after a valid daily universe exists. Even then, SSR changes *how* a short is routed, not whether shares exist.",
        "",
        "## 19. Borrow / locate",
        "",
        "Unavailable. Scenarios 1–3 were not run because there are no theoretical trades. Label if a later phase tests prices anyway: `UNCONSTRAINED_THEORETICAL`. A profitable tape without locates is not Book 3.",
        "",
        "## 20. Slippage stress",
        "",
        "Predeclared overlays 0 / 0.25% / 0.50% / 1.00% adverse. Not applied. Small-cap fills are not 1-tick futures fills.",
        "",
        "## 21. Train / holdout",
        "",
        "Predeclared TRAIN through 2022-12-30, HOLDOUT from 2023-01-03. EQUS.MINI cannot populate TRAIN. No split was run.",
        "",
        "## 22. Walk-forward",
        "",
        "n/a",
        "",
        "## 23. Year-by-year",
        "",
        "n/a. Explicitly not a 2020–2021-only study, because no years were studied.",
        "",
        "## 24. MFE / MAE",
        "",
        "n/a",
        "",
        "## 25. Operational feasibility",
        "",
        "AITRADE cannot execute this sleeve today. There is no equity scanner, locate workflow, stock broker adapter, or halt-aware equity router. NinjaTrader Sim101 is MNQ. Even a valid backtest would still fail Phase AM until those pieces exist.",
        "",
        "## 26. Portfolio relationship",
        "",
        "No equity P&L series. No comparison to GC VWAP V2 or NQ DVP. Read-only frozen books were not modified.",
        "",
        "## 27. Recommendation",
        "",
        payload.get("recommendation_text") or "",
        "",
        "Execution remained `DRY_RUN`. No candidate JSON.",
        "",
        "If a later phase acquires data, minimum acceptable bundle:",
        "",
        "1. Survivorship-safe US common-stock master (delisted, ticker changes, corporate actions) covering 2018–present.",
        "2. Point-in-time market cap (float if possible; otherwise labelled degraded).",
        "3. Regular-hours 1m OHLCV including dead names, reverse-split consistent.",
        "4. Halt/LULD timestamps (or conservative halt stress).",
        "5. Borrow/locate model that is not 'every name shortable at 0 fee'.",
        "6. Then, and only then, test the locked `SMALLCAP_GAP50_OR5_BREAKDOWN` without holdout tuning.",
        "",
    ]
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text("\n".join(lines), encoding="utf-8")


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    if spec.get("primary_candidate", {}).get("id") != "SMALLCAP_GAP50_OR5_BREAKDOWN":
        raise SystemExit("primary candidate changed after lock")
    if spec.get("methodology_corrections"):
        raise SystemExit("spec corrections not allowed in this phase")
    feas = json.loads(FEAS.read_text(encoding="utf-8")) if FEAS.exists() else {}
    cap = capability_rows()
    src = source_rows(feas)
    costs = [{"quote": k, **(v if isinstance(v, dict) else {"value": v})} for k, v in (feas.get("costs") or {}).items()]
    _write_csv(REPORTS / "phase43_capability_matrix.csv", cap)
    _write_csv(REPORTS / "phase43_data_sources.csv", src)
    _write_csv(REPORTS / "phase43_cost_quotes.csv", costs)
    rec = "DO_NOT_PURCHASE_BLINDLY_DO_NOT_FAKE_UNIVERSE"
    rec_text = (
        "Do not freeze. Do not test First Red Day or Bounce Short on the same missing universe. "
        "Do not download EQUS.MINI as a shortcut: TRAIN through 2022 would be empty, float and borrow are still missing, and Mini has no halt status. "
        "XNAS.ITCH 1m ALL 2018–2026 quotes at about $1502 and is still Nasdaq-only without PIT cap/float/borrow. "
        "Acquire a survivorship-safe master first, then 1m + halts + a conservative locate model, then run the locked OR5 candidate."
    )
    frozen_after = assert_frozen()
    payload = {
        "ok": True,
        "phase": 43,
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": VERDICT,
        "FLOAT_DATA_UNAVAILABLE": True,
        "HALT_MODEL_DEGRADED": True,
        "BORROW_HISTORY_UNAVAILABLE": True,
        "SURVIVORSHIP_SAFE_UNIVERSE": False,
        "primary_tested": False,
        "n_entered": 0,
        "candidate_written": False,
        "recommendation": rec,
        "recommendation_text": rec_text,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
        },
        "feasibility": {
            "credential_names_present": feas.get("credential_names_present"),
            "costs": feas.get("costs"),
            "tiingo_equity_probe": feas.get("tiingo_equity_probe"),
            "dataset_starts": {
                name: ((block.get("range") or {}).get("value") or {}).get("start")
                for name, block in (feas.get("datasets") or {}).items()
            },
            "equs_mini_schemas": ((feas.get("datasets") or {}).get("EQUS.MINI") or {}).get("schemas"),
        },
        "gate_reasons": [
            "No local US equity history.",
            "No survivorship-safe security master; Tiingo BBBY empty.",
            "FLOAT_DATA_UNAVAILABLE and no PIT market cap.",
            "No halt tape; EQUS.MINI lacks status schema.",
            "No borrow/locate history.",
            "EQUS.MINI/IEX/DBEQ start 2023-03-28; declared TRAIN through 2022 is empty.",
            "Purchasing Mini 1m (~$541) or ITCH 1m (~$1502) still would not complete float/borrow/PIT cap.",
            "AITRADE has no equity execution path.",
        ],
    }
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_markdown(payload)
    print(VERDICT, rec, flush=True)
    return payload


if __name__ == "__main__":
    main()
