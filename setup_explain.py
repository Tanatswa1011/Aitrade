"""Human-readable setup explanation from canonical TradeSetup data only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from models import TradeSetup


def _fmt_ts(ts: Any) -> str:
    if ts is None:
        return "n/a"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(ts)


def _num(x: Any, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return str(x)


def explain_setup(setup: TradeSetup) -> str:
    """
    Format TradeSetup into a deterministic text report.

    Uses only fields present on the model — never invents prices/events.
    """
    lines: list[str] = []
    title_dir = setup.direction or "Unknown-direction"
    lines.append(f"{setup.symbol or 'UNKNOWN'} {title_dir.title()} Session Setup")
    lines.append(f"Status: {setup.status}")
    lines.append(f"Setup ID: {setup.id}")
    lines.append("")

    htf = setup.higher_timeframe_context or {}
    daily = (htf.get("daily_bias") or {}) if isinstance(htf, dict) else {}
    h4 = (htf.get("h4_bias") or {}) if isinstance(htf, dict) else {}
    lines.append("Higher Timeframe Context:")
    lines.append("  Daily")
    lines.append(f"    Bias: {str(daily.get('direction') or 'unknown').title()}")
    if daily.get("confidence"):
        lines.append(f"    Confidence: {daily.get('confidence')}")
    evid = daily.get("evidence") or {}
    if evid.get("reason"):
        lines.append(f"    Reason: {evid.get('reason')}")
    if evid.get("last_break_timestamp") is not None:
        lines.append(f"    Break: {_fmt_ts(evid.get('last_break_timestamp'))}")
    if evid.get("bars_since_break") is not None:
        lines.append(f"    Bars since break: {evid.get('bars_since_break')}")
    if evid.get("last_confirmed_swing_high") is not None:
        lines.append(f"    Last swing high: {_num(evid.get('last_confirmed_swing_high'))}")
    if evid.get("last_confirmed_swing_low") is not None:
        lines.append(f"    Last swing low: {_num(evid.get('last_confirmed_swing_low'))}")
    lines.append("  4H")
    lines.append(f"    Bias: {str(h4.get('direction') or 'unknown').title()}")
    if h4.get("confidence"):
        lines.append(f"    Confidence: {h4.get('confidence')}")
    evid4 = h4.get("evidence") or {}
    if evid4.get("reason"):
        lines.append(f"    Reason: {evid4.get('reason')}")
    if evid4.get("last_break_timestamp") is not None:
        lines.append(f"    Break: {_fmt_ts(evid4.get('last_break_timestamp'))}")
    if evid4.get("bars_since_break") is not None:
        lines.append(f"    Bars since break: {evid4.get('bars_since_break')}")
    lines.append(
        f"  Alignment: {str(htf.get('alignment') or 'unknown').replace('_', ' ').title()}"
    )
    if setup.setup_vs_daily:
        lines.append(f"  Setup vs Daily: {setup.setup_vs_daily}")
    if setup.setup_vs_h4:
        lines.append(f"  Setup vs 4H: {setup.setup_vs_h4}")
    provider = (htf.get("source_metadata") or {}).get("provider")
    if provider:
        lines.append(f"  Bias source: {provider}")
    lines.append("")

    lines.append("Session:")
    lines.append(f"  {setup.session}")
    if setup.trading_date:
        lines.append(f"  Trading date: {setup.trading_date}")
    exec_tf = setup.execution_timeframe or setup.timeframe
    if exec_tf:
        lines.append(f"  Execution TF: {exec_tf}")
    if setup.timeframe and setup.timeframe != exec_tf:
        lines.append(f"  Chart/source TF: {setup.timeframe}")

    sr = setup.session_range or {}
    if sr:
        lines.append(f"  High: {_num(sr.get('high'))}")
        lines.append(f"  Low: {_num(sr.get('low'))}")
        lines.append(f"  Complete: {sr.get('complete')}")
        lines.append(f"  Coverage: {sr.get('coverage_status')}")
        lines.append(f"  Source: {sr.get('source')}")

    meta = setup.source_metadata or {}
    if meta.get("reason"):
        lines.append("")
        lines.append("Progress note:")
        lines.append(f"  {meta['reason']}")

    sweep = setup.sweep
    if sweep:
        lines.append("")
        lines.append("Liquidity event:")
        lines.append(
            f"  {setup.session} {sweep.get('side')} swept @ {_num(sweep.get('level'))}"
        )
        lines.append(f"  Sweep time: {_fmt_ts(sweep.get('sweep_timestamp'))}")
        extreme = meta.get("sweep_extreme")
        if extreme is not None:
            lines.append(f"  Sweep extreme: {_num(extreme)}")
        else:
            lines.append(f"  Sweep price: {_num(sweep.get('sweep_price'))}")

    conf = setup.confirmation
    if conf:
        lines.append("")
        lines.append("Structure:")
        lines.append(
            f"  {str(conf.get('direction')).title()} {conf.get('kind')} "
            f"@ {_num(conf.get('level'))}"
        )
        lines.append(f"  Time: {_fmt_ts(conf.get('event_timestamp'))}")
        lines.append(f"  Timing confidence: {conf.get('timing_confidence')}")
        lines.append(f"  Source: {conf.get('source')}")

    fvg = setup.fvg
    if fvg:
        lines.append("")
        lines.append("FVG:")
        lines.append(f"  {_num(fvg.get('low'))} - {_num(fvg.get('high'))}")
        lines.append(f"  CE: {_num(fvg.get('midpoint'))}")
        lines.append(f"  Created: {_fmt_ts(fvg.get('created_timestamp'))}")

    if setup.entries:
        lines.append("")
        lines.append("Entry candidates:")
        for item in setup.entries:
            # Support both EntryAnalysis objects and dicts from to_dict round-trip
            if hasattr(item, "entry"):
                entry = item.entry.to_dict() if hasattr(item.entry, "to_dict") else item.entry
                risk = None if item.risk is None else (
                    item.risk.to_dict() if hasattr(item.risk, "to_dict") else item.risk
                )
                target = None if item.target is None else (
                    item.target.to_dict() if hasattr(item.target, "to_dict") else item.target
                )
            else:
                entry = (item or {}).get("entry") or {}
                risk = (item or {}).get("risk")
                target = (item or {}).get("target")

            mode = entry.get("mode")
            lines.append(f"  {mode}")
            lines.append(f"    Triggered: {entry.get('triggered')} ({entry.get('status')})")
            lines.append(f"    Entry: {_num(entry.get('price'))}")
            lines.append(f"    Entry depth: {_num(entry.get('entry_depth'), 4)}")
            lines.append(f"    Max retrace depth: {_num(entry.get('max_retrace_depth'), 4)}")
            if risk:
                lines.append(f"    Stop mode: {risk.get('stop_mode')}")
                lines.append(f"    Stop: {_num(risk.get('stop_price'))}")
                lines.append(f"    Risk: {_num(risk.get('risk_distance'))}")
                lines.append(f"    Risk valid: {risk.get('valid')}")
                if risk.get("invalidation_reason"):
                    lines.append(f"    Risk invalidation: {risk.get('invalidation_reason')}")
            if target and target.get("fixed_rr_targets"):
                for ft in target["fixed_rr_targets"]:
                    lines.append(
                        f"    {ft.get('rr')}R: {_num(ft.get('price'))}"
                    )
            if target:
                if target.get("opposite_liquidity_label"):
                    lines.append(
                        f"    {target.get('opposite_liquidity_label')}: "
                        f"{_num(target.get('opposite_liquidity_price'))}"
                    )
                    lines.append(
                        f"    RR to opposite: {_num(target.get('rr_to_opposite'), 4)} "
                        f"(valid={target.get('opposite_target_valid')})"
                    )

    if setup.invalidation_reason:
        lines.append("")
        lines.append(f"Invalidation: {setup.invalidation_reason}")
    if setup.expiry_reason:
        lines.append(f"Expiry: {setup.expiry_reason}")

    if meta.get("dst_uncertainty"):
        lines.append("")
        lines.append("Reliability:")
        lines.append(f"  Session source: {meta.get('session_source')}")
        lines.append(f"  Session confidence: {meta.get('session_confidence')}")
        lines.append(f"  Coverage: {meta.get('coverage_status')}")
        if meta.get("confirmation_timing_confidence"):
            lines.append(
                f"  CHoCH timing confidence: {meta.get('confirmation_timing_confidence')}"
            )

    lines.append("")
    lines.append("Documented ambiguity:")
    lines.append(f"  {meta.get('trigger_bar_stop_ambiguity') or 'TRIGGER_BAR_STOP_AMBIGUITY'}")

    return "\n".join(lines)
