"""Canonical futures instrument identity — map representations at adapter boundaries.

NinjaTrader ATI/OIF uses month-name contracts (``MNQ SEP26``).
Ops/UI dumps often use numeric month (``MNQ 09-26``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MONTH_ABBR = {
    1: "JAN",
    2: "FEB",
    3: "MAR",
    4: "APR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AUG",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DEC",
}
ABBR_TO_MONTH = {v: k for k, v in MONTH_ABBR.items()}

ALLOWED_EXECUTION_ROOTS = frozenset({"MNQ"})


class InstrumentError(ValueError):
    """Unrecognized or disallowed execution instrument."""


@dataclass(frozen=True)
class FuturesInstrument:
    root: str
    expiry_month: int
    expiry_year: int

    def __post_init__(self) -> None:
        root = (self.root or "").strip().upper()
        if root not in ALLOWED_EXECUTION_ROOTS:
            raise InstrumentError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{self.root}")
        if int(self.expiry_month) not in MONTH_ABBR:
            raise InstrumentError(f"REFUSED_UNSUPPORTED_INSTRUMENT:month={self.expiry_month}")
        year = int(self.expiry_year)
        if year < 100:
            year = 2000 + year
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "expiry_month", int(self.expiry_month))
        object.__setattr__(self, "expiry_year", year)

    def ninjatrader_oif(self) -> str:
        """NinjaTrader ATI PLACE instrument, e.g. MNQ SEP26."""
        yy = str(self.expiry_year)[-2:]
        return f"{self.root} {MONTH_ABBR[self.expiry_month]}{yy}"

    def display(self) -> str:
        """Ops/UI form, e.g. MNQ 09-26."""
        yy = str(self.expiry_year)[-2:]
        return f"{self.root} {self.expiry_month:02d}-{yy}"


# Phase 31/55A locked front month (do not silently roll).
MNQ_SEP26 = FuturesInstrument(root="MNQ", expiry_month=9, expiry_year=2026)
EXEC_INSTRUMENT_NT = MNQ_SEP26.ninjatrader_oif()
EXEC_INSTRUMENT_DISPLAY = MNQ_SEP26.display()


def parse_execution_instrument(raw: Optional[str]) -> FuturesInstrument:
    """Accept NT or display forms; reject full-size NQ and unknown roots."""
    s = (raw or "").strip().upper().replace("_", " ")
    if not s:
        raise InstrumentError("REFUSED_UNSUPPORTED_INSTRUMENT:empty")
    if s.startswith("NQ") and not s.startswith("MNQ"):
        raise PermissionError(f"REFUSED_FULL_SIZE_NQ:{raw}")

    parts = s.replace("/", " ").split()
    if len(parts) == 1 and len(parts[0]) >= 5:
        # MNQSEP26
        root, rest = parts[0][:3], parts[0][3:]
        parts = [root, rest]

    if len(parts) < 2:
        raise InstrumentError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{raw}")

    root = parts[0]
    token = "".join(parts[1:])
    token = token.replace("-", "")

    if len(token) == 4 and token[:2].isdigit() and token[2:].isdigit():
        month = int(token[:2])
        year = 2000 + int(token[2:])
        inst = FuturesInstrument(root=root, expiry_month=month, expiry_year=year)
    elif len(token) >= 5 and token[:3] in ABBR_TO_MONTH and token[3:].isdigit():
        inst = FuturesInstrument(
            root=root,
            expiry_month=ABBR_TO_MONTH[token[:3]],
            expiry_year=2000 + int(token[3:]),
        )
    else:
        raise InstrumentError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{raw}")

    if inst != MNQ_SEP26:
        raise InstrumentError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{raw}")
    return inst


def ninjatrader_oif_symbol(raw: Optional[str]) -> str:
    return parse_execution_instrument(raw).ninjatrader_oif()
