"""Multi-book forward scorecard, data-readiness, and historical benchmarks.

Does not invent paper trades. Does not retune locked/frozen books.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from dvp_family_monitor import INSUFFICIENT, family_monitor, gc_diversification_monitor
from es_dvp_lock import LOCKED_PATH, load_locked_document, locked_config_hash
from es_dvp_paper import JOURNAL_DIR as ES_JOURNAL
from es_dvp_paper import PAPER_TRADES_PATH as ES_PAPER
from es_dvp_paper import campaign_status as es_campaign
from es_dvp_paper import summarize_paper_journal as es_summary
from gc_vwap_paper import JOURNAL_DIR as GC_JOURNAL
from gc_vwap_paper import PAPER_TRADES_PATH as GC_PAPER
from gc_vwap_paper import paper_campaign_status as gc_campaign
from gc_vwap_paper import summarize_paper_journal as gc_summary
from nq_dvp_paper import JOURNAL_DIR as NQ_JOURNAL
from nq_dvp_paper import PAPER_TRADES_PATH as NQ_PAPER
from nq_dvp_paper import paper_campaign_status as nq_campaign
from nq_dvp_paper import summarize_paper_journal as nq_summary
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, GC_FROZEN, NQ_FILE_SHA, NQ_FROZEN, assert_frozen, file_sha256

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
SCORECARD_CSV = REPORTS / "phase47_forward_scorecard.csv"
OVERLAP_CSV = REPORTS / "phase47_dvp_family_overlap.csv"

TARGET_N = 30
PREFERRED_N = 50
STRONG_N = 100

# Historical references — monitoring only, never used to rewrite journals.
GC_HIST = {
    "source": "strategy_frozen/gc_vwap_v2_phase26.json holdout",
    "expected_wr": None,
    "expectancy": 0.34482758620689646,
    "expectancy_unit": "E[2R]",
    "pf": None,
    "average_stop": None,
    "trade_frequency": "session opportunistic",
    "long_short_mix": "mean-reversion both sides (V2 reclaim)",
    "historical_max_dd": None,
    "losing_streak": None,
}
NQ_HIST = {
    "source": "strategy_frozen/nq_dvp_phase30.json full sample",
    "expected_wr": 0.6664333216660833,
    "expectancy": 0.07439621981099055,
    "expectancy_unit": "E[R]",
    "pf": 1.2685863440176592,
    "average_stop": 80.0,
    "trade_frequency": "~800 / year research",
    "long_short_mix": "both sides",
    "historical_max_dd": None,
    "losing_streak": None,
}


def ensure_empty_journal(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")


def ensure_all_journals() -> dict[str, Any]:
    ensure_empty_journal(GC_PAPER)
    ensure_empty_journal(NQ_PAPER)
    ensure_empty_journal(ES_PAPER)
    (ES_JOURNAL / "setups.jsonl").parent.mkdir(parents=True, exist_ok=True)
    for extra in (ES_JOURNAL / "setups.jsonl", ES_JOURNAL / "daily_state.jsonl"):
        if not extra.exists():
            extra.write_text("", encoding="utf-8")
    for extra in (GC_JOURNAL / "daily_state.jsonl", NQ_JOURNAL / "daily_state.jsonl"):
        extra.parent.mkdir(parents=True, exist_ok=True)
        if not extra.exists():
            extra.write_text("", encoding="utf-8")
    return {
        "gc": str(GC_PAPER).replace("\\", "/"),
        "nq": str(NQ_PAPER).replace("\\", "/"),
        "es": str(ES_PAPER).replace("\\", "/"),
        "append_only": True,
        "historical_backfill": False,
    }


def _fmt(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, str):
        return x
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def es_hist_from_lock() -> dict[str, Any]:
    doc = load_locked_document()
    bm = doc.get("historical_benchmark") or {}
    full = bm.get("full") or {}
    return {
        "source": "strategy_candidates/phase46_ES_DVP.json (locked into Phase 47)",
        "expected_wr": full.get("win_rate"),
        "expectancy": full.get("expectancy_r"),
        "expectancy_unit": "E[R]",
        "pf": full.get("profit_factor"),
        "average_stop": full.get("avg_stop_points"),
        "trade_frequency": full.get("trades_per_year"),
        "long_short_mix": {"long": (full.get("long") or {}).get("n"), "short": (full.get("short") or {}).get("n")},
        "historical_max_dd": full.get("max_dd_points"),
        "losing_streak": full.get("max_consec_losses"),
        "train_er": (bm.get("train") or {}).get("expectancy_r"),
        "holdout_er": (bm.get("holdout") or {}).get("expectancy_r"),
        "corr_nq_dvp": ((bm.get("corr_nq_dvp") or {}).get("daily_pnl_correlation")),
    }


def compare_forward(name: str, fwd: dict[str, Any], hist_er: Any, hist_wr: Any) -> dict[str, Any]:
    n = int(fwd.get("resolved") or 0)
    if n < 30:
        return {
            "strategy": name,
            "forward_n": n,
            "note": "Do not call deviations statistically meaningful with tiny N.",
            "forward_wr": fwd.get("win_rate"),
            "historical_wr": hist_wr,
            "forward_e": fwd.get("expectancy_r") if fwd.get("expectancy_r") is not None else fwd.get("expectancy_points"),
            "historical_e": hist_er,
            "stop_frequency": fwd.get("stop_frequency"),
            "target_frequency": fwd.get("target_frequency"),
            "avg_hold_sec": fwd.get("avg_hold_sec"),
            "uncertainty": "VERY_HIGH" if n < 10 else "HIGH",
        }
    return {
        "strategy": name,
        "forward_n": n,
        "forward_wr": fwd.get("win_rate"),
        "historical_wr": hist_wr,
        "forward_e": fwd.get("expectancy_r") if fwd.get("expectancy_r") is not None else fwd.get("expectancy_points"),
        "historical_e": hist_er,
        "stop_frequency": fwd.get("stop_frequency"),
        "target_frequency": fwd.get("target_frequency"),
        "avg_hold_sec": fwd.get("avg_hold_sec"),
        "uncertainty": "MODERATE" if n < 100 else "IMPROVING",
    }


def scorecard() -> dict[str, Any]:
    gc = gc_summary()
    nq = nq_summary()
    es = es_summary()
    gc.pop("rows", None)
    nq.pop("rows", None)
    es.pop("rows", None)
    frozen = assert_frozen()
    lock = load_locked_document()
    lock_ok = lock.get("locked_config_hash") == locked_config_hash()
    gc_n = int(gc.get("resolved") or 0)
    nq_n = int(nq.get("resolved") or 0)
    es_n = int(es.get("resolved") or 0)
    es_h = es_hist_from_lock()
    rows = [
        {
            "strategy": "GC VWAP V2",
            "status": "FROZEN_PAPER_VALIDATION",
            "forward_n": gc_n,
            "target": TARGET_N,
            "progress": f"GC_FORWARD_N = {gc_n} / {TARGET_N}",
            "forward_e": gc.get("expectancy_points"),
            "historical_e": GC_HIST["expectancy"],
            "historical_e_unit": GC_HIST["expectancy_unit"],
            "hash_ok": frozen.get("ok") and file_sha256(GC_FROZEN) == GC_FILE_SHA,
            "config_hash": FROZEN_GC_HASH,
        },
        {
            "strategy": "NQ DVP",
            "status": "FROZEN_PAPER_VALIDATION",
            "forward_n": nq_n,
            "target": TARGET_N,
            "progress": f"NQ_FORWARD_N = {nq_n} / {TARGET_N}",
            "forward_e": nq.get("expectancy_points") if nq.get("expectancy_r") is None else nq.get("expectancy_r"),
            "historical_e": NQ_HIST["expectancy"],
            "historical_e_unit": NQ_HIST["expectancy_unit"],
            "hash_ok": frozen.get("ok") and file_sha256(NQ_FROZEN) == NQ_FILE_SHA,
            "config_hash": FROZEN_NQ_HASH,
        },
        {
            "strategy": "ES DVP",
            "status": "LOCKED_FORWARD_VALIDATION_CANDIDATE",
            "forward_n": es_n,
            "target": TARGET_N,
            "progress": f"ES_FORWARD_N = {es_n} / {TARGET_N}",
            "forward_e": es.get("expectancy_r"),
            "historical_e": es_h["expectancy"],
            "historical_e_unit": "Phase46 E[R]",
            "hash_ok": lock_ok,
            "config_hash": lock.get("locked_config_hash"),
        },
    ]
    return {
        "rows": rows,
        "checkpoints": {"minimum": TARGET_N, "preferred": PREFERRED_N, "stronger": STRONG_N},
        "gc_status": gc_campaign(gc_n),
        "nq_status": nq_campaign(nq_n),
        "es_status": es_campaign(es_n),
        "comparisons": [
            compare_forward("GC VWAP V2", gc, GC_HIST["expectancy"], GC_HIST["expected_wr"]),
            compare_forward("NQ DVP", nq, NQ_HIST["expectancy"], NQ_HIST["expected_wr"]),
            compare_forward("ES DVP", es, es_h["expectancy"], es_h["expected_wr"]),
        ],
        "early_promotion": False,
        "note": "Before N=30 per book, status remains FORWARD_VALIDATION_IN_PROGRESS. Do not freeze ES. Do not demote on a handful of losses.",
    }


def write_scorecard_csv(card: dict[str, Any] | None = None) -> Path:
    card = card or scorecard()
    REPORTS.mkdir(parents=True, exist_ok=True)
    fields = ["strategy", "status", "forward_n", "target", "forward_e", "historical_e", "hash_ok", "progress", "config_hash"]
    with SCORECARD_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in card["rows"]:
            w.writerow(r)
    return SCORECARD_CSV


def write_overlap_csv(fam: dict[str, Any] | None = None) -> Path:
    fam = fam or family_monitor()
    REPORTS.mkdir(parents=True, exist_ok=True)
    keys = [
        "metric",
        "value",
    ]
    metrics = [
        ("nq_forward_trades", fam.get("nq_forward_trades")),
        ("es_forward_trades", fam.get("es_forward_trades")),
        ("same_day_overlap", fam.get("same_day_overlap")),
        ("same_direction_overlap", fam.get("same_direction_overlap")),
        ("simultaneous_position_overlap", fam.get("simultaneous_position_overlap")),
        ("p_es_active_given_nq", fam.get("p_es_active_given_nq")),
        ("p_nq_active_given_es", fam.get("p_nq_active_given_es")),
        ("forward_pnl_correlation", fam.get("forward_pnl_correlation")),
        ("combined_family_dd_r", fam.get("combined_family_dd_r")),
        ("worst_same_day_loss_r", fam.get("worst_same_day_loss_r")),
        ("status", fam.get("status")),
    ]
    with OVERLAP_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for k, v in metrics:
            w.writerow({"metric": k, "value": v if v is not None else INSUFFICIENT})
    return OVERLAP_CSV


def probe_forward_data() -> dict[str, Any]:
    """CLI probe: historical stitch is not live. Live requires CDP CME chart."""
    hist = {
        "GC": (ROOT / "data" / "databento" / "GC" / "stitched").exists(),
        "NQ": (ROOT / "data" / "databento" / "NQ" / "stitched").exists(),
        "ES": (ROOT / "data" / "databento" / "ES" / "stitched").exists(),
    }
    live: dict[str, Any] = {}
    for name in ("GC", "NQ", "ES"):
        live[name] = {
            "status": "FORWARD_DATA_BLOCKED",
            "missing": (
                f"real-time CME {name} futures OHLCV via TradingView/CDP "
                "(get_chart_info + fetch_bars on the matching futures chart)"
            ),
            "historical_databento_present": hist[name],
            "note": (
                "Databento stitch is research history. Replaying it does not count as forward N. "
                "Do not invent paper trades while the live chart feed is missing."
            ),
        }
    cdp_present = False
    try:
        import cdp  # noqa: F401

        cdp_present = True
    except Exception:
        cdp_present = False
    return {
        "cdp_module_present": cdp_present,
        "live": live,
        "overall": "FORWARD_DATA_BLOCKED",
        "reason": "CLI/validator has no attached real-time CME chart. Forward N stays 0 until a live GC/NQ/ES futures chart feed is connected. Evaluation accounts are not required.",
    }


def portfolio_status(*, es_n: int, gc_n: int, nq_n: int, frozen_ok: bool, lock_ok: bool, data: dict[str, Any]) -> str:
    if not frozen_ok or not lock_ok:
        return "MULTI_BOOK_FORWARD_VALIDATION_BLOCKED"
    if es_n > 0 or gc_n > 0 or nq_n > 0:
        return "MULTI_BOOK_FORWARD_VALIDATION_IN_PROGRESS"
    # Infrastructure ready even if live data is blocked — blocked is reported separately.
    if not LOCKED_PATH.exists():
        return "MULTI_BOOK_FORWARD_VALIDATION_BLOCKED"
    return "MULTI_BOOK_FORWARD_VALIDATION_READY"
