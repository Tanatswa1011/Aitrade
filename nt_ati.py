"""NinjaTrader ATI bridge — Sim101 MNQ only (BUY/SELL + bracket/OCO validation).

OIF: write oif*.txt directly into Documents\\NinjaTrader 8\\incoming (no .tmp rename).

Bracket mechanism:
  PLACE MARKET entry → poll NT log for actual fill → PLACE STOPMARKET + LIMIT
  sharing one OCO ID (ATI OCO field). Prices derive from the real fill; never faked.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_NT_ROOT = Path.home() / "OneDrive" / "Documents" / "NinjaTrader 8"
ALT_NT_ROOT = Path.home() / "Documents" / "NinjaTrader 8"

DEFAULT_ACCOUNT = "Sim101"
DEFAULT_INSTRUMENT = "MNQ SEP26"
MNQ_TICK_SIZE = 0.25
BRACKET_DISTANCE_POINTS = 5.0
ALLOWED_ACTIONS = {"BUY", "SELL"}

PROJECT_LOG_DIR = Path("logs")
BRACKET_JOURNAL = PROJECT_LOG_DIR / "ninjatrader_bracket_tests.jsonl"
ACTIVE_STATE_PATH = PROJECT_LOG_DIR / "ninjatrader_bracket_active.json"

BRACKET_MECHANISM = "OIF_FILL_THEN_OCO_CHILDREN"
BRACKET_MECHANISM_WHY = (
    "ATI PLACE cannot attach absolute stop/target prices without an existing ATM template. "
    "Entry fill is readable from the NinjaTrader log; child STOPMARKET+LIMIT with a shared "
    "OCO ID is the documented OIF path that uses the real fill and NT-native OCO cancel."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_stamp(fmt: str) -> str:
    return _utc_now().strftime(fmt)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def resolve_nt_root(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit
    for p in (DEFAULT_NT_ROOT, ALT_NT_ROOT):
        if (p / "incoming").is_dir():
            return p
    raise FileNotFoundError("NinjaTrader 8 documents folder not found (incoming missing)")


def assert_sim101(account: str) -> None:
    """Hard lock: Sim101 only for this execution phase."""
    if (account or "").strip() != DEFAULT_ACCOUNT:
        raise PermissionError(f"LIVE_ACCOUNT_BLOCKED:{account}")


def assert_sim_only(account: str) -> None:
    """Backward-compatible name → Sim101 hard lock."""
    assert_sim101(account)


def assert_mnq_instrument(instrument: str) -> None:
    s = (instrument or "").strip()
    if s != DEFAULT_INSTRUMENT:
        su = s.upper()
        if su.startswith("NQ") and not su.startswith("MNQ"):
            raise PermissionError(f"REFUSED_FULL_SIZE_NQ:{instrument}")
        raise PermissionError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{instrument}")


def assert_qty_one(quantity: int) -> None:
    if int(quantity) != 1:
        raise PermissionError(f"REFUSED_QTY_NOT_1:{quantity}")


def round_to_tick(price: float, tick: float = MNQ_TICK_SIZE) -> float:
    ticks = round(float(price) / float(tick))
    return round(ticks * float(tick), 10)


def validate_tick_aligned(price: float, tick: float = MNQ_TICK_SIZE) -> bool:
    q = round(float(price) / float(tick))
    return abs(float(price) - q * float(tick)) < 1e-9


def new_oco_id(prefix: str = "AITRADE_TEST") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def long_bracket_prices(entry: float, distance: float = BRACKET_DISTANCE_POINTS) -> dict[str, float]:
    entry_r = round_to_tick(entry)
    stop = round_to_tick(entry_r - float(distance))
    target = round_to_tick(entry_r + float(distance))
    return {"entry": entry_r, "stop": stop, "target": target}


def short_bracket_prices(entry: float, distance: float = BRACKET_DISTANCE_POINTS) -> dict[str, float]:
    entry_r = round_to_tick(entry)
    stop = round_to_tick(entry_r + float(distance))
    target = round_to_tick(entry_r - float(distance))
    return {"entry": entry_r, "stop": stop, "target": target}


def asymmetric_bracket_prices(
    direction: str,
    entry: float,
    *,
    stop_points: float,
    target_points: float,
) -> dict[str, float]:
    """DVP production brackets (e.g. long 80/40, short 80/50)."""
    entry_r = round_to_tick(entry)
    d = direction.upper()
    if d in ("LONG", "BULLISH"):
        return {
            "entry": entry_r,
            "stop": round_to_tick(entry_r - float(stop_points)),
            "target": round_to_tick(entry_r + float(target_points)),
        }
    if d in ("SHORT", "BEARISH"):
        return {
            "entry": entry_r,
            "stop": round_to_tick(entry_r + float(stop_points)),
            "target": round_to_tick(entry_r - float(target_points)),
        }
    raise ValueError(f"direction_must_be_LONG_or_SHORT:{direction}")


def ati_enabled(nt_root: Optional[Path] = None) -> bool:
    root = resolve_nt_root(nt_root)
    cfg = root / "Config.xml"
    if not cfg.exists():
        return False
    return "<IsAtiEnabled>true</IsAtiEnabled>" in cfg.read_text(encoding="utf-8", errors="ignore")


def set_ati_enabled(enabled: bool, *, nt_root: Optional[Path] = None) -> dict[str, Any]:
    root = resolve_nt_root(nt_root)
    cfg = root / "Config.xml"
    if not cfg.exists():
        raise FileNotFoundError(cfg)
    text = cfg.read_text(encoding="utf-8")
    want = "<IsAtiEnabled>true</IsAtiEnabled>" if enabled else "<IsAtiEnabled>false</IsAtiEnabled>"
    opposite = "<IsAtiEnabled>false</IsAtiEnabled>" if enabled else "<IsAtiEnabled>true</IsAtiEnabled>"
    if want in text:
        return {"ok": True, "changed": False, "enabled": enabled, "path": str(cfg)}
    if opposite not in text:
        raise RuntimeError("IsAtiEnabled tag not found in Config.xml")
    backup = cfg.with_suffix(".xml.bak_aitrade")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    cfg.write_text(text.replace(opposite, want, 1), encoding="utf-8")
    return {"ok": True, "changed": True, "enabled": enabled, "backup": str(backup), "path": str(cfg)}


def build_place_oif(
    *,
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
    action: str = "BUY",
    quantity: int = 1,
    order_type: str = "MARKET",
    limit_price: float = 0.0,
    stop_price: float = 0.0,
    tif: str = "DAY",
    oco_id: str = "",
    order_id: str = "",
    strategy: str = "",
    strategy_id: str = "",
) -> str:
    assert_sim101(account)
    assert_mnq_instrument(instrument)
    assert_qty_one(quantity)
    act = action.upper().strip()
    if act not in ALLOWED_ACTIONS:
        raise ValueError(f"action_must_be_BUY_or_SELL:{action}")
    otype = order_type.upper().strip()
    oid = order_id or f"AITRADE_{_utc_stamp('%Y%m%d%H%M%S')}"
    lim = 0 if limit_price in (None, "") else float(limit_price)
    stp = 0 if stop_price in (None, "") else float(stop_price)
    if otype in ("LIMIT", "STOPLIMIT") and lim != 0 and not validate_tick_aligned(lim):
        raise ValueError(f"limit_not_tick_aligned:{lim}")
    if otype in ("STOPMARKET", "STOPLIMIT") and stp != 0 and not validate_tick_aligned(stp):
        raise ValueError(f"stop_not_tick_aligned:{stp}")
    # Match proven OIF style: integer 0 (not 0.0) for unused prices
    lim_s = "0" if float(lim) == 0.0 else str(lim)
    stp_s = "0" if float(stp) == 0.0 else str(stp)
    # PLACE;account;instrument;action;qty;type;limit;stop;TIF;OCO;orderId;strategy;strategyId
    return (
        f"PLACE;{account};{instrument};{act};{int(quantity)};{otype};"
        f"{lim_s};{stp_s};{tif.upper()};{oco_id};{oid};{strategy};{strategy_id}"
    )


def build_close_position_oif(
    *,
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
) -> str:
    assert_sim101(account)
    assert_mnq_instrument(instrument)
    return f"CLOSEPOSITION;{account};{instrument};;;;;;;;;;"


def build_cancel_oif(order_id: str, *, strategy_id: str = "") -> str:
    if not order_id:
        raise ValueError("order_id_required")
    return f"CANCEL;;;;;;;;;;{order_id};{strategy_id};"


def drop_oif(
    line: str,
    *,
    nt_root: Optional[Path] = None,
    prefix: str = "oif",
) -> dict[str, Any]:
    root = resolve_nt_root(nt_root)
    incoming = root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp("%Y%m%d%H%M%S%f")
    path = incoming / f"{prefix}{stamp[-6:]}.txt"
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(line.strip() + "\n")
        fh.flush()
    return {
        "ok": True,
        "path": str(path),
        "line": line.strip(),
        "incoming": str(incoming),
        "ati_enabled_in_config": ati_enabled(root),
    }


def drop_oif_lines(
    lines: list[str],
    *,
    nt_root: Optional[Path] = None,
    prefix: str = "oif",
) -> dict[str, Any]:
    """Stack multiple OIF instructions in one file (NT supports multi-line OIF)."""
    root = resolve_nt_root(nt_root)
    incoming = root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp("%Y%m%d%H%M%S%f")
    path = incoming / f"{prefix}{stamp[-6:]}.txt"
    body = "\n".join(x.strip() for x in lines if x.strip()) + "\n"
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(body)
        fh.flush()
    return {
        "ok": True,
        "path": str(path),
        "lines": [x.strip() for x in lines if x.strip()],
        "incoming": str(incoming),
        "ati_enabled_in_config": ati_enabled(root),
    }


def place_sim_mnq_test(
    *,
    action: str = "BUY",
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
    nt_root: Optional[Path] = None,
    submit: bool = True,
) -> dict[str, Any]:
    """Drop exactly one Sim101 MNQ market order via ATI."""
    line = build_place_oif(account=account, instrument=instrument, action=action, quantity=1)
    out: dict[str, Any] = {
        "ok": True,
        "mode": "SIM_TEST_ONLY",
        "account": account,
        "instrument": instrument,
        "action": action.upper(),
        "quantity": 1,
        "line": line,
        "submitted": False,
    }
    if not submit:
        return out
    info = drop_oif(line, nt_root=nt_root)
    out.update(info)
    out["submitted"] = True
    out["note"] = (
        "OIF dropped. If ATI is disabled, enable Tools→Options→Automated trading interface "
        "and/or restart NinjaTrader, then re-run."
    )
    return out


def wait_for_oif_consumed(path: str | Path, *, timeout_sec: float = 8.0) -> dict[str, Any]:
    p = Path(path)
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if not p.exists():
            return {"consumed": True, "elapsed_sec": time.time() - t0}
        time.sleep(0.25)
    return {"consumed": False, "elapsed_sec": time.time() - t0, "still_exists": p.exists()}


def latest_log_path(nt_root: Optional[Path] = None) -> Path:
    root = resolve_nt_root(nt_root)
    logs = sorted((root / "log").glob("log.*.en.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not logs:
        logs = sorted((root / "log").glob("log.*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not logs:
        raise FileNotFoundError("no NinjaTrader log files")
    return logs[0]


def read_log_tail(*, nt_root: Optional[Path] = None, n: int = 400) -> list[str]:
    path = latest_log_path(nt_root)
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]


def tail_log_matches(patterns: list[str], *, nt_root: Optional[Path] = None, n: int = 80) -> list[str]:
    lines = read_log_tail(nt_root=nt_root, n=n)
    return [line for line in lines if any(p.lower() in line.lower() for p in patterns)]


_POS_RE = re.compile(
    r"Instrument='(?P<instr>[^']+)' Account='(?P<acct>[^']+)' Average price=(?P<avg>[0-9.]+) "
    r"Quantity=(?P<qty>\d+) Market position=(?P<pos>Flat|Long|Short)"
)
_FILL_RE = re.compile(
    r"Order='(?P<oid>[^'/]+)(?:/[^']*)?' Name='(?P<name>[^']*)' New state='Filled' "
    r"Instrument='(?P<instr>[^']+)' Action='(?P<action>[^']+)' .*? "
    r"Quantity=(?P<qty>\d+) Type='(?P<otype>[^']+)' .*? Filled=(?P<filled>\d+) Fill price=(?P<px>[0-9.]+)"
)
_ORDER_STATE_RE = re.compile(
    r"Order='(?P<oid>[^'/]+)(?:/[^']*)?' Name='(?P<name>[^']*)' New state='(?P<state>[^']+)' "
    r"Instrument='(?P<instr>[^']+)'"
)


def parse_mnq_sim_position(
    lines: Optional[list[str]] = None,
    *,
    nt_root: Optional[Path] = None,
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
) -> dict[str, Any]:
    lines = lines if lines is not None else read_log_tail(nt_root=nt_root, n=800)
    last = None
    for line in lines:
        m = _POS_RE.search(line)
        if not m:
            continue
        if m.group("acct") != account or m.group("instr") != instrument:
            continue
        last = {
            "account": m.group("acct"),
            "instrument": m.group("instr"),
            "average_price": float(m.group("avg")),
            "quantity": int(m.group("qty")),
            "market_position": m.group("pos"),
            "flat": m.group("pos") == "Flat" or int(m.group("qty")) == 0,
            "raw": line,
        }
    if last is None:
        return {
            "account": account,
            "instrument": instrument,
            "market_position": "UNKNOWN",
            "quantity": None,
            "flat": None,
            "error": "POSITION_UNKNOWN",
        }
    return last


def parse_nt_order_id_after_oif(
    ati_order_id: str,
    lines: Optional[list[str]] = None,
    *,
    nt_root: Optional[Path] = None,
) -> Optional[str]:
    """Map ATI order id → NinjaTrader internal Order='uuid' after OIF processing.

    NT may submit stacked OIF children out of file order. Prefer matching by
    order type (Stop Market vs Limit) when the ATI id encodes STOP/TGT/ENTRY.
    """
    lines = lines if lines is not None else read_log_tail(nt_root=nt_root, n=1200)

    # Prefer type-aware match for bracket children
    want_type = None
    u = ati_order_id.upper()
    if "STOP" in u:
        want_type = "Stop Market"
    elif "TGT" in u or "TARGET" in u:
        want_type = "Limit"
    elif "ENTRY" in u:
        want_type = "Market"

    if want_type:
        # Find OIF processing timestamp region for this ATI id, then type-match Submitted/Filled
        start = None
        for i, line in enumerate(lines):
            if "OIF," in line and ati_order_id in line and "processing" in line:
                start = i
        if start is not None:
            for line in lines[start : start + 60]:
                if ati_order_id.startswith("AITRADE_") and f"Order='" not in line:
                    continue
                m = re.search(
                    rf"Order='(?P<oid>[0-9a-fA-F]+)(?:/[^']*)?' Name='[^']*' New state='Submitted' "
                    rf"Instrument='MNQ SEP26' Action='[^']+' Limit price=[^ ]+ Stop price=[^ ]+ "
                    rf"Quantity=\d+ Type='{re.escape(want_type)}'",
                    line,
                )
                if m:
                    return m.group("oid")
            # looser: Type='...' anywhere on Submitted line after OIF
            for line in lines[start : start + 60]:
                if "New state='Submitted'" not in line or "MNQ SEP26" not in line:
                    continue
                if f"Type='{want_type}'" not in line:
                    continue
                m = re.search(r"Order='(?P<oid>[0-9a-fA-F]+)", line)
                if m:
                    return m.group("oid")

    # Fallback: greedy OIF→next Submitted (entry / legacy)
    oif_events: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if "OIF," not in line or "processing" not in line or "PLACE;" not in line:
            continue
        ids = re.findall(r"(AITRADE_[A-Z0-9_]+)", line, flags=re.IGNORECASE)
        ati_in_line = ids[-1] if ids else None
        if ati_in_line:
            oif_events.append((i, ati_in_line))

    submitted: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.search(
            r"Order='(?P<oid>[0-9a-fA-F]+)(?:/[^']*)?' Name='[^']*' New state='Submitted' "
            r"Instrument='MNQ SEP26'",
            line,
        )
        if m:
            submitted.append((i, m.group("oid")))

    used_sub: set[int] = set()
    mapping: dict[str, str] = {}
    for oif_i, ati in oif_events:
        for s_i, (sub_i, nt_oid) in enumerate(submitted):
            if s_i in used_sub:
                continue
            if sub_i > oif_i:
                mapping[ati] = nt_oid
                used_sub.add(s_i)
                break
    return mapping.get(ati_order_id)


def parse_fill_for_order_id(
    order_id: str,
    lines: Optional[list[str]] = None,
    *,
    nt_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Resolve fill by ATI id (via OIF→NT uuid map) or by NT uuid directly."""
    lines = lines if lines is not None else read_log_tail(nt_root=nt_root, n=1200)
    nt_oid = order_id
    if order_id.startswith("AITRADE_"):
        mapped = parse_nt_order_id_after_oif(order_id, lines=lines, nt_root=nt_root)
        if mapped:
            nt_oid = mapped
        else:
            # ATI id never appears on Fill lines — require mapping
            return None

    for line in reversed(lines):
        if nt_oid not in line or "New state='Filled'" not in line or "Fill price=" not in line:
            continue
        if f"Order='{nt_oid}" not in line:
            continue
        px_m = re.search(r"Fill price=([0-9.]+)", line)
        if not px_m:
            continue
        act_m = re.search(r"Action='([^']+)'", line)
        return {
            "order_id": order_id,
            "nt_order_id": nt_oid,
            "fill_price": float(px_m.group(1)),
            "action": None if not act_m else act_m.group(1),
            "raw": line,
        }
    return None


def wait_for_entry_fill(
    order_id: str,
    *,
    nt_root: Optional[Path] = None,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        hit = parse_fill_for_order_id(order_id, nt_root=nt_root)
        if hit:
            hit["elapsed_sec"] = time.time() - t0
            hit["ok"] = True
            return hit
        time.sleep(0.35)
    # diagnostics
    lines = read_log_tail(nt_root=nt_root, n=200)
    mapped = parse_nt_order_id_after_oif(order_id, lines=lines, nt_root=nt_root)
    return {
        "ok": False,
        "error": "ENTRY_FILL_TIMEOUT",
        "order_id": order_id,
        "mapped_nt_order_id": mapped,
        "recent_oif": [ln for ln in lines if "OIF" in ln][-5:],
    }


def load_active_state(path: Path = ACTIVE_STATE_PATH) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "CORRUPT", "error": "ACTIVE_STATE_CORRUPT"}


def save_active_state(state: Optional[dict[str, Any]], path: Path = ACTIVE_STATE_PATH) -> None:
    PROJECT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if state is None:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def append_bracket_journal(record: dict[str, Any], path: Path = BRACKET_JOURNAL) -> None:
    PROJECT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def guard_ready_for_bracket(
    *,
    nt_root: Optional[Path] = None,
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
    active_path: Path = ACTIVE_STATE_PATH,
    log_lines: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Refuse if not flat, active AITRADE test exists, or position unknown."""
    active = load_active_state(active_path)
    if active and active.get("status") in (
        "BRACKET_ARMED",
        "ENTRY_SUBMITTED",
        "CHILDREN_PENDING",
        "AWAITING_MANUAL_SIM_MOVE",
    ):
        return {
            "ok": False,
            "error_code": "TEST_ORDER_STATE_UNSAFE",
            "reason": "active_aitrade_bracket",
            "active": active,
        }
    if active and active.get("status") == "CORRUPT":
        return {"ok": False, "error_code": "TEST_ORDER_STATE_UNSAFE", "reason": "corrupt_active_state"}

    pos = parse_mnq_sim_position(log_lines, nt_root=nt_root, account=account, instrument=instrument)
    if pos.get("flat") is None:
        return {
            "ok": False,
            "error_code": "TEST_ORDER_STATE_UNSAFE",
            "reason": "position_unknown",
            "position": pos,
        }
    if not pos.get("flat"):
        return {
            "ok": False,
            "error_code": "NOT_FLAT",
            "reason": "Sim101 MNQ position not flat — refuse new bracket",
            "position": pos,
        }
    return {"ok": True, "position": pos, "active": active}


def build_bracket_child_oifs(
    *,
    direction: str,
    entry_fill: float,
    oco_id: str,
    stop_order_id: str,
    target_order_id: str,
    account: str = DEFAULT_ACCOUNT,
    instrument: str = DEFAULT_INSTRUMENT,
    distance: Optional[float] = None,
    stop_points: Optional[float] = None,
    target_points: Optional[float] = None,
) -> dict[str, Any]:
    d = direction.upper()
    if stop_points is not None and target_points is not None:
        px = asymmetric_bracket_prices(
            d, entry_fill, stop_points=float(stop_points), target_points=float(target_points)
        )
    else:
        dist = BRACKET_DISTANCE_POINTS if distance is None else float(distance)
        if d in ("LONG", "BULLISH"):
            px = long_bracket_prices(entry_fill, dist)
            d = "LONG"
        elif d in ("SHORT", "BEARISH"):
            px = short_bracket_prices(entry_fill, dist)
            d = "SHORT"
        else:
            raise ValueError(f"direction_must_be_LONG_or_SHORT:{direction}")

    if d in ("LONG", "BULLISH"):
        stop_action, target_action = "SELL", "SELL"
        d = "LONG"
    elif d in ("SHORT", "BEARISH"):
        stop_action, target_action = "BUY", "BUY"
        d = "SHORT"
    else:
        raise ValueError(f"direction_must_be_LONG_or_SHORT:{direction}")

    stop_line = build_place_oif(
        account=account,
        instrument=instrument,
        action=stop_action,
        quantity=1,
        order_type="STOPMARKET",
        limit_price=0.0,
        stop_price=px["stop"],
        oco_id=oco_id,
        order_id=stop_order_id,
    )
    target_line = build_place_oif(
        account=account,
        instrument=instrument,
        action=target_action,
        quantity=1,
        order_type="LIMIT",
        limit_price=px["target"],
        stop_price=0.0,
        oco_id=oco_id,
        order_id=target_order_id,
    )
    return {
        "prices": px,
        "stop_line": stop_line,
        "target_line": target_line,
        "stop_action": stop_action,
        "target_action": target_action,
    }


def detect_orphan_aitrade_orders(
    order_ids: list[str],
    *,
    nt_root: Optional[Path] = None,
    log_lines: Optional[list[str]] = None,
    oco_id: Optional[str] = None,
) -> dict[str, Any]:
    """Best-effort protective-order detection.

    ATI order ids do not appear in Order='uuid' log lines. Map via OIF processing,
    and/or match shared OCO id. Stop-market may remain Accepted (not Working) until hit.
    """
    lines = log_lines if log_lines is not None else read_log_tail(nt_root=nt_root, n=1200)
    mapped: dict[str, str] = {}
    for oid in order_ids:
        if not oid:
            continue
        if oid.startswith("AITRADE_"):
            nt_oid = parse_nt_order_id_after_oif(oid, lines=lines, nt_root=nt_root)
            if nt_oid:
                mapped[oid] = nt_oid
        else:
            mapped[oid] = oid

    live_states = {"Working", "Accepted", "Submitted", "PartFilled", "TriggerPending", "AcceptedByRisk"}
    last: dict[str, str] = {}
    for ati, nt_oid in mapped.items():
        for line in lines:
            if f"Order='{nt_oid}" not in line or "New state=" not in line:
                continue
            sm = re.search(r"New state='([^']+)'", line)
            if sm:
                last[ati] = sm.group(1)

    # Also count any order currently showing our OCO id as Accepted/Working
    oco_live: list[str] = []
    if oco_id:
        last_by_nt: dict[str, str] = {}
        for line in lines:
            if f"Oco='{oco_id}'" not in line:
                continue
            m = re.search(r"Order='(?P<oid>[^'/]+).*New state='(?P<state>[^']+)'", line)
            if m:
                last_by_nt[m.group("oid")] = m.group("state")
        oco_live = [oid for oid, st in last_by_nt.items() if st in live_states]
        for oid, st in last_by_nt.items():
            last.setdefault(oid, st)

    working = {oid: st for oid, st in last.items() if st in live_states}
    # Prefer OCO live count when available
    orphan_ids = sorted(set(oco_live) | set(working.keys()))
    return {
        "mapped_nt_ids": mapped,
        "last_states": last,
        "orphan_order_ids": orphan_ids,
        "orphan_count": len(orphan_ids),
        "oco_live_count": len(oco_live),
    }


def flatten_sim(
    *,
    nt_root: Optional[Path] = None,
    submit: bool = True,
    active_path: Path = ACTIVE_STATE_PATH,
) -> dict[str, Any]:
    """Cancel known AITRADE child/entry IDs then CLOSEPOSITION Sim101 MNQ only."""
    assert_sim101(DEFAULT_ACCOUNT)
    assert_mnq_instrument(DEFAULT_INSTRUMENT)
    active = load_active_state(active_path) or {}
    cancel_ids = [
        x
        for x in (
            active.get("nt_entry_order_id"),
            active.get("nt_stop_order_id"),
            active.get("nt_target_order_id"),
            active.get("entry_order_id"),
            active.get("stop_order_id"),
            active.get("target_order_id"),
        )
        if x
    ]
    # de-dupe preserve order
    seen: set[str] = set()
    cancel_ids = [x for x in cancel_ids if not (x in seen or seen.add(x))]
    lines = [build_cancel_oif(oid) for oid in cancel_ids]
    lines.append(build_close_position_oif())
    out: dict[str, Any] = {
        "ok": True,
        "mode": "SIM_TEST_ONLY",
        "command": "FLATTEN_SIM",
        "account": DEFAULT_ACCOUNT,
        "instrument": DEFAULT_INSTRUMENT,
        "cancel_order_ids": cancel_ids,
        "oif_lines": lines,
        "submitted": False,
        "status": "FLATTEN_PLANNED",
    }
    if not submit:
        return out

    drop = drop_oif_lines(lines, nt_root=nt_root)
    wait = wait_for_oif_consumed(drop["path"], timeout_sec=8.0)
    time.sleep(0.8)
    pos = parse_mnq_sim_position(nt_root=nt_root)
    orphans = detect_orphan_aitrade_orders(
        cancel_ids,
        nt_root=nt_root,
        oco_id=active.get("oco_id"),
    )
    flat_ok = bool(pos.get("flat"))
    orphan_ok = orphans["orphan_count"] == 0
    status = "FLATTENED" if flat_ok and orphan_ok else ("MANUAL_FLATTEN_REQUIRED" if not flat_ok else "OCO_FAILURE")
    out.update(
        {
            "drop": drop,
            "wait": wait,
            "submitted": True,
            "position_after": pos,
            "orphans": orphans,
            "status": status,
            "ok": status == "FLATTENED",
        }
    )
    if status in ("FLATTENED", "OCO_FAILURE"):
        save_active_state(None, path=active_path)
    append_bracket_journal(
        {
            "test_id": active.get("test_id") or f"FLATTEN_{_utc_stamp('%Y%m%d%H%M%S')}",
            "timestamp": _utc_iso(),
            "account": DEFAULT_ACCOUNT,
            "instrument": DEFAULT_INSTRUMENT,
            "direction": active.get("direction"),
            "quantity": 1,
            "status": status,
            "final_position": pos.get("market_position"),
            "orphan_orders_remaining": orphans["orphan_count"],
            "command": "FLATTEN_SIM",
        }
    )
    return out


def run_bracket_test(
    direction: str,
    *,
    nt_root: Optional[Path] = None,
    submit: bool = True,
    distance: float = BRACKET_DISTANCE_POINTS,
    active_path: Path = ACTIVE_STATE_PATH,
    journal_path: Path = BRACKET_JOURNAL,
    fill_timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """
    Arm a Sim101 MNQ bracket. Leaves trade active for manual Simulated Data Feed test.
    submit=False → plan only (unit tests). Never leaves unprotected position if submit=True
    and children fail (attempts FLATTEN_SIM).
    """
    direction_u = direction.upper().strip()
    if direction_u not in ("LONG", "SHORT"):
        raise ValueError("direction_must_be_LONG_or_SHORT")

    print_banner = {"mode": "SIM_TEST_ONLY", "note": "Global Simulation Mode assumed enabled by user"}
    guard = guard_ready_for_bracket(nt_root=nt_root, active_path=active_path)
    if not guard["ok"] and submit:
        return {**guard, "mode": "SIM_TEST_ONLY", "direction": direction_u}

    test_id = f"BRK_{direction_u}_{_utc_stamp('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    oco_id = new_oco_id()
    entry_oid = f"AITRADE_ENTRY_{uuid.uuid4().hex[:12]}"
    stop_oid = f"AITRADE_STOP_{uuid.uuid4().hex[:12]}"
    target_oid = f"AITRADE_TGT_{uuid.uuid4().hex[:12]}"
    entry_action = "BUY" if direction_u == "LONG" else "SELL"
    entry_line = build_place_oif(action=entry_action, order_id=entry_oid)

    planned = {
        "ok": True,
        "mode": "SIM_TEST_ONLY",
        "banner": print_banner,
        "mechanism": BRACKET_MECHANISM,
        "mechanism_why": BRACKET_MECHANISM_WHY,
        "test_id": test_id,
        "direction": direction_u,
        "account": DEFAULT_ACCOUNT,
        "instrument": DEFAULT_INSTRUMENT,
        "quantity": 1,
        "distance_points": distance,
        "tick_size": MNQ_TICK_SIZE,
        "oco_id": oco_id,
        "entry_order_id": entry_oid,
        "stop_order_id": stop_oid,
        "target_order_id": target_oid,
        "entry_line": entry_line,
        "guard": guard,
        "submitted": False,
        "status": "PLANNED",
    }

    # Illustrative children from a placeholder only when submit=False (tests); never used as live prices.
    if not submit:
        demo = build_bracket_child_oifs(
            direction=direction_u,
            entry_fill=9000.0,
            oco_id=oco_id,
            stop_order_id=stop_oid,
            target_order_id=target_oid,
            distance=distance,
        )
        planned["example_children_from_9000"] = demo
        planned["status"] = "DRY_RUN"
        return planned

    # --- live submit path ---
    save_active_state(
        {
            "status": "ENTRY_SUBMITTED",
            "test_id": test_id,
            "direction": direction_u,
            "oco_id": oco_id,
            "entry_order_id": entry_oid,
            "stop_order_id": stop_oid,
            "target_order_id": target_oid,
            "started_at": _utc_iso(),
        },
        path=active_path,
    )

    entry_drop = drop_oif(entry_line, nt_root=nt_root)
    entry_wait = wait_for_oif_consumed(entry_drop["path"], timeout_sec=8.0)
    fill = wait_for_entry_fill(entry_oid, nt_root=nt_root, timeout_sec=fill_timeout_sec)
    if not fill.get("ok"):
        flat = flatten_sim(nt_root=nt_root, submit=True, active_path=active_path)
        rec = {
            "test_id": test_id,
            "timestamp": _utc_iso(),
            "account": DEFAULT_ACCOUNT,
            "instrument": DEFAULT_INSTRUMENT,
            "direction": direction_u,
            "quantity": 1,
            "entry_order_id": entry_oid,
            "status": "ENTRY_FILL_TIMEOUT_FLATTENED",
            "flatten": flat.get("status"),
            "oco_id": oco_id,
        }
        append_bracket_journal(rec, path=journal_path)
        return {
            **planned,
            "ok": False,
            "submitted": True,
            "entry_drop": entry_drop,
            "entry_wait": entry_wait,
            "fill": fill,
            "flatten": flat,
            "status": "ENTRY_FILL_TIMEOUT_FLATTENED",
        }

    entry_fill = float(fill["fill_price"])
    nt_entry_oid = fill.get("nt_order_id") or entry_oid
    children = build_bracket_child_oifs(
        direction=direction_u,
        entry_fill=entry_fill,
        oco_id=oco_id,
        stop_order_id=stop_oid,
        target_order_id=target_oid,
        distance=distance,
    )
    save_active_state(
        {
            "status": "CHILDREN_PENDING",
            "test_id": test_id,
            "direction": direction_u,
            "oco_id": oco_id,
            "entry_order_id": entry_oid,
            "nt_entry_order_id": nt_entry_oid,
            "entry_fill": entry_fill,
            "stop_price": children["prices"]["stop"],
            "target_price": children["prices"]["target"],
            "stop_order_id": stop_oid,
            "target_order_id": target_oid,
            "started_at": _utc_iso(),
        },
        path=active_path,
    )

    child_drop = drop_oif_lines(
        [children["stop_line"], children["target_line"]],
        nt_root=nt_root,
    )
    child_wait = wait_for_oif_consumed(child_drop["path"], timeout_sec=8.0)
    if not child_wait.get("consumed"):
        flat = flatten_sim(nt_root=nt_root, submit=True, active_path=active_path)
        status = (
            "PROTECTION_FAILURE_FLATTENED"
            if flat.get("status") == "FLATTENED"
            else "MANUAL_FLATTEN_REQUIRED"
        )
        rec = {
            "test_id": test_id,
            "timestamp": _utc_iso(),
            "account": DEFAULT_ACCOUNT,
            "instrument": DEFAULT_INSTRUMENT,
            "direction": direction_u,
            "quantity": 1,
            "entry_order_id": entry_oid,
            "entry_fill": entry_fill,
            "stop_price": children["prices"]["stop"],
            "target_price": children["prices"]["target"],
            "oco_id": oco_id,
            "status": status,
            "final_position": (flat.get("position_after") or {}).get("market_position"),
            "orphan_orders_remaining": (flat.get("orphans") or {}).get("orphan_count"),
        }
        append_bracket_journal(rec, path=journal_path)
        return {
            **planned,
            "ok": False,
            "submitted": True,
            "entry_fill": entry_fill,
            "children": children,
            "child_drop": child_drop,
            "child_wait": child_wait,
            "flatten": flat,
            "status": status,
        }

    # Confirm children accepted in log (best effort) — stop may stay Accepted
    time.sleep(1.0)
    orphans = detect_orphan_aitrade_orders(
        [stop_oid, target_oid],
        nt_root=nt_root,
        oco_id=oco_id,
    )
    armed = orphans.get("oco_live_count", 0) >= 1 or orphans["orphan_count"] >= 1
    if not armed:
        flat = flatten_sim(nt_root=nt_root, submit=True, active_path=active_path)
        status = (
            "PROTECTION_FAILURE_FLATTENED"
            if flat.get("status") == "FLATTENED"
            else "MANUAL_FLATTEN_REQUIRED"
        )
        append_bracket_journal(
            {
                "test_id": test_id,
                "timestamp": _utc_iso(),
                "account": DEFAULT_ACCOUNT,
                "instrument": DEFAULT_INSTRUMENT,
                "direction": direction_u,
                "quantity": 1,
                "entry_order_id": entry_oid,
                "entry_fill": entry_fill,
                "stop_price": children["prices"]["stop"],
                "target_price": children["prices"]["target"],
                "stop_order_id": stop_oid,
                "target_order_id": target_oid,
                "oco_id": oco_id,
                "status": status,
            },
            path=journal_path,
        )
        return {
            **planned,
            "ok": False,
            "submitted": True,
            "entry_fill": entry_fill,
            "children": children,
            "status": status,
            "flatten": flat,
        }

    user_instructions = _manual_sim_instructions(direction_u, children["prices"])
    active_rec = {
        "status": "AWAITING_MANUAL_SIM_MOVE",
        "test_id": test_id,
        "direction": direction_u,
        "oco_id": oco_id,
        "entry_order_id": entry_oid,
        "entry_fill": entry_fill,
        "stop_price": children["prices"]["stop"],
        "target_price": children["prices"]["target"],
        "stop_order_id": stop_oid,
        "target_order_id": target_oid,
        "started_at": _utc_iso(),
    }
    save_active_state(active_rec, path=active_path)
    journal_rec = {
        **active_rec,
        "timestamp": _utc_iso(),
        "account": DEFAULT_ACCOUNT,
        "instrument": DEFAULT_INSTRUMENT,
        "quantity": 1,
        "final_exit_type": None,
        "exit_price": None,
        "final_position": "Long" if direction_u == "LONG" else "Short",
        "orphan_orders_remaining": orphans["orphan_count"],
        "status": "BRACKET_ARMED",
        "mechanism": BRACKET_MECHANISM,
    }
    append_bracket_journal(journal_rec, path=journal_path)

    return {
        **planned,
        "ok": True,
        "submitted": True,
        "entry_drop": entry_drop,
        "entry_wait": entry_wait,
        "entry_fill": entry_fill,
        "stop_price": children["prices"]["stop"],
        "target_price": children["prices"]["target"],
        "children": children,
        "child_drop": child_drop,
        "child_wait": child_wait,
        "working_exits": orphans,
        "status": "BRACKET_ARMED",
        "user_instructions": user_instructions,
    }


def _manual_sim_instructions(direction: str, prices: dict[str, float]) -> dict[str, Any]:
    if direction == "LONG":
        return {
            "recommended_first_test": "TARGET",
            "to_hit_target": (
                f"In NinjaTrader Simulated Data Feed, push price UP through {prices['target']} "
                f"(entry {prices['entry']} + 5)."
            ),
            "to_hit_stop": (
                f"Push price DOWN through {prices['stop']} (entry {prices['entry']} - 5)."
            ),
            "expect_on_target": [
                "Target LIMIT fills",
                "Stop cancels via OCO",
                "Position Flat",
                "No working AITRADE exit orders",
                "Record TARGET_OCO_PASS",
            ],
            "expect_on_stop": [
                "Stop STOPMARKET fills",
                "Target cancels via OCO",
                "Position Flat",
                "No working AITRADE exit orders",
                "Record STOP_OCO_PASS",
            ],
            "verify_immediately_after_entry": [
                f"Long 1 MNQ @ ~{prices['entry']}",
                f"Working SELL STOPMARKET @ {prices['stop']}",
                f"Working SELL LIMIT @ {prices['target']}",
                "Both exits share the same OCO id",
            ],
        }
    return {
        "recommended_first_test": "TARGET",
        "to_hit_target": (
            f"Push Simulated Data Feed price DOWN through {prices['target']} "
            f"(entry {prices['entry']} - 5)."
        ),
        "to_hit_stop": (
            f"Push price UP through {prices['stop']} (entry {prices['entry']} + 5)."
        ),
        "expect_on_target": [
            "Target LIMIT fills",
            "Stop cancels via OCO",
            "Position Flat",
            "Record TARGET_OCO_PASS",
        ],
        "expect_on_stop": [
            "Stop fills",
            "Target cancels",
            "Position Flat",
            "Record STOP_OCO_PASS",
        ],
        "verify_immediately_after_entry": [
            f"Short 1 MNQ @ ~{prices['entry']}",
            f"Working BUY STOPMARKET @ {prices['stop']}",
            f"Working BUY LIMIT @ {prices['target']}",
            "Shared OCO id",
        ],
    }


def finalize_bracket_observation(
    *,
    expected_exit: str,
    nt_root: Optional[Path] = None,
    active_path: Path = ACTIVE_STATE_PATH,
    journal_path: Path = BRACKET_JOURNAL,
) -> dict[str, Any]:
    """Post-manual helper: classify TARGET_OCO_PASS / STOP_OCO_PASS / OCO_FAILURE.

    Requires evidence of the expected child fill — Flat alone is not enough
    (Chart Trader / FLATTEN_SIM must not count as TARGET_OCO_PASS).
    """
    active = load_active_state(active_path)
    if not active:
        return {"ok": False, "error": "NO_ACTIVE_BRACKET"}
    pos = parse_mnq_sim_position(nt_root=nt_root)
    stop_ati = active.get("stop_order_id")
    tgt_ati = active.get("target_order_id")
    oids = [stop_ati, tgt_ati, active.get("entry_order_id")]
    orphans = detect_orphan_aitrade_orders(
        [x for x in oids if x],
        nt_root=nt_root,
        oco_id=active.get("oco_id"),
    )
    lines = read_log_tail(nt_root=nt_root, n=1200)
    stop_nt = orphans.get("mapped_nt_ids", {}).get(stop_ati or "")
    tgt_nt = orphans.get("mapped_nt_ids", {}).get(tgt_ati or "")

    def _filled(nt_oid: Optional[str]) -> Optional[dict[str, Any]]:
        if not nt_oid:
            return None
        return parse_fill_for_order_id(nt_oid, lines=lines, nt_root=nt_root)

    def _last_state(nt_oid: Optional[str]) -> Optional[str]:
        if not nt_oid:
            return None
        last = None
        for line in lines:
            if f"Order='{nt_oid}" in line and "New state=" in line:
                sm = re.search(r"New state='([^']+)'", line)
                if sm:
                    last = sm.group(1)
        return last

    tgt_fill = _filled(tgt_nt)
    stop_fill = _filled(stop_nt)
    tgt_state = _last_state(tgt_nt)
    stop_state = _last_state(stop_nt)

    flat = bool(pos.get("flat"))
    expected = expected_exit.upper()
    exit_price = None
    final_exit_type = None

    if expected == "TARGET":
        if flat and tgt_fill and orphans["orphan_count"] == 0 and stop_state in ("Cancelled", "Cancel pending", None):
            status = "TARGET_OCO_PASS"
            final_exit_type = "TARGET"
            exit_price = tgt_fill.get("fill_price")
        elif flat and not tgt_fill:
            status = "FLAT_BUT_NOT_TARGET_EXIT"
        elif flat and orphans["orphan_count"] > 0:
            status = "OCO_FAILURE"
        else:
            status = "STILL_OPEN"
    elif expected == "STOP":
        if flat and stop_fill and orphans["orphan_count"] == 0 and tgt_state in ("Cancelled", "Cancel pending", None):
            status = "STOP_OCO_PASS"
            final_exit_type = "STOP"
            exit_price = stop_fill.get("fill_price")
        elif flat and not stop_fill:
            status = "FLAT_BUT_NOT_STOP_EXIT"
        elif flat and orphans["orphan_count"] > 0:
            status = "OCO_FAILURE"
        else:
            status = "STILL_OPEN"
    else:
        status = "UNKNOWN_EXPECTED_EXIT"

    rec = {
        "test_id": active.get("test_id"),
        "timestamp": _utc_iso(),
        "account": DEFAULT_ACCOUNT,
        "instrument": DEFAULT_INSTRUMENT,
        "direction": active.get("direction"),
        "quantity": 1,
        "entry_order_id": active.get("entry_order_id"),
        "entry_fill": active.get("entry_fill"),
        "stop_price": active.get("stop_price"),
        "target_price": active.get("target_price"),
        "stop_order_id": stop_ati,
        "target_order_id": tgt_ati,
        "oco_id": active.get("oco_id"),
        "final_exit_type": final_exit_type,
        "exit_price": exit_price,
        "final_position": pos.get("market_position"),
        "orphan_orders_remaining": orphans["orphan_count"],
        "stop_last_state": stop_state,
        "target_last_state": tgt_state,
        "status": status,
    }
    append_bracket_journal(rec, path=journal_path)
    if status in ("TARGET_OCO_PASS", "STOP_OCO_PASS"):
        save_active_state(None, path=active_path)
    elif status in ("FLAT_BUT_NOT_TARGET_EXIT", "FLAT_BUT_NOT_STOP_EXIT", "OCO_FAILURE"):
        # clear active so next bracket can run; record remains in journal
        save_active_state(None, path=active_path)
    return {
        "ok": status.endswith("PASS"),
        "result": rec,
        "position": pos,
        "orphans": orphans,
    }


def _cli(argv: list[str]) -> int:
    import sys

    if len(argv) < 2:
        print(
            "Usage: python nt_ati.py BUY|SELL|TEST_BRACKET_LONG|TEST_BRACKET_SHORT|"
            "FLATTEN_SIM|STATUS|FINALIZE_TARGET|FINALIZE_STOP [--enable-ati]",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1].upper()
    if "--enable-ati" in argv:
        print(json.dumps(set_ati_enabled(True), indent=2))

    if cmd in ("BUY", "SELL"):
        out = place_sim_mnq_test(action=cmd)
        print(json.dumps(out, indent=2))
        wait = wait_for_oif_consumed(out["path"], timeout_sec=6.0)
        print(json.dumps({"wait": wait}, indent=2))
        hits = tail_log_matches(["ATI", "oif", "MNQ SEP26", "AITRADE", "Sim101"], n=40)
        print(json.dumps({"log_hits": hits[-10:]}, indent=2))
        return 0

    if cmd == "TEST_BRACKET_LONG":
        out = run_bracket_test("LONG", submit=True)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1

    if cmd == "TEST_BRACKET_SHORT":
        out = run_bracket_test("SHORT", submit=True)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1

    if cmd == "FLATTEN_SIM":
        out = flatten_sim(submit=True)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok") else 1

    if cmd == "STATUS":
        active = load_active_state()
        pos = parse_mnq_sim_position()
        oids = []
        if active:
            oids = [active.get("entry_order_id"), active.get("stop_order_id"), active.get("target_order_id")]
        orphans = (
            detect_orphan_aitrade_orders(
                [x for x in oids if x],
                oco_id=(active or {}).get("oco_id"),
            )
            if oids
            else {"orphan_count": 0}
        )
        print(
            json.dumps(
                {"mode": "SIM_TEST_ONLY", "active": active, "position": pos, "orphans": orphans},
                indent=2,
                default=str,
            )
        )
        return 0

    if cmd == "FINALIZE_TARGET":
        print(json.dumps(finalize_bracket_observation(expected_exit="TARGET"), indent=2, default=str))
        return 0

    if cmd == "FINALIZE_STOP":
        print(json.dumps(finalize_bracket_observation(expected_exit="STOP"), indent=2, default=str))
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv))
