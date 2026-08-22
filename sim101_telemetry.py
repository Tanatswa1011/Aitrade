"""Phase 55B.0 — authoritative read-only Sim101 MNQ position from NinjaTrader.

FundedNext MCP / top-level AddOn ``position`` (FundedNext) must never substitute.
ATI log parse is diagnostic fallback only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SIM101_STALE_SEC = 30.0
SOURCE_NT_ACCOUNT = "NINJATRADER_ACCOUNT_POSITION"
SOURCE_ATI_FALLBACK = "ATI_LOG_FALLBACK"
SOURCE_MISSING = "SIM101_MISSING"
SOURCE_STALE = "SIM101_STALE"
SOURCE_UNKNOWN = "POSITION_UNKNOWN"


def _parse_ts(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e12:
            v /= 1000.0
        return v
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _side_qty(pos: dict[str, Any]) -> tuple[str, int, Optional[float]]:
    qty = int(pos.get("quantity") or 0)
    mp = str(pos.get("side") or pos.get("market_position") or "FLAT").upper()
    if qty == 0 or mp == "FLAT":
        return "FLAT", 0, None
    if "LONG" in mp:
        return "LONG", abs(qty), pos.get("average_price")
    if "SHORT" in mp:
        return "SHORT", abs(qty), pos.get("average_price")
    return "FLAT", 0, None


def parse_sim101_position(
    dump: Optional[dict[str, Any]],
    *,
    now: Optional[float] = None,
    stale_sec: float = SIM101_STALE_SEC,
    dump_mtime: Optional[float] = None,
) -> dict[str, Any]:
    """Parse Sim101 MNQ from AddOn dump. Does not read FundedNext ``position``."""
    now_ts = time_now(now)
    empty = {
        "present": False,
        "known": False,
        "account": "Sim101",
        "instrument": "MNQ 09-26",
        "quantity": None,
        "side": None,
        "average_price": None,
        "flat": None,
        "timestamp": None,
        "source": SOURCE_MISSING,
        "stale": False,
        "age_sec": None,
        "read_only": True,
    }
    if not isinstance(dump, dict):
        empty["source"] = SOURCE_MISSING
        return empty

    sim = dump.get("sim101")
    if not isinstance(sim, dict):
        return dict(empty, source=SOURCE_MISSING, note="sim101_block_absent")

    present = bool(sim.get("present"))
    pos = sim.get("position") if isinstance(sim.get("position"), dict) else None
    if not present or pos is None:
        # Old AddOn: present+excluded without position → not a known flat.
        return dict(
            empty,
            present=present,
            account=sim.get("account") or sim.get("id") or "Sim101",
            source=SOURCE_MISSING if not present else SOURCE_UNKNOWN,
            note="sim101_position_not_exposed" if present else "sim101_account_missing",
        )

    ts_raw = pos.get("timestamp") or dump.get("timestamp") or dump.get("ts")
    ts = _parse_ts(ts_raw)
    mtime = dump_mtime if dump_mtime is not None else dump.get("_mtime")
    try:
        mtime = float(mtime) if mtime is not None else None
    except (TypeError, ValueError):
        mtime = None
    age = None
    if ts is not None:
        age = max(0.0, now_ts - ts)
    elif mtime is not None:
        age = max(0.0, now_ts - mtime)
        ts = mtime
    stale = age is None or age > float(stale_sec)
    side, qty, avg = _side_qty(pos)
    instr = str(pos.get("instrument") or "MNQ 09-26")
    if stale:
        return {
            "present": True,
            "known": False,
            "account": sim.get("account") or "Sim101",
            "instrument": instr,
            "quantity": qty,
            "side": side,
            "average_price": avg,
            "flat": None,
            "timestamp": ts_raw,
            "source": SOURCE_STALE,
            "stale": True,
            "age_sec": age,
            "read_only": True,
        }
    return {
        "present": True,
        "known": True,
        "account": sim.get("account") or "Sim101",
        "instrument": instr,
        "quantity": qty,
        "side": side,
        "market_position": side.title() if side != "FLAT" else "Flat",
        "average_price": avg if side != "FLAT" else None,
        "flat": side == "FLAT" and qty == 0,
        "timestamp": ts_raw,
        "source": str(pos.get("source") or SOURCE_NT_ACCOUNT),
        "stale": False,
        "age_sec": age,
        "read_only": True,
    }


def merge_ati_fallback(primary: dict[str, Any], ati: Optional[dict[str, Any]]) -> dict[str, Any]:
    """ATI is diagnostic only. Never overrides a known NT Sim101 reading."""
    out = dict(primary)
    out["ati_diagnostic"] = ati
    if primary.get("known"):
        return out
    if not isinstance(ati, dict):
        return out
    if ati.get("flat") is None and not ati.get("known"):
        return out
    qty = int(ati.get("quantity") or 0)
    mp = str(ati.get("market_position") or ati.get("side") or "")
    if not mp:
        return out
    flat = ati.get("flat")
    if flat is None:
        return out
    side = "FLAT" if flat or qty == 0 else ("LONG" if "long" in mp.lower() else "SHORT")
    out.update(
        {
            "known": True,
            "present": True,
            "quantity": 0 if side == "FLAT" else abs(qty),
            "side": side,
            "market_position": mp,
            "average_price": ati.get("average_price") if side != "FLAT" else None,
            "flat": side == "FLAT",
            "source": SOURCE_ATI_FALLBACK,
            "read_only": True,
        }
    )
    return out


def fundednext_must_not_substitute(dump: Optional[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    """Guard: top-level dump position is FundedNext, never Sim101 actual."""
    out = dict(parsed)
    fn_pos = dump.get("position") if isinstance(dump, dict) else None
    out["fundednext_position_ignored"] = True
    out["fundednext_top_level_position"] = fn_pos if isinstance(fn_pos, dict) else None
    if not parsed.get("known"):
        out["flat"] = None
        out["known"] = False
    return out


def time_now(now: Optional[float] = None) -> float:
    if now is not None:
        return float(now)
    return datetime.now(timezone.utc).timestamp()


def recovery_from_sim101(
    actual: dict[str, Any],
    *,
    expected_flat: bool = True,
    aittrade_orders: int = 0,
) -> str:
    if not actual.get("known") or actual.get("flat") is None:
        return "UNKNOWN_STATE"
    if actual.get("stale"):
        return "UNKNOWN_STATE"
    if expected_flat and actual.get("flat") and aittrade_orders == 0:
        return "FLAT_SAFE"
    if expected_flat and not actual.get("flat"):
        return "ORPHAN_POSITION"
    if not expected_flat and actual.get("flat"):
        return "UNKNOWN_STATE"
    return "UNKNOWN_STATE"
