"""NQ front-month calendar from AITRADE volume-crossover rolls (18:00 NY activation)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
ROLLS_PATH = ROOT / "data" / "databento" / "NQ" / "stitched" / "rolls.jsonl"


def load_rolls(path: Path = ROLLS_PATH) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def contract_on_rth_date(d: date, rolls: list[dict[str, Any]]) -> str:
    """RTH 09:30-16:00 is before the 18:00 activation, so roll-day RTH stays on the old contract."""
    iso = d.isoformat()
    active = "NQH0"
    for r in rolls:
        if iso > r["decision_date"]:
            active = r["new_contract"]
        else:
            break
    return active


def trading_dates(start: date, end: date, holidays: set[str]) -> list[str]:
    out = []
    d = start
    while d <= end:
        iso = d.isoformat()
        if d.weekday() < 5 and iso not in holidays:
            out.append(iso)
        d += timedelta(days=1)
    return out


def dates_by_contract(
    start: date,
    end: date,
    rolls: list[dict[str, Any]],
    holidays: set[str],
) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for iso in trading_dates(start, end, holidays):
        c = contract_on_rth_date(date.fromisoformat(iso), rolls)
        by.setdefault(c, []).append(iso)
    return by
