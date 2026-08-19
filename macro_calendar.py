"""Official BLS CPI / Employment Situation publication calendar (research-only).

Timestamps are America/New_York, DST-aware, default 08:30 ET unless an embargo
line in the archived release states otherwise.

Consensus (survey) is NOT available in this repository: OpenBB calendar
providers require paid credentials that are not configured. Surprise fields are
therefore recorded as unavailable rather than inferred from revised FRED series.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from nq_post_news_models import MacroEvent

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "macro"
EMPSIT_HREFS = DATA_DIR / "empsit_index_hrefs.json"
CPI_HREFS = DATA_DIR / "cpi_index_hrefs.json"
EVENTS_PATH = DATA_DIR / "bls_events.jsonl"
AUDIT_PATH = DATA_DIR / "macro_data_audit.json"

BLS_BASE = "https://www.bls.gov"
UA = {
    "User-Agent": "AITRADE-research/1.0 (local event-study; no commercial redistribution)",
    "Accept": "text/html",
}
CTX = ssl.create_default_context()

_HREF_RE = {
    "CPI": re.compile(r"/news\.release/archives/cpi_(\d{8})\.htm", re.I),
    "NFP": re.compile(r"/news\.release/archives/empsit_(\d{8})\.htm", re.I),
}

_EMBARGO_RE = re.compile(
    r"embargoed until\s+8:30\s+a\.m\.\s*\((?:ET|EDT|EST)\)[^\n]{0,80}",
    re.I,
)
_EMBARGO_TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*a\.m\.\s*\((?:ET|EDT|EST)\)", re.I)

_CPI_MOM_RE = re.compile(
    r"Consumer Price Index for All Urban Consumers \(CPI-U\)\s+"
    r"(increased|decreased|rose|fell|declined|was unchanged)(?:\s+by)?\s*"
    r"(?:(\d+\.\d+)\s+percent)?",
    re.I,
)
_CORE_MOM_RE = re.compile(
    r"index for all items less food and energy\s+"
    r"(increased|decreased|rose|fell|declined|was unchanged)(?:\s+by)?\s*"
    r"(?:(\d+\.\d+)\s+percent)?",
    re.I,
)
_CPI_YOY_RE = re.compile(
    r"Over the last 12 months,\s+the all items index\s+"
    r"(increased|decreased|rose|fell|declined)(?:\s+by)?\s+(\d+\.\d+)\s+percent",
    re.I,
)
_NFP_RE_A = re.compile(
    r"Total nonfarm payroll employment\s+"
    r"(increased|decreased|declined|rose|fell|was unchanged)(?:\s+by)?\s*"
    r"(?:([\d.]+)\s*(million))?(?:([\d,]+))?",
    re.I,
)
_NFP_RE_B = re.compile(
    r"nonfarm payroll employment\s*\(([-+]?[\d,]+)\)",
    re.I,
)
_UNRATE_RE = re.compile(
    r"unemployment rate(?: was unchanged at| was| remained at|, at)?\s*"
    r"(\d+\.\d+)\s+percent",
    re.I,
)
_AHE_CENTS_RE = re.compile(
    r"average hourly earnings for all employees on private nonfarm payrolls[^\.]{0,80}"
    r"(increased|decreased|rose|fell|were little changed|unchanged)"
    r"[^\.]{0,40}?(?:\(?([+-]?\d+)\s+cents\)?)?",
    re.I,
)
_REF_PERIOD_RE = re.compile(
    r"(?:THE EMPLOYMENT SITUATION|CONSUMER PRICE INDEX)\s*[--–-]\s*([A-Z]+ \d{4})",
    re.I,
)


def _signed_pct(verb: Optional[str], mag: Optional[str]) -> Optional[float]:
    if verb is None:
        return None
    v = verb.lower()
    if "unchanged" in v:
        return 0.0
    if mag is None:
        return None
    x = float(mag)
    if any(w in v for w in ("decreas", "fell", "declin")):
        return -x
    return x


def _parse_nfp_count(text: str) -> Optional[float]:
    m = _NFP_RE_A.search(text)
    if m:
        verb, million_val, million_unit, raw = m.group(1), m.group(2), m.group(3), m.group(4)
        if "unchanged" in verb.lower():
            return 0.0
        sign = -1.0 if any(w in verb.lower() for w in ("decreas", "fell", "declin")) else 1.0
        if million_val and million_unit:
            return sign * float(million_val) * 1_000_000.0
        if raw:
            return sign * float(raw.replace(",", ""))
    m2 = _NFP_RE_B.search(text)
    if m2:
        return float(m2.group(1).replace(",", ""))
    return None


def parse_first_print(html: str, family: str) -> dict[str, Any]:
    """Extract first-print headlines from an archived BLS HTML release."""
    # Drop nav chrome by focusing on the news body when possible.
    body = html
    low = html.lower()
    start = low.find("the employment situation")
    if start < 0:
        start = low.find("consumer price index")
    if start >= 0:
        body = html[start : start + 12000]
    out: dict[str, Any] = {
        "embargo_line": None,
        "release_local_parsed": None,
        "reference_period": None,
        "actuals": {},
        "parse_notes": [],
    }
    em = _EMBARGO_RE.search(html) or _EMBARGO_TIME_RE.search(html)
    if em:
        out["embargo_line"] = re.sub(r"\s+", " ", em.group(0)).strip()
        tm = _EMBARGO_TIME_RE.search(out["embargo_line"])
        if tm:
            out["release_local_parsed"] = tm.group(1)
            if tm.group(1) != "8:30":
                out["parse_notes"].append(f"non_830_embargo:{tm.group(1)}")
    rp = _REF_PERIOD_RE.search(html)
    if rp:
        out["reference_period"] = rp.group(1).title()
    if family == "CPI":
        mom = _CPI_MOM_RE.search(body)
        core = _CORE_MOM_RE.search(body)
        yoy = _CPI_YOY_RE.search(body)
        out["actuals"]["cpi_u_mom_pct"] = None if not mom else _signed_pct(mom.group(1), mom.group(2))
        out["actuals"]["core_cpi_mom_pct"] = None if not core else _signed_pct(core.group(1), core.group(2))
        out["actuals"]["cpi_u_yoy_pct"] = None if not yoy else _signed_pct(yoy.group(1), yoy.group(2))
        if out["actuals"]["cpi_u_mom_pct"] is None:
            out["parse_notes"].append("cpi_mom_unparsed")
    else:
        out["actuals"]["nfp_change"] = _parse_nfp_count(body)
        um = _UNRATE_RE.search(body)
        out["actuals"]["unemployment_rate_pct"] = None if not um else float(um.group(1))
        ahe = _AHE_CENTS_RE.search(body)
        if ahe:
            cents = ahe.group(2)
            verb = ahe.group(1).lower()
            if "unchanged" in verb or "little changed" in verb:
                out["actuals"]["ahe_cents"] = 0.0 if cents is None else float(cents)
            elif cents is not None:
                sign = -1.0 if any(w in verb for w in ("decreas", "fell")) else 1.0
                out["actuals"]["ahe_cents"] = sign * abs(float(cents))
            else:
                out["actuals"]["ahe_cents"] = None
        else:
            out["actuals"]["ahe_cents"] = None
        if out["actuals"]["nfp_change"] is None:
            out["parse_notes"].append("nfp_unparsed")
    return out


def _hrefs(path: Path, family: str) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    pat = _HREF_RE[family]
    for h in rows:
        m = pat.search(str(h))
        if not m:
            continue
        ymd = m.group(1)
        mm, dd, yyyy = int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])
        try:
            d = date(yyyy, mm, dd).isoformat()
        except ValueError:
            continue
        if d in seen:
            continue
        seen.add(d)
        rel = m.group(0) if m.group(0).startswith("/") else "/" + m.group(0)
        out.append((d, BLS_BASE + rel))
    out.sort()
    return out


def ny_release_ts(publication_date: str, hhmm: str = "08:30") -> int:
    d = date.fromisoformat(publication_date)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-160000"})
    with urllib.request.urlopen(req, context=CTX, timeout=8) as r:
        return r.read().decode("latin-1", errors="replace")


def build_events(
    *,
    start: str = "2020-01-01",
    end: str = "2026-08-14",
    fetch_prints: bool = False,
) -> list[MacroEvent]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    events: list[MacroEvent] = []
    for family, path in (("CPI", CPI_HREFS), ("NFP", EMPSIT_HREFS)):
        for pub, url in _hrefs(path, family):
            if pub < start or pub > end:
                continue
            actuals: dict[str, Any] = {}
            embargo = None
            ref = None
            notes: list[str] = []
            parsed_local = "08:30"
            if fetch_prints:
                try:
                    html = _fetch(url)
                    parsed = parse_first_print(html, family)
                    actuals = parsed.get("actuals") or {}
                    embargo = parsed.get("embargo_line")
                    ref = parsed.get("reference_period")
                    notes = list(parsed.get("parse_notes") or [])
                    if parsed.get("release_local_parsed"):
                        # Normalize "8:30" -> "08:30"
                        hh, mm = parsed["release_local_parsed"].split(":")
                        parsed_local = f"{int(hh):02d}:{int(mm):02d}"
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"fetch_failed:{type(exc).__name__}")
            quality = {
                "first_print_source": "bls_archived_html_headline" if fetch_prints else "dates_only",
                "consensus_source": None,
                "consensus_available": False,
                "revision_leakage_risk": "low_for_first_print_archive; consensus_unavailable",
                "parse_notes": notes,
                "assumed_release_local_if_unparsed": "08:30",
            }
            events.append(
                MacroEvent(
                    event_id=f"{family}|{pub}|{parsed_local}",
                    event_family=family,
                    publication_date=pub,
                    release_local=parsed_local,
                    source="BLS_archived_news_release",
                    source_url=url,
                    embargo_line=embargo,
                    reference_period=ref,
                    actuals=actuals,
                    consensus={
                        "status": "UNAVAILABLE",
                        "reason": (
                            "No pre-release survey consensus in repo. OpenBB calendar "
                            "(FMP/TradingEconomics) requires credentials that are not set. "
                            "Nasdaq calendar API returns only the current session, not history. "
                            "FRED/ALFRED vintages were not fetched (no FRED_API_KEY)."
                        ),
                    },
                    surprise={
                        "raw_surprise": None,
                        "standardized_surprise": None,
                        "status": "UNAVAILABLE_NO_CONSENSUS",
                    },
                    data_quality=quality,
                )
            )
    events.sort(key=lambda e: (e.publication_date, e.event_family))
    return events


def write_events(events: list[MacroEvent]) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.to_dict(), default=str) + "\n")
    return str(EVENTS_PATH)


def load_events() -> list[MacroEvent]:
    if not EVENTS_PATH.exists():
        return []
    out: list[MacroEvent] = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out.append(MacroEvent(**row))
    return out


def ensure_events(*, fetch_prints: bool = False) -> list[MacroEvent]:
    if EVENTS_PATH.exists() and EVENTS_PATH.stat().st_size > 0:
        return load_events()
    events = build_events(fetch_prints=fetch_prints)
    write_events(events)
    return events


def write_audit(events: list[MacroEvent]) -> dict[str, Any]:
    cpi = [e for e in events if e.event_family == "CPI"]
    nfp = [e for e in events if e.event_family == "NFP"]

    def _cov(rows: list[MacroEvent], key: str) -> dict[str, Any]:
        vals = [e.actuals.get(key) for e in rows]
        parsed = sum(1 for v in vals if v is not None)
        return {"n": len(rows), "parsed": parsed, "parse_rate": None if not rows else parsed / len(rows)}

    audit = {
        "source": "BLS archived news releases (HTML)",
        "timezone": "America/New_York",
        "dst_aware": True,
        "default_release_local": "08:30",
        "coverage": {
            "start": None if not events else events[0].publication_date,
            "end": None if not events else events[-1].publication_date,
            "cpi_n": len(cpi),
            "nfp_n": len(nfp),
        },
        "first_print_parse": {
            "cpi_u_mom_pct": _cov(cpi, "cpi_u_mom_pct"),
            "core_cpi_mom_pct": _cov(cpi, "core_cpi_mom_pct"),
            "nfp_change": _cov(nfp, "nfp_change"),
            "unemployment_rate_pct": _cov(nfp, "unemployment_rate_pct"),
            "ahe_cents": _cov(nfp, "ahe_cents"),
        },
        "consensus": {
            "available": False,
            "openbb_calendar": "credentials_missing_fmp_tradingeconomics_fred",
            "nasdaq_api": "current_session_only",
            "implication": "Cannot compute raw_surprise = actual - consensus without leakage-safe survey vintages",
        },
        "known_limitations": [
            "October 2025 Employment Situation and CPI were not published on the normal schedule due to the 2025 federal appropriations lapse; delayed/missing prints are whatever BLS archived.",
            "Headline parsers can fail on atypical wording (especially COVID-era NFP). Unparsed actuals are null, not imputed.",
            "Archived HTML is the first public release text, but BLS later revises CES/CPI in subsequent months. We do not splice those revisions into actuals.",
            "No Treasury yield or DXY series is stored in this repository; those confirmation legs are omitted rather than substituted.",
            "No ES Databento store exists; ES is not silently replaced by a cash index or CFD.",
            "Survey consensus is unavailable; surprise-conditioned rules are not tested in this phase.",
        ],
        "events_path": str(EVENTS_PATH).replace("\\", "/"),
    }
    AUDIT_PATH.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


if __name__ == "__main__":
    import sys

    fetch = "--prints" in sys.argv
    ev = build_events(fetch_prints=fetch)
    write_events(ev)
    audit = write_audit(ev)
    print(json.dumps({"n": len(ev), "fetch_prints": fetch, "audit": audit["coverage"], "parse": audit["first_print_parse"]}, indent=2))
