"""Phase 46 — portability of frozen VWAP-MR and DVP mechanisms (research-only).

Does not modify strategy_frozen/. Defaults of nq_drift_vwap_engine remain NQ.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from gc_orb_engine import detect_roll_gap_timestamps
from gc_vwap_engine import analyze_candidate, collect_extension_sequences, time_to_vwap_touch
from gc_vwap_models import GCVWAPStrategyConfig, VwapSessionSpec
from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import replay_all_days
from nq_drift_vwap_models import DVPStrategyConfig
from nq_pdh_pdl import ny_date
from orb_index_engine import US_RTH_HOLIDAYS

NY = ZoneInfo("America/New_York")
TRAIN_END = "2022-12-30"
HOLDOUT_START = "2023-01-03"

V2_CFG = GCVWAPStrategyConfig(
    strategy_family="gc_vwap_mean_reversion_v1",
    candidate_id="V2_BAND_RECLAIM_2SIG_RETEST",
    confirmation_mode="BAND_RECLAIM",
    entry_mode="FROZEN_2SIG_RETEST",
    sigma_threshold=2.0,
    max_entry_bars=6,
    min_vwap_bars=6,
)

CLOCKS = {
    "ES_MR": {"start": "09:30", "end": "15:55", "no_new": "14:55"},
    "CL_MR": {"start": "09:00", "end": "14:30", "no_new": "13:30"},
    "NQ_MR": {"start": "09:30", "end": "15:55", "no_new": "14:55"},
    "GC_MR": {"start": "08:20", "end": "13:30", "no_new": "12:30"},
    "ES_DVP": {"vwap_reset": "09:30", "trade_start": "10:30", "no_new": "15:30", "force_close": "15:55"},
    "NQ_DVP": {"vwap_reset": "09:30", "trade_start": "10:30", "no_new": "15:30", "force_close": "15:55"},
    "CL_DVP": {"vwap_reset": "09:00", "trade_start": "10:00", "no_new": "13:30", "force_close": "14:25"},
    "GC_DVP": {"vwap_reset": "08:20", "trade_start": "09:20", "no_new": "12:30", "force_close": "13:25"},
}

INSTRUMENTS = {
    "ES": {"tick": 0.25, "point_usd": 50.0, "commission_points": 0.08, "micro_usd": 5.0, "roll_jump": 8.0},
    "CL": {"tick": 0.01, "point_usd": 1000.0, "commission_points": 0.02, "micro_usd": 100.0, "roll_jump": 0.50},
    "NQ": {"tick": 0.25, "point_usd": 20.0, "commission_points": 0.20, "micro_usd": 2.0, "roll_jump": 20.0},
    "GC": {"tick": 0.10, "point_usd": 100.0, "commission_points": 0.04, "micro_usd": 10.0, "roll_jump": 8.0},
}


def _local(td: str, hhmm: str) -> int:
    d = date.fromisoformat(td)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=NY).strftime("%H:%M")


def round_tick(px: float, tick: float) -> float:
    return round(round(px / tick) * tick, 10)


def session_spec(instrument: str, family: str) -> VwapSessionSpec:
    key = f"{instrument}_{family}"
    c = CLOCKS[key]
    return VwapSessionSpec(
        timezone="America/New_York",
        start_local=c["start"],
        end_local=c["end"],
        no_new_setups_after=c["no_new"],
        min_vwap_bars=6,
        event_prefix=f"{instrument}_VWAP2S",
        session_note=f"Phase 46 {instrument} VWAP-MR session locked before P&L",
    )


def valid_session_dates(bars: Sequence[Bar], start_hhmm: str, end_hhmm: str, min_bars: int = 20) -> list[str]:
    by: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        by[ny_date(int(b.time))].append(b)
    out = []
    for td, rows in sorted(by.items()):
        if td in US_RTH_HOLIDAYS:
            continue
        if date.fromisoformat(td).weekday() >= 5:
            continue
        t0, t1 = _local(td, start_hhmm), _local(td, end_hhmm)
        n = sum(1 for b in rows if t0 <= int(b.time) < t1)
        if n >= min_bars:
            out.append(td)
    return out


def session_range(bars_1m: Sequence[Bar], td: str, start: str, end: str) -> Optional[float]:
    t0, t1 = _local(td, start), _local(td, end)
    rows = [b for b in bars_1m if t0 <= int(b.time) < t1]
    if len(rows) < 10:
        return None
    return max(float(b.high) for b in rows) - min(float(b.low) for b in rows)


def atr14_series(ranges: list[tuple[str, float]]) -> dict[str, float]:
    """Wilder ATR14 of daily session ranges; value on day D uses ranges strictly before D."""
    out: dict[str, float] = {}
    prev: Optional[float] = None
    window: list[float] = []
    for td, rng in ranges:
        if prev is not None:
            out[td] = prev
        window.append(rng)
        if len(window) < 14:
            prev = sum(window) / len(window)
        elif len(window) == 14:
            prev = sum(window) / 14.0
        else:
            prev = (prev * 13.0 + rng) / 14.0
    return out


def median_train_atr(bars_1m: Sequence[Bar], start: str, end: str) -> Optional[float]:
    dates = valid_session_dates(bars_1m, start, end)
    by: dict[str, list[Bar]] = defaultdict(list)
    for b in bars_1m:
        by[ny_date(int(b.time))].append(b)
    pairs = []
    for td in dates:
        rng = session_range(by[td], td, start, end)
        if rng and rng > 0:
            pairs.append((td, rng))
    atr = atr14_series(pairs)
    train = [atr[td] for td in atr if td <= TRAIN_END]
    return None if not train else float(statistics.median(train))


def dvp_scaled_cfg(scale: float, tick: float, *, neighbor: float = 1.0) -> DVPStrategyConfig:
    s = float(scale) * float(neighbor)
    return DVPStrategyConfig(
        strategy_family="nq_drift_vwap_pullback_v1",
        candidate_id="DVP_PORT",
        hour_return_threshold=0.001,
        long_stop_points=max(tick, round_tick(80.0 * s, tick)),
        long_target_points=max(tick, round_tick(40.0 * s, tick)),
        short_stop_points=max(tick, round_tick(80.0 * s, tick)),
        short_target_points=max(tick, round_tick(50.0 * s, tick)),
        extras={"scale": scale, "neighbor": neighbor},
    )


@dataclass
class PortTrade:
    instrument: str
    family: str
    candidate: str
    trading_date: str
    direction: str
    entry_ts: int
    entry: float
    stop: float
    target: float
    exit_ts: Optional[int]
    exit: Optional[float]
    outcome: str
    points: Optional[float]
    r_multiple: Optional[float]
    mfe: Optional[float] = None
    mae: Optional[float] = None
    hold_sec: Optional[int] = None
    news_blackout: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def cost_points(tick: float, commission: float, adverse: float) -> float:
    return 2.0 * float(adverse) * float(tick) + float(commission)


def apply_cost(points: Optional[float], tick: float, commission: float, adverse: float) -> Optional[float]:
    if points is None:
        return None
    return float(points) - cost_points(tick, commission, adverse)


def in_news_blackout(ts: int, instrument: str) -> bool:
    hh = _hhmm(ts)
    dt = datetime.fromtimestamp(int(ts), tz=NY)
    # EIA approximation: Wednesday 10:25-10:35 ET, CL only
    if instrument == "CL" and dt.weekday() == 2 and "10:25" <= hh <= "10:35":
        return True
    if "08:25" <= hh <= "08:35":
        return True
    return False


def resolve_path(
    bars_1m: Sequence[Bar],
    *,
    entry_ts: int,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    flatten_ts: int,
) -> dict[str, Any]:
    mfe = mae = 0.0
    for b in bars_1m:
        t = int(b.time)
        if t < entry_ts:
            continue
        if t > flatten_ts + 60:
            break
        if direction == "bullish":
            mfe = max(mfe, float(b.high) - entry)
            mae = max(mae, entry - float(b.low))
            hit_sl = float(b.low) <= stop
            hit_tp = float(b.high) >= target
        else:
            mfe = max(mfe, entry - float(b.low))
            mae = max(mae, float(b.high) - entry)
            hit_sl = float(b.high) >= stop
            hit_tp = float(b.low) <= target
        at_flat = flatten_ts <= t < flatten_ts + 60
        if hit_sl and hit_tp:
            return {"outcome": "AMBIGUOUS", "exit_ts": t, "exit": None, "mfe": mfe, "mae": mae}
        if hit_sl:
            return {"outcome": "STOP_HIT", "exit_ts": t, "exit": stop, "mfe": mfe, "mae": mae}
        if hit_tp:
            return {"outcome": "TARGET_HIT", "exit_ts": t, "exit": target, "mfe": mfe, "mae": mae}
        if at_flat:
            return {"outcome": "TIME_EXIT", "exit_ts": t, "exit": float(b.open), "mfe": mfe, "mae": mae}
    return {"outcome": "TIME_EXIT", "exit_ts": flatten_ts, "exit": None, "mfe": mfe, "mae": mae}


def signed_points(direction: str, entry: float, exit_px: Optional[float]) -> Optional[float]:
    if exit_px is None:
        return None
    return (exit_px - entry) if direction == "bullish" else (entry - exit_px)


def mr_session_for(instrument: str) -> VwapSessionSpec:
    fam = "MR"
    return session_spec(instrument, fam)


def structural_mr(instrument: str, bars_5m: Sequence[Bar], bars_1m: Sequence[Bar], sigma: float = 2.0) -> dict[str, Any]:
    spec = mr_session_for(instrument)
    inst = INSTRUMENTS[instrument]
    flags = detect_roll_gap_timestamps(bars_5m, min_price_jump=float(inst["roll_jump"]))
    dates = valid_session_dates(bars_5m, spec.start_local, spec.end_local)
    by1: dict[str, list[Bar]] = defaultdict(list)
    by5: dict[str, list[Bar]] = defaultdict(list)
    for b in bars_1m:
        by1[ny_date(int(b.time))].append(b)
    for b in bars_5m:
        by5[ny_date(int(b.time))].append(b)
    n_ext = n_reclaim = n_retest = n_vwap = n_continue = 0
    times = []
    mfe_vwap = []
    mae_after = []
    for td in dates:
        seqs = collect_extension_sequences(by5.get(td, []), td, roll_flags=flags, sigma=sigma, session=spec)
        for seq in seqs:
            n_ext += 1
            if seq.get("reclaim_bar") is not None:
                n_reclaim += 1
            setup = analyze_candidate(seq, V2_CFG)
            if setup.entry_triggered:
                n_retest += 1
            touch = time_to_vwap_touch(seq)
            if touch and touch.get("touched"):
                n_vwap += 1
                if touch.get("minutes_after") is not None:
                    times.append(float(touch["minutes_after"]))
            st0 = seq["first_state"]
            v0 = None if st0 is None else st0.vwap
            extreme = float(seq["extreme"])
            first_ts = int(seq["first_ts"])
            end = int(seq["session_end"])
            path = [b for b in by1.get(td, []) if first_ts <= int(b.time) < end]
            if v0 is not None and path:
                if seq["side"] == "above":
                    mfe_vwap.append(max(0.0, extreme - min(float(b.low) for b in path)))
                    # continuation = new high after reclaim rather than VWAP
                    if seq.get("reclaim_bar") is not None:
                        after = [b for b in path if int(b.time) > int(seq["reclaim_bar"].time)]
                        if after and max(float(b.high) for b in after) > extreme:
                            n_continue += 1
                        if after:
                            mae_after.append(max(0.0, max(float(b.high) for b in after) - float(seq["reclaim_bar"].close)))
                else:
                    mfe_vwap.append(max(0.0, max(float(b.high) for b in path) - extreme))
                    if seq.get("reclaim_bar") is not None:
                        after = [b for b in path if int(b.time) > int(seq["reclaim_bar"].time)]
                        if after and min(float(b.low) for b in after) < extreme:
                            n_continue += 1
                        if after:
                            mae_after.append(max(0.0, float(seq["reclaim_bar"].close) - min(float(b.low) for b in after)))
    def _r(a, b):
        return None if not b else a / b
    return {
        "n_sessions": len(dates),
        "n_extensions": n_ext,
        "P_reclaim": _r(n_reclaim, n_ext),
        "P_retest_entry": _r(n_retest, n_ext),
        "P_vwap_touch": _r(n_vwap, n_ext),
        "P_continue_after_reclaim": _r(n_continue, n_reclaim) if n_reclaim else None,
        "median_min_to_vwap": None if not times else statistics.median(times),
        "mean_mfe_toward_vwap": None if not mfe_vwap else statistics.mean(mfe_vwap),
        "mean_mae_after_reclaim": None if not mae_after else statistics.mean(mae_after),
        "extensions_per_year": None if not dates else n_ext / max((int(dates[-1][:4]) - int(dates[0][:4]) + 1), 1),
    }


def simulate_mr_trades(
    instrument: str,
    bars_5m: Sequence[Bar],
    bars_1m: Sequence[Bar],
    *,
    sigma: float = 2.0,
    target_r: float = 2.0,
    adverse: float = 1.0,
    candidate: str = "ES_VWAP_MR_V2_PORT",
) -> list[PortTrade]:
    spec = mr_session_for(instrument)
    cfg = GCVWAPStrategyConfig(
        strategy_family="gc_vwap_mean_reversion_v1",
        candidate_id="V2_BAND_RECLAIM_2SIG_RETEST",
        confirmation_mode="BAND_RECLAIM",
        entry_mode="FROZEN_2SIG_RETEST",
        sigma_threshold=float(sigma),
        max_entry_bars=6,
        min_vwap_bars=6,
    )
    inst = INSTRUMENTS[instrument]
    tick = float(inst["tick"])
    comm = float(inst["commission_points"])
    flags = detect_roll_gap_timestamps(bars_5m, min_price_jump=float(inst["roll_jump"]))
    dates = valid_session_dates(bars_5m, spec.start_local, spec.end_local)
    by1: dict[str, list[Bar]] = defaultdict(list)
    by5: dict[str, list[Bar]] = defaultdict(list)
    for b in bars_1m:
        by1[ny_date(int(b.time))].append(b)
    for b in bars_5m:
        by5[ny_date(int(b.time))].append(b)
    trades: list[PortTrade] = []
    for td in dates:
        seqs = collect_extension_sequences(by5.get(td, []), td, roll_flags=flags, sigma=sigma, session=spec)
        taken = False
        for seq in seqs:
            if taken:
                break
            setup = analyze_candidate(seq, cfg)
            if not setup.entry_triggered or not setup.risk_valid or setup.entry_price is None:
                continue
            if setup.entry_timestamp is None or setup.stop_price is None or not setup.risk_distance:
                continue
            if in_news_blackout(int(setup.entry_timestamp), instrument):
                trades.append(PortTrade(
                    instrument=instrument, family="VWAP_MR", candidate=candidate, trading_date=td,
                    direction=setup.direction, entry_ts=int(setup.entry_timestamp), entry=float(setup.entry_price),
                    stop=float(setup.stop_price), target=float(setup.entry_price),
                    exit_ts=None, exit=None, outcome="NEWS_BLACKOUT", points=None, r_multiple=None, news_blackout=True,
                ))
                continue
            direction = setup.direction
            theo = float(setup.entry_price)
            fill = theo + adverse * tick if direction == "bullish" else theo - adverse * tick
            stop = float(setup.stop_price)
            risk = abs(fill - stop)
            if risk < tick:
                continue
            tgt = fill + target_r * risk if direction == "bullish" else fill - target_r * risk
            flatten = int(seq["session_end"])
            path = resolve_path(by1.get(td, []), entry_ts=int(setup.entry_timestamp), direction=direction, entry=fill, stop=stop, target=tgt, flatten_ts=flatten)
            pts = signed_points(direction, fill, path.get("exit"))
            if pts is not None:
                pts = pts - adverse * tick - comm
            r = None if pts is None or risk <= 0 else pts / risk
            trades.append(PortTrade(
                instrument=instrument, family="VWAP_MR", candidate=candidate, trading_date=td,
                direction=direction, entry_ts=int(setup.entry_timestamp), entry=fill, stop=stop, target=tgt,
                exit_ts=path.get("exit_ts"), exit=path.get("exit"), outcome=path["outcome"],
                points=pts, r_multiple=r, mfe=path.get("mfe"), mae=path.get("mae"),
                hold_sec=None if path.get("exit_ts") is None else int(path["exit_ts"]) - int(setup.entry_timestamp),
                extras={"sigma": sigma, "target_r": target_r, "risk": risk, "theo": theo},
            ))
            taken = True
    return trades


def dvp_trades_to_port(
    instrument: str,
    trades,
    *,
    adverse: float,
    candidate: str,
) -> list[PortTrade]:
    inst = INSTRUMENTS[instrument]
    tick = float(inst["tick"])
    comm = float(inst["commission_points"])
    out: list[PortTrade] = []
    for t in trades:
        if in_news_blackout(int(t.entry_timestamp), instrument):
            out.append(PortTrade(
                instrument=instrument, family="DVP", candidate=candidate, trading_date=t.trading_date,
                direction=t.direction, entry_ts=int(t.entry_timestamp), entry=float(t.entry_price),
                stop=float(t.stop_price), target=float(t.target_price), exit_ts=None, exit=None,
                outcome="NEWS_BLACKOUT", points=None, r_multiple=None, news_blackout=True,
            ))
            continue
        fill = float(t.entry_price)
        risk = abs(fill - float(t.stop_price))
        pts = t.points
        if pts is not None:
            pts = float(pts) - 2.0 * adverse * tick - comm
        r = None if pts is None or risk <= 0 else pts / risk
        out.append(PortTrade(
            instrument=instrument, family="DVP", candidate=candidate, trading_date=t.trading_date,
            direction=t.direction, entry_ts=int(t.entry_timestamp), entry=fill,
            stop=float(t.stop_price), target=float(t.target_price),
            exit_ts=t.exit_timestamp, exit=t.exit_price, outcome=t.outcome,
            points=pts, r_multiple=r,
            extras={"risk": (t.extras or {}).get("risk_points") or risk},
        ))
    return out


def run_dvp(
    instrument: str,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    cfg: DVPStrategyConfig,
    *,
    adverse: float,
    candidate: str,
) -> tuple[list[PortTrade], dict[str, Any]]:
    clock = CLOCKS[f"{instrument}_DVP"]
    raw, guard = replay_all_days(bars_1m, bars_5m, bars_15m, cfg, clock=clock, instrument=instrument)
    return dvp_trades_to_port(instrument, raw, adverse=adverse, candidate=candidate), guard


def structural_dvp(instrument: str, bars_1m, bars_5m, bars_15m, cfg: DVPStrategyConfig) -> dict[str, Any]:
    clock = CLOCKS[f"{instrument}_DVP"]
    raw, guard = replay_all_days(bars_1m, bars_5m, bars_15m, cfg, clock=clock, instrument=instrument)
    n = len(raw)
    if not n:
        return {"n_trades": 0, **guard}
    pos = sum(1 for t in raw if t.points is not None and t.points > 0)
    fails = sum(1 for t in raw if t.outcome == "STOP_HIT")
    return {
        "n_trades": n,
        "win_rate_gross": pos / n,
        "stop_rate": fails / n,
        "mean_points_gross": statistics.mean([t.points for t in raw if t.points is not None]),
        **guard,
    }


def score(trades: list[PortTrade], *, train_end=TRAIN_END, holdout_start=HOLDOUT_START) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT", "FORCE_CLOSE") and t.points is not None]
    amb = [t for t in trades if t.outcome == "AMBIGUOUS"]
    news = [t for t in trades if t.news_blackout]
    entered = [t for t in trades if t.outcome not in ("NEWS_BLACKOUT",)]

    def _pack(rows: list[PortTrade]) -> dict[str, Any]:
        if not rows:
            return {"n_resolved": 0, "n_ambiguous": 0, "expectancy_r": None, "expectancy_points": None, "win_rate": None, "profit_factor": None}
        pts = [float(t.points) for t in rows if t.points is not None]
        rs = [float(t.r_multiple) for t in rows if t.r_multiple is not None]
        wins = [p for p in pts if p > 0]
        losses = [abs(p) for p in pts if p <= 0]
        wr = sum(1 for p in pts if p > 0) / len(pts)
        equity = peak = 0.0
        max_dd = 0.0
        streak = max_streak = 0
        day: dict[str, float] = defaultdict(float)
        for t in rows:
            if t.points is None:
                continue
            equity += float(t.points)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            if t.points <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
            day[t.trading_date] += float(t.points)
        longs = [t for t in rows if t.direction in ("bullish", "long")]
        shorts = [t for t in rows if t.direction in ("bearish", "short")]

        def side(xs):
            pp = [float(t.points) for t in xs if t.points is not None]
            rr = [float(t.r_multiple) for t in xs if t.r_multiple is not None]
            if not pp:
                return {"n": 0, "win_rate": None, "expectancy_points": None, "expectancy_r": None}
            return {
                "n": len(pp),
                "win_rate": sum(1 for x in pp if x > 0) / len(pp),
                "expectancy_points": statistics.mean(pp),
                "expectancy_r": None if not rr else statistics.mean(rr),
            }

        years = []
        by_y: dict[int, list[PortTrade]] = defaultdict(list)
        for t in rows:
            by_y[int(t.trading_date[:4])].append(t)
        for y in sorted(by_y):
            ys = by_y[y]
            yp = [float(t.points) for t in ys if t.points is not None]
            yr = [float(t.r_multiple) for t in ys if t.r_multiple is not None]
            yw = sum(1 for x in yp if x > 0)
            yloss = sum(abs(x) for x in yp if x <= 0)
            ywin = sum(x for x in yp if x > 0)
            years.append({
                "year": y,
                "n_resolved": len(yp),
                "win_rate": None if not yp else yw / len(yp),
                "expectancy_points": None if not yp else statistics.mean(yp),
                "expectancy_r": None if not yr else statistics.mean(yr),
                "profit_factor": None if not yloss else ywin / yloss,
            })
        risks = [float((t.extras or {}).get("risk") or abs(t.entry - t.stop)) for t in rows]
        holds = [int(t.hold_sec) for t in rows if t.hold_sec]
        daily_loss = [v for v in day.values() if v < 0]
        return {
            "n_resolved": len(rows),
            "n_ambiguous": 0,
            "expectancy_points": statistics.mean(pts),
            "expectancy_r": None if not rs else statistics.mean(rs),
            "win_rate": wr,
            "profit_factor": None if not losses else (sum(wins) / sum(losses) if losses else None),
            "max_dd_points": abs(max_dd),
            "max_consec_losses": max_streak,
            "avg_stop_points": None if not risks else statistics.mean(risks),
            "median_stop_points": None if not risks else statistics.median(risks),
            "p95_stop_points": None if len(risks) < 8 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
            "avg_hold_sec": None if not holds else statistics.mean(holds),
            "avg_mfe": statistics.mean([float(t.mfe) for t in rows if t.mfe is not None]) if any(t.mfe is not None for t in rows) else None,
            "avg_mae": statistics.mean([float(t.mae) for t in rows if t.mae is not None]) if any(t.mae is not None for t in rows) else None,
            "worst_day_points": None if not daily_loss else min(daily_loss),
            "n_days": len(day),
            "trades_per_year": None if not rows else len(rows) / max(1, int(rows[-1].trading_date[:4]) - int(rows[0].trading_date[:4]) + 1),
            "long": side(longs),
            "short": side(shorts),
            "years": years,
            "daily_pnl": dict(day),
        }

    full = _pack(resolved)
    full["n_ambiguous"] = len(amb)
    full["n_news_removed"] = len(news)
    full["n_entered"] = len(entered)
    train = _pack([t for t in resolved if t.trading_date <= train_end])
    hold = _pack([t for t in resolved if t.trading_date >= holdout_start])
    return {"full": full, "train": train, "holdout": hold}


def daily_corr(a: dict[str, float], b: dict[str, float]) -> dict[str, Any]:
    keys = sorted(set(a) & set(b))
    if len(keys) < 10:
        return {"n_overlap": len(keys), "daily_pnl_correlation": None, "losing_day_overlap": None}
    xs = [a[k] for k in keys]
    ys = [b[k] for k in keys]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    corr = None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    lose_a = {k for k, v in a.items() if v < 0}
    lose_b = {k for k, v in b.items() if v < 0}
    return {
        "n_overlap": len(keys),
        "daily_pnl_correlation": corr,
        "losing_day_overlap": len(lose_a & lose_b),
        "active_day_union": len(set(a) | set(b)),
    }
