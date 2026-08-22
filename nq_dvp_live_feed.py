"""Phase 55B.0 — live NinjaTrader NQ bars into frozen DVP. Strategy logic is not modified.

Warmup bars are tagged HISTORICAL_WARMUP and cannot become executable live signals.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_databento import DATA_ROOT, aggregate_1m_to_ny
from nq_drift_vwap_engine import session_anchors
from nq_drift_vwap_models import (
    FORCE_CLOSE_LOCAL,
    NO_NEW_TRADES_AFTER_LOCAL,
    OR_TIMEZONE,
    TRADE_START_LOCAL,
    VWAP_RESET_LOCAL,
)
from nq_dvp_freeze import frozen_config_hash, load_frozen_document, load_frozen_strategy_config, semantic_payload
from nq_dvp_live_signal import STALE_5M_SECONDS, evaluate_completed_bars
from execution_status import NQ_FROZEN_HASH

NY = ZoneInfo(OR_TIMEZONE)
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "phase55_live_dvp.json"
SIGNAL_INSTRUMENT = "NQ 09-26"
EXEC_INSTRUMENT = "MNQ 09-26"
STRATEGY_ID = "NQ_DRIFT_VWAP_PULLBACK"
MARKET_SOURCE = "NINJATRADER_TRADOVATE"
MIN_15M_FOR_DRIFT = 5
MIN_1M_SESSION = 30


def _as_bar(row: Any) -> Optional[Bar]:
    if isinstance(row, Bar):
        return row
    if not isinstance(row, dict):
        return None
    try:
        return Bar(
            time=int(row["time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=None if row.get("volume") is None else float(row["volume"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def bar_identity(ts: int, timeframe: str, *, instrument: str = "NQ") -> str:
    dt = datetime.fromtimestamp(int(ts), tz=NY)
    return f"{instrument}:{dt.isoformat()}:{timeframe}"


def ny_now(now_ts: Optional[int] = None) -> datetime:
    if now_ts is None:
        return datetime.now(tz=NY)
    return datetime.fromtimestamp(int(now_ts), tz=NY)


def trading_date_ny(now_ts: Optional[int] = None) -> str:
    return ny_now(now_ts).date().isoformat()


def load_nt_1m_bars(dump: Optional[dict[str, Any]]) -> list[Bar]:
    if not isinstance(dump, dict):
        return []
    rows = dump.get("nq_bars_1m") or []
    out: list[Bar] = []
    seen: set[int] = set()
    for row in rows:
        if isinstance(row, dict) and row.get("finalized") is False:
            continue
        bar = _as_bar(row)
        if bar is None or int(bar.time) in seen:
            continue
        seen.add(int(bar.time))
        out.append(bar)
    out.sort(key=lambda b: int(b.time))
    return out


def load_historical_warmup_1m(trading_date: str) -> list[Bar]:
    """Session 1m bars from the stitched Databento archive. Never executable."""
    try:
        from bar_dataset import load_dataset
    except Exception:
        return []
    root = DATA_ROOT / "stitched"
    try:
        doc = load_dataset("databento_NQ_stitched", "1m", root=root)
    except Exception:
        return []
    bars = [_as_bar(b) for b in (doc.get("bars") or [])]
    bars = [b for b in bars if b is not None]
    anchors = session_anchors(trading_date)
    start = anchors["vwap_reset"]
    end = anchors["force_close"] + 3600
    return [b for b in bars if start <= int(b.time) < end]


def merge_warmup_and_live(
    warmup: Sequence[Bar],
    live: Sequence[Bar],
) -> dict[str, Any]:
    live_sorted = sorted(live, key=lambda b: int(b.time))
    first_live = int(live_sorted[0].time) if live_sorted else None
    last_live = int(live_sorted[-1].time) if live_sorted else None
    warm_kept = []
    if first_live is not None:
        warm_kept = [b for b in warmup if int(b.time) < first_live]
    else:
        warm_kept = list(warmup)
    last_hist = int(warm_kept[-1].time) if warm_kept else None
    merged = list(warm_kept) + live_sorted
    live_times = {int(b.time) for b in live_sorted}
    return {
        "bars_1m": merged,
        "warmup_1m": warm_kept,
        "live_1m": live_sorted,
        "last_historical_bar_ts": last_hist,
        "first_live_bar_ts": first_live,
        "last_live_bar_ts": last_live,
        "live_times": live_times,
        "warmup_count": len(warm_kept),
        "live_count": len(live_sorted),
    }


def _enough_context(bars_1m: Sequence[Bar], bars_15m: Sequence[Bar], trading_date: str) -> bool:
    anchors = session_anchors(trading_date)
    sess_1m = [b for b in bars_1m if int(b.time) >= anchors["vwap_reset"]]
    sess_15 = [b for b in bars_15m if int(b.time) >= anchors["vwap_reset"]]
    return len(sess_1m) >= MIN_1M_SESSION and len(sess_15) >= MIN_15M_FOR_DRIFT


def strategy_status(
    *,
    now_ts: int,
    trading_date: str,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    live_count: int,
    snapshot_state: str,
) -> str:
    anchors = session_anchors(trading_date)
    if now_ts >= anchors["no_new"]:
        return "SESSION_CLOSED"
    if live_count <= 0:
        return "WARMING_UP"
    if not _enough_context(bars_1m, bars_15m, trading_date):
        return "WARMING_UP"
    if now_ts < anchors["trade_start"]:
        return "WARMING_UP"
    if snapshot_state in ("ENTRY_PENDING_NEXT_BAR", "WAITING_FOR_PULLBACK", "POSITIVE_DRIFT", "NEGATIVE_DRIFT"):
        return "LIVE"
    if bars_5m:
        last5 = int(bars_5m[-1].time)
        if now_ts - (last5 + 300) <= STALE_5M_SECONDS:
            return "READY"
    return "READY"


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "last_evaluated_trigger": None,
            "last_historical_bar_ts": None,
            "first_live_bar_ts": None,
            "seen_triggers": [],
            "last_live_signal_id": None,
        }
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "last_evaluated_trigger": None,
            "seen_triggers": [],
        }


def _save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")


def evaluate_live_dvp(
    *,
    dump: Optional[dict[str, Any]] = None,
    now_ts: Optional[int] = None,
    warmup_bars: Optional[Sequence[Bar]] = None,
    persist: bool = True,
    consume: bool = False,
    seen_triggers: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Evaluate frozen DVP on live NT 1m bars. Does not place orders."""
    if dump is None:
        try:
            from nt_readonly import NTReadOnly

            dump = NTReadOnly().runtime_snapshot()
        except Exception:
            dump = None
    now_dt = ny_now(now_ts)
    now = int(now_dt.timestamp()) if now_ts is None else int(now_ts)
    trading_date = trading_date_ny(now)
    doc = load_frozen_document()
    cfg = load_frozen_strategy_config(doc)
    live_hash = frozen_config_hash(semantic_payload(cfg))
    hash_ok = live_hash == NQ_FROZEN_HASH == doc.get("frozen_config_hash")

    live_1m = load_nt_1m_bars(dump)
    warmup = list(warmup_bars) if warmup_bars is not None else load_historical_warmup_1m(trading_date)
    merged = merge_warmup_and_live(warmup, live_1m)
    bars_1m = merged["bars_1m"]
    bars_5m = aggregate_1m_to_ny(bars_1m, 5) if bars_1m else []
    bars_15m = aggregate_1m_to_ny(bars_1m, 15) if bars_1m else []
    last_5 = bars_5m[-1] if bars_5m else None
    last_15 = bars_15m[-1] if bars_15m else None

    st = _load_state()
    seen = set(seen_triggers if seen_triggers is not None else (st.get("seen_triggers") or []))
    snap = evaluate_completed_bars(
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        trading_date=trading_date,
        cfg=cfg,
        seen_triggers=seen,
        now_ts=now,
    )
    intended = snap.intended_order
    pending = snap.pending_entry
    live_times: set[int] = merged["live_times"]
    first_live = merged["first_live_bar_ts"]
    trigger_ts = None
    if intended:
        trigger_ts = int(intended.get("trigger_ts") or 0)
    elif pending:
        trigger_ts = int(pending.get("trigger_ts") or 0)

    in_live_session_bar = trigger_ts is not None and trigger_ts in live_times
    if trigger_ts is not None and first_live is not None and trigger_ts < first_live:
        in_live_session_bar = False

    anchors = session_anchors(trading_date)
    session_open = anchors["trade_start"] <= now < anchors["no_new"]
    freshness = snap.data_freshness_sec
    stale = freshness is not None and freshness > STALE_5M_SECONDS
    trigger_key = None
    if intended:
        trigger_key = intended.get("trigger_key")
    elif pending:
        trigger_key = pending.get("trigger_key")

    duplicate = bool(trigger_key and trigger_key in seen)
    executable = bool(
        intended
        and in_live_session_bar
        and session_open
        and not stale
        and not duplicate
        and hash_ok
        and snap.blocked_reason is None
    )

    status = strategy_status(
        now_ts=now,
        trading_date=trading_date,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        live_count=merged["live_count"],
        snapshot_state=snap.state,
    )
    pipeline = "LIVE_DVP_PIPELINE_READY"
    if status == "SESSION_CLOSED":
        pipeline = "LIVE_DVP_PIPELINE_READY_SESSION_CLOSED"
        executable = False
    elif status == "WARMING_UP":
        pipeline = "LIVE_DVP_WARMING_UP"
        executable = False
    elif merged["live_count"] <= 0:
        pipeline = "LIVE_NQ_BARS_UNAVAILABLE"
        executable = False

    live_signal = None
    if intended and in_live_session_bar and not duplicate:
        bar_id = bar_identity(int(intended["trigger_ts"]), "5m")
        signal_id = str(intended.get("trade_id") or bar_id)
        live_signal = {
            "source": "phase54_live",
            "strategy": STRATEGY_ID,
            "strategy_hash": live_hash,
            "signal_id": signal_id,
            "signal_timestamp": now_dt.isoformat(),
            "bar_timestamp": datetime.fromtimestamp(int(intended["trigger_ts"]), tz=NY).isoformat(),
            "bar_identity": bar_id,
            "direction": intended.get("direction"),
            "signal_instrument": SIGNAL_INSTRUMENT,
            "execution_instrument": EXEC_INSTRUMENT,
            "market_source": MARKET_SOURCE,
            "live_bar": True,
            "executable": executable,
            "trigger_key": trigger_key,
            "intended_entry": intended.get("signal_entry_price"),
            "trading_date": trading_date,
            "ts": now_dt.isoformat(),
            "entry_timestamp": intended.get("entry_timestamp"),
            "theoretical_stop": intended.get("theoretical_stop"),
            "theoretical_target": intended.get("theoretical_target"),
        }
        if persist:
            st["last_evaluated_trigger"] = trigger_key
            st["last_live_signal_id"] = signal_id
            if consume and executable:
                seen.add(str(trigger_key))
                st["seen_triggers"] = sorted(seen)[-64:]
    elif intended and not in_live_session_bar:
        live_signal = {
            "source": "HISTORICAL_WARMUP",
            "live_bar": False,
            "executable": False,
            "direction": intended.get("direction"),
            "trigger_key": trigger_key,
            "trading_date": trading_date,
            "note": "warmup_or_replay_not_executable",
        }

    if persist:
        st["last_historical_bar_ts"] = merged["last_historical_bar_ts"]
        st["first_live_bar_ts"] = merged["first_live_bar_ts"]
        st["last_live_bar_ts"] = merged["last_live_bar_ts"]
        st["strategy_status"] = status
        st["pipeline"] = pipeline
        _save_state(st)

    return {
        "ok": True,
        "pipeline": pipeline,
        "strategy_status": status,
        "dvp_state": snap.state,
        "trading_date": trading_date,
        "timezone": OR_TIMEZONE,
        "session_window": f"{TRADE_START_LOCAL}–{NO_NEW_TRADES_AFTER_LOCAL} ET",
        "vwap_reset": VWAP_RESET_LOCAL,
        "force_close": FORCE_CLOSE_LOCAL,
        "frozen_hash": live_hash,
        "frozen_hash_ok": hash_ok,
        "live_nq_stream": "ACTIVE" if merged["live_count"] > 0 else "INACTIVE",
        "bar_source": "NINJATRADER_BARSREQUEST_1M",
        "warmup_source": "databento_NQ_stitched" if warmup else None,
        "warmup_count": merged["warmup_count"],
        "live_1m_count": merged["live_count"],
        "last_historical_bar_ts": merged["last_historical_bar_ts"],
        "first_live_bar_ts": merged["first_live_bar_ts"],
        "last_live_bar_ts": merged["last_live_bar_ts"],
        "last_finalized_5m": None
        if last_5 is None
        else {
            "time": int(last_5.time),
            "iso_et": datetime.fromtimestamp(int(last_5.time), tz=NY).isoformat(),
            "identity": bar_identity(int(last_5.time), "5m"),
            "open": last_5.open,
            "high": last_5.high,
            "low": last_5.low,
            "close": last_5.close,
            "volume": last_5.volume,
        },
        "last_finalized_15m": None
        if last_15 is None
        else {
            "time": int(last_15.time),
            "iso_et": datetime.fromtimestamp(int(last_15.time), tz=NY).isoformat(),
            "identity": bar_identity(int(last_15.time), "15m"),
            "open": last_15.open,
            "high": last_15.high,
            "low": last_15.low,
            "close": last_15.close,
            "volume": last_15.volume,
        },
        "snapshot": snap.to_dict(),
        "live_signal": live_signal,
        "executable": executable,
        "duplicate": duplicate,
        "stale": stale,
        "PROP_EXECUTION": False,
        "required_timeframes": ["1m", "5m", "15m"],
        "signal_instrument": SIGNAL_INSTRUMENT,
        "execution_instrument": EXEC_INSTRUMENT,
        "market_source": MARKET_SOURCE,
    }
