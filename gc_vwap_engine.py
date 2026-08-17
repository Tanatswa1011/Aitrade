"""GC session VWAP + mean-reversion engine (Phase 25)."""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from gc_orb_engine import build_risk, build_targets, detect_roll_gap_timestamps, trading_dates_in_bars
from gc_vwap_models import (
    MAX_ENTRY_BARS,
    MIN_VWAP_BARS,
    NO_NEW_SETUP_AFTER_LOCAL,
    OR_TIMEZONE,
    PHASE25_CANDIDATES,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    SIGMA_THRESHOLD,
    STRATEGY_FAMILY,
    ConfirmationMode,
    EntryMode,
    DEFAULT_NY_SESSION,
    GCVWAPExtensionEvent,
    GCVWAPSetup,
    GCVWAPStrategyConfig,
    SessionVWAPState,
    VwapSessionSpec,
)
from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    EntryStatus,
    FixedRRTarget,
    RiskPlan,
    TargetPlan,
)

NY = ZoneInfo(OR_TIMEZONE)


def config_hash(cfg: GCVWAPStrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            cfg.confirmation_mode,
            cfg.entry_mode,
            str(cfg.sigma_threshold),
            str(cfg.max_entry_bars),
            str(cfg.min_vwap_bars),
            str(cfg.volume_filter),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_ts(trading_date: str, hhmm: str, tz_name: str = OR_TIMEZONE) -> int:
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, hhmm.split(":"))
    tz = ZoneInfo(tz_name)
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).timestamp())


def session_window(
    trading_date: str,
    session: VwapSessionSpec = DEFAULT_NY_SESSION,
) -> tuple[int, int, int]:
    start = _local_ts(trading_date, session.start_local, session.timezone)
    end = _local_ts(trading_date, session.end_local, session.timezone)
    no_new = _local_ts(trading_date, session.no_new_setups_after, session.timezone)
    return start, end, no_new


def trading_dates_for_session(bars: Sequence[Bar], session: VwapSessionSpec = DEFAULT_NY_SESSION) -> list[str]:
    """Calendar dates in the session timezone that appear in the bar set."""
    tz = ZoneInfo(session.timezone)
    dates = sorted(
        {
            datetime.fromtimestamp(int(b.time), tz=tz).date().isoformat()
            for b in bars
        }
    )
    return dates


def typical_price(bar: Bar) -> float:
    return (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0


def compute_session_vwap_series(
    bars: Sequence[Bar],
    trading_date: str,
    session: VwapSessionSpec = DEFAULT_NY_SESSION,
) -> list[SessionVWAPState]:
    """Cumulative VWAP + volume-weighted std through each completed session bar."""
    start, end, _ = session_window(trading_date, session)
    min_bars = int(session.min_vwap_bars)
    session_bars = [b for b in sorted(bars, key=lambda x: int(x.time)) if start <= int(b.time) < end]
    out: list[SessionVWAPState] = []
    sum_pv = 0.0
    sum_v = 0.0
    # For weighted variance around running VWAP we recompute from history each bar
    hist: list[tuple[float, float]] = []  # (tp, vol)
    for i, b in enumerate(session_bars):
        tp = typical_price(b)
        vol = float(b.volume or 0.0)
        if vol < 0:
            vol = 0.0
        sum_pv += tp * vol
        sum_v += vol
        hist.append((tp, vol))
        vwap = None if sum_v <= 0 else sum_pv / sum_v
        std = None
        if vwap is not None and sum_v > 0 and i + 1 >= 2:
            # volume-weighted std of typical prices vs current VWAP
            var_num = 0.0
            for tp_i, v_i in hist:
                if v_i <= 0:
                    continue
                d = tp_i - vwap
                var_num += v_i * d * d
            var = var_num / sum_v
            std = math.sqrt(var) if var > 0 else 0.0
        z = None
        bands = {1: None, 2: None, 3: None}
        if vwap is not None and std is not None and std > 0:
            z = (float(b.close) - vwap) / std
            for k in (1, 2, 3):
                bands[k] = (vwap - k * std, vwap + k * std)
        elif vwap is not None and std == 0.0:
            z = 0.0
            bands = {1: (vwap, vwap), 2: (vwap, vwap), 3: (vwap, vwap)}
        valid = vwap is not None and sum_v > 0 and (i + 1) >= min_bars and std is not None
        out.append(
            SessionVWAPState(
                trading_date=trading_date,
                session_start=start,
                session_end=end,
                timestamp=int(b.time),
                vwap=vwap,
                cumulative_volume=sum_v,
                bars_used=i + 1,
                session_std=std,
                band_1=bands[1],
                band_2=bands[2],
                band_3=bands[3],
                z_vwap=z,
                valid=bool(valid),
                extras={"session_note": session.session_note, "close": float(b.close), "timezone": session.timezone},
            )
        )
    return out


def make_event_id(trading_date: str, side: str, first_ts: int, prefix: str = "VWAP2S") -> str:
    return f"GC|{trading_date}|{prefix}|{side}|{first_ts}"


def _is_upper_ext(state: SessionVWAPState, sigma: float = SIGMA_THRESHOLD) -> bool:
    if not state.valid or state.z_vwap is None:
        return False
    return float(state.z_vwap) >= float(sigma)


def _is_lower_ext(state: SessionVWAPState, sigma: float = SIGMA_THRESHOLD) -> bool:
    if not state.valid or state.z_vwap is None:
        return False
    return float(state.z_vwap) <= -float(sigma)


def collect_extension_sequences(
    bars: Sequence[Bar],
    trading_date: str,
    *,
    roll_flags: Optional[set[int]] = None,
    sigma: float = SIGMA_THRESHOLD,
    session: VwapSessionSpec = DEFAULT_NY_SESSION,
) -> list[dict[str, Any]]:
    """
    Scan one session; emit non-overlapping extension sequences with reclaim markers.
    Each item includes bars/states needed for candidate analysis.
    """
    flags = roll_flags or set()
    start, end, no_new = session_window(trading_date, session)
    states = compute_session_vwap_series(bars, trading_date, session=session)
    if not states:
        return []
    by_ts = {int(s.timestamp): s for s in states}
    session_bars = [b for b in sorted(bars, key=lambda x: int(x.time)) if start <= int(b.time) < end]
    sequences: list[dict[str, Any]] = []
    i = 0
    while i < len(session_bars):
        b = session_bars[i]
        st = by_ts.get(int(b.time))
        if st is None or not st.valid or int(b.time) > no_new:
            i += 1
            continue
        upper = _is_upper_ext(st, sigma)
        lower = _is_lower_ext(st, sigma)
        if not upper and not lower:
            i += 1
            continue
        side = "above" if upper else "below"
        direction = "bearish" if upper else "bullish"  # fade direction
        first_ts = int(b.time)
        extreme = float(b.high) if upper else float(b.low)
        max_abs_z = abs(float(st.z_vwap or 0.0))
        first_z = float(st.z_vwap or 0.0)
        frozen_2sig = None
        if st.band_2:
            frozen_2sig = float(st.band_2[1] if upper else st.band_2[0])
        ext_high = float(b.high)
        ext_low = float(b.low)
        j = i
        reclaim_idx = None
        reclaim_bar = None
        # Stay extended / update extreme until reclaim close inside band
        while j < len(session_bars):
            bj = session_bars[j]
            sj = by_ts.get(int(bj.time))
            if sj is None:
                j += 1
                continue
            if upper:
                extreme = max(extreme, float(bj.high))
                ext_high = max(ext_high, float(bj.high))
                ext_low = min(ext_low, float(bj.low))
                still = _is_upper_ext(sj, sigma)
                # reclaim: close back below +2σ
                reclaimed = (
                    sj.valid
                    and sj.band_2 is not None
                    and float(bj.close) < float(sj.band_2[1])
                    and int(bj.time) > first_ts
                )
            else:
                extreme = min(extreme, float(bj.low))
                ext_high = max(ext_high, float(bj.high))
                ext_low = min(ext_low, float(bj.low))
                still = _is_lower_ext(sj, sigma)
                reclaimed = (
                    sj.valid
                    and sj.band_2 is not None
                    and float(bj.close) > float(sj.band_2[0])
                    and int(bj.time) > first_ts
                )
            if sj.z_vwap is not None:
                max_abs_z = max(max_abs_z, abs(float(sj.z_vwap)))
            if reclaimed:
                reclaim_idx = j
                reclaim_bar = bj
                break
            if not still and int(bj.time) > first_ts:
                # left extension without formal reclaim vs frozen semantics — treat close inside as reclaim
                if sj.band_2 is not None:
                    reclaim_idx = j
                    reclaim_bar = bj
                    break
            j += 1

        sequences.append(
            {
                "trading_date": trading_date,
                "side": side,
                "direction": direction,
                "first_idx": i,
                "first_ts": first_ts,
                "first_bar": b,
                "first_state": st,
                "extreme": extreme,
                "ext_high": ext_high,
                "ext_low": ext_low,
                "max_abs_z": max_abs_z,
                "first_z": first_z,
                "frozen_2sig": frozen_2sig,
                "extension_midpoint": (ext_high + ext_low) / 2.0,
                "reclaim_idx": reclaim_idx,
                "reclaim_bar": reclaim_bar,
                "session_bars": session_bars,
                "by_ts": by_ts,
                "roll_artifact": first_ts in flags,
                "no_new": no_new,
                "session_end": end,
                "session_spec": session.to_dict(),
                "event_prefix": session.event_prefix,
            }
        )
        # advance past this sequence to avoid overlap
        i = (reclaim_idx + 1) if reclaim_idx is not None else (i + 1)
        if reclaim_idx is None:
            # no reclaim before end — still record; candidates may expire
            i = max(i, j if j > i else i + 1)
    return sequences


def time_to_vwap_touch(
    seq: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Structural: first_extension → first bar whose range touches then-current VWAP."""
    session_bars = seq["session_bars"]
    by_ts = seq["by_ts"]
    first_ts = int(seq["first_ts"])
    direction = seq["direction"]
    for b in session_bars:
        if int(b.time) < first_ts:
            continue
        st = by_ts.get(int(b.time))
        if st is None or st.vwap is None:
            continue
        v = float(st.vwap)
        touched = float(b.low) <= v <= float(b.high)
        if touched:
            bars_after = sum(1 for x in session_bars if first_ts < int(x.time) <= int(b.time))
            return {
                "touched": True,
                "touch_timestamp": int(b.time),
                "bars_after": bars_after,
                "minutes_after": bars_after * 5,
                "direction": direction,
                "side": seq["side"],
            }
    return {
        "touched": False,
        "touch_timestamp": None,
        "bars_after": None,
        "minutes_after": None,
        "direction": direction,
        "side": seq["side"],
    }


def _find_confirmation(
    seq: dict[str, Any],
    mode: str,
) -> Optional[dict[str, Any]]:
    session_bars = seq["session_bars"]
    by_ts = seq["by_ts"]
    first_idx = int(seq["first_idx"])
    first_ts = int(seq["first_ts"])
    side = seq["side"]
    upper = side == "above"

    if mode == ConfirmationMode.NONE.value:
        # confirmation = first extension bar itself
        return {
            "confirmation_timestamp": first_ts,
            "confirmation_bar": seq["first_bar"],
            "confirmation_idx": first_idx,
            "mode": mode,
        }

    if mode == ConfirmationMode.BAND_RECLAIM.value:
        if seq["reclaim_bar"] is None:
            return None
        return {
            "confirmation_timestamp": int(seq["reclaim_bar"].time),
            "confirmation_bar": seq["reclaim_bar"],
            "confirmation_idx": seq["reclaim_idx"],
            "mode": mode,
        }

    if mode == ConfirmationMode.RECLAIM_CANDLE_BREAK.value:
        if seq["reclaim_bar"] is None or seq["reclaim_idx"] is None:
            return None
        ri = int(seq["reclaim_idx"])
        rb = seq["reclaim_bar"]
        # next bars: close beyond reclaim candle extreme toward VWAP
        for k in range(ri + 1, len(session_bars)):
            b = session_bars[k]
            if int(b.time) > seq["no_new"] + 3600:  # allow confirm a bit after no-new for entry window
                break
            if upper:
                # short: close below reclaim low
                if float(b.close) < float(rb.low):
                    return {
                        "confirmation_timestamp": int(b.time),
                        "confirmation_bar": b,
                        "confirmation_idx": k,
                        "mode": mode,
                        "reclaim_bar": rb,
                    }
            else:
                if float(b.close) > float(rb.high):
                    return {
                        "confirmation_timestamp": int(b.time),
                        "confirmation_bar": b,
                        "confirmation_idx": k,
                        "mode": mode,
                        "reclaim_bar": rb,
                    }
        return None

    if mode == ConfirmationMode.TWO_BAR_RETURN.value:
        # after first extension, two consecutive closes toward VWAP
        for k in range(first_idx + 2, len(session_bars)):
            b0 = session_bars[k - 2]
            b1 = session_bars[k - 1]
            b2 = session_bars[k]
            if int(b0.time) < first_ts:
                continue
            if upper:
                ok = float(b1.close) < float(b0.close) and float(b2.close) < float(b1.close)
            else:
                ok = float(b1.close) > float(b0.close) and float(b2.close) > float(b1.close)
            if ok:
                return {
                    "confirmation_timestamp": int(b2.time),
                    "confirmation_bar": b2,
                    "confirmation_idx": k,
                    "mode": mode,
                }
        return None

    return None


def _find_entry(
    seq: dict[str, Any],
    conf: dict[str, Any],
    cfg: GCVWAPStrategyConfig,
) -> Optional[dict[str, Any]]:
    session_bars = seq["session_bars"]
    mode = cfg.entry_mode
    conf_idx = int(conf["confirmation_idx"])
    conf_bar = conf["confirmation_bar"]
    direction = seq["direction"]
    max_bars = int(cfg.max_entry_bars)

    if mode == EntryMode.IMMEDIATE_2SIG_CLOSE.value:
        return {
            "entry_price": float(conf_bar.close),
            "entry_timestamp": int(conf_bar.time),
            "entry_bar": conf_bar,
        }

    if mode == EntryMode.CONFIRMATION_CLOSE.value:
        return {
            "entry_price": float(conf_bar.close),
            "entry_timestamp": int(conf_bar.time),
            "entry_bar": conf_bar,
        }

    if mode == EntryMode.EXTENSION_MIDPOINT.value:
        mid = float(seq["extension_midpoint"])
        # after confirmation, wait for touch of midpoint within max_entry_bars
        for k in range(conf_idx, min(len(session_bars), conf_idx + 1 + max_bars)):
            b = session_bars[k]
            if k == conf_idx:
                # allow same bar only if range includes mid after confirmation close semantics:
                # require later bar for midpoint retrace to avoid look-ahead on confirmation bar
                continue
            hit = float(b.low) <= mid <= float(b.high)
            if hit:
                return {"entry_price": mid, "entry_timestamp": int(b.time), "entry_bar": b}
        return None

    if mode == EntryMode.FROZEN_2SIG_RETEST.value:
        band = seq.get("frozen_2sig")
        if band is None:
            return None
        band = float(band)
        for k in range(conf_idx + 1, min(len(session_bars), conf_idx + 1 + max_bars)):
            b = session_bars[k]
            hit = float(b.low) <= band <= float(b.high)
            if hit:
                return {"entry_price": band, "entry_timestamp": int(b.time), "entry_bar": b}
        return None

    return None


def analyze_candidate(seq: dict[str, Any], cfg: GCVWAPStrategyConfig) -> GCVWAPSetup:
    prefix = str(seq.get("event_prefix") or "VWAP2S")
    eid = make_event_id(seq["trading_date"], seq["side"], int(seq["first_ts"]), prefix=prefix)
    setup_id = f"{eid}|cand:{cfg.candidate_id}"

    def _empty(reason: str, state: str = "EXPIRED", **extra) -> GCVWAPSetup:
        return GCVWAPSetup(
            strategy_family=cfg.strategy_family,
            setup_id=setup_id,
            vwap_extension_event_id=eid,
            candidate_id=cfg.candidate_id,
            trading_date=seq["trading_date"],
            direction=seq["direction"],
            entry_mode=cfg.entry_mode,
            confirmation_mode=cfg.confirmation_mode,
            entry_price=None,
            entry_timestamp=None,
            entry_triggered=False,
            stop_price=None,
            risk_distance=None,
            risk_valid=False,
            risk_invalidation_reason=None,
            event=_event_dict(seq, eid, None),
            state=state,
            reason=reason,
            extras=extra,
        )

    if seq.get("roll_artifact"):
        return _empty("ROLL_ARTIFACT", state="INVALIDATED")
    if int(seq["first_ts"]) > int(seq["no_new"]):
        return _empty("past_no_new_setup_cutoff")

    conf = _find_confirmation(seq, cfg.confirmation_mode)
    if conf is None:
        return _empty("no_confirmation")

    # Confirmation itself should not start after hard session end
    if int(conf["confirmation_timestamp"]) >= int(seq["session_end"]):
        return _empty("confirmation_after_session_end")

    entry = _find_entry(seq, conf, cfg)
    if entry is None:
        return _empty("entry_timeout")

    direction = seq["direction"]
    entry_price = float(entry["entry_price"])
    entry_ts = int(entry["entry_timestamp"])
    stop = float(seq["extreme"])
    if entry_ts >= int(seq["session_end"]):
        return _empty("entry_after_session_end")

    risk = build_risk(direction=direction, entry_price=entry_price, stop_price=stop)
    # or_size proxy for descriptive targets: abs deviation at entry vs unused
    or_size = abs(entry_price - stop)
    targets, _ = build_targets(entry_price, stop, direction, or_size) if risk.valid else ([], {})

    # Distance to then-current VWAP at entry
    st = seq["by_ts"].get(entry_ts)
    vwap_dist = None if st is None or st.vwap is None else abs(entry_price - float(st.vwap))
    vwap_r = None if vwap_dist is None or not risk.risk_distance else vwap_dist / float(risk.risk_distance)

    return GCVWAPSetup(
        strategy_family=cfg.strategy_family,
        setup_id=setup_id,
        vwap_extension_event_id=eid,
        candidate_id=cfg.candidate_id,
        trading_date=seq["trading_date"],
        direction=direction,
        entry_mode=cfg.entry_mode,
        confirmation_mode=cfg.confirmation_mode,
        entry_price=entry_price,
        entry_timestamp=entry_ts,
        entry_triggered=True,
        stop_price=risk.stop_price,
        risk_distance=risk.risk_distance,
        risk_valid=risk.valid,
        risk_invalidation_reason=risk.invalidation_reason,
        targets=targets,
        event=_event_dict(seq, eid, conf),
        state="ENTRY_READY" if risk.valid else "INVALIDATED",
        reason=None if risk.valid else risk.invalidation_reason,
        extras={
            "vwap_distance_at_entry": vwap_dist,
            "vwap_distance_r": vwap_r,
            "max_abs_z": seq["max_abs_z"],
            "frozen_2sig": seq.get("frozen_2sig"),
            "confirmation_timestamp": conf["confirmation_timestamp"],
        },
    )


def _event_dict(seq: dict[str, Any], eid: str, conf: Optional[dict]) -> dict[str, Any]:
    return GCVWAPExtensionEvent(
        event_id=eid,
        trading_date=seq["trading_date"],
        direction=seq["direction"],
        extension_side=seq["side"],
        first_extension_timestamp=int(seq["first_ts"]),
        confirmation_timestamp=None if not conf else int(conf["confirmation_timestamp"]),
        extension_extreme=float(seq["extreme"]),
        frozen_2sig_band=seq.get("frozen_2sig"),
        extension_midpoint=seq.get("extension_midpoint"),
        max_abs_z=float(seq["max_abs_z"]),
        first_extension_z=float(seq["first_z"]),
        roll_artifact=bool(seq.get("roll_artifact")),
        extras={},
    ).to_dict()


def setup_to_entry_analysis(setup: GCVWAPSetup) -> EntryAnalysis:
    entry = EntryCandidate(
        mode=setup.entry_mode,
        direction=setup.direction,
        price=setup.entry_price,
        triggered=setup.entry_triggered,
        trigger_timestamp=setup.entry_timestamp,
        trigger_bar_index=None,
        fvg_reference={},
        setup_reference={
            "setup_id": setup.setup_id,
            "vwap_extension_event_id": setup.vwap_extension_event_id,
        },
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value if setup.entry_triggered else EntryStatus.WAITING.value,
        extras={},
    )
    risk = RiskPlan(
        direction=setup.direction,
        stop_mode="extension_extreme",
        entry_price=float(setup.entry_price or 0.0),
        stop_price=setup.stop_price,
        risk_distance=setup.risk_distance,
        risk_points=setup.risk_distance,
        buffer=0.0,
        valid=setup.risk_valid,
        invalidation_reason=setup.risk_invalidation_reason,
        setup_reference={},
        extras={},
    )
    fixed = [
        FixedRRTarget(rr=float(t["rr"]), price=float(t["price"]), distance=float(t["distance"]))
        for t in (setup.targets or [])
    ]
    target = TargetPlan(
        fixed_rr_targets=fixed,
        opposite_liquidity=False,
        opposite_liquidity_label=None,
        opposite_liquidity_price=None,
        rr_to_opposite=None,
        opposite_target_valid=False,
        valid=bool(fixed),
        setup_reference={},
        extras={},
    )
    return EntryAnalysis(entry=entry, risk=risk, target=target)


def evaluate_vwap_touch_after_entry(
    *,
    bars: Sequence[Bar],
    trading_date: str,
    entry_ts: int,
    direction: str,
    stop_price: float,
    session_end: int,
    session: VwapSessionSpec = DEFAULT_NY_SESSION,
) -> dict[str, Any]:
    """Chronological VWAP touch using only VWAP known at each bar; stop cuts off."""
    states = {int(s.timestamp): s for s in compute_session_vwap_series(bars, trading_date, session=session)}
    ordered = [b for b in sorted(bars, key=lambda x: int(x.time)) if int(b.time) >= int(entry_ts)]
    for b in ordered:
        t = int(b.time)
        if t > int(session_end):
            break
        # stop first same-bar ambiguity → not counted as VWAP hit
        hit_stop = (
            float(b.low) <= float(stop_price)
            if direction == "bullish"
            else float(b.high) >= float(stop_price)
        )
        st = states.get(t)
        vwap = None if st is None else st.vwap
        hit_vwap = vwap is not None and float(b.low) <= float(vwap) <= float(b.high)
        if hit_stop and hit_vwap:
            return {"vwap_hit": False, "ambiguous": True, "timestamp": t}
        if hit_stop:
            return {"vwap_hit": False, "ambiguous": False, "timestamp": t, "stopped": True}
        if hit_vwap:
            return {
                "vwap_hit": True,
                "ambiguous": False,
                "timestamp": t,
                "vwap": float(vwap),
            }
    return {"vwap_hit": False, "ambiguous": False, "expired": True}


def collect_all_sequences(
    bars: Sequence[Bar],
    session: VwapSessionSpec = DEFAULT_NY_SESSION,
) -> list[dict[str, Any]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    roll = detect_roll_gap_timestamps(ordered)
    out: list[dict[str, Any]] = []
    for td in trading_dates_for_session(ordered, session):
        out.extend(collect_extension_sequences(ordered, td, roll_flags=roll, session=session))
    return out
