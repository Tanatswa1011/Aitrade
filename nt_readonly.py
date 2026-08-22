"""Read-only NinjaTrader state. Never writes OIF / incoming. Never submits orders.

Sim101 is detected and ignored for FundedNext evaluation fields.
PROP_EXECUTION remains false; this module has no submit functions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

UTC = timezone.utc
SIM_ACCOUNT_NAMES = {"Sim101", "Playback101", "Backtest"}
FN_NAME_RE = re.compile(r"^(FN|FNFT|FUNDEDNEXT)", re.I)

# Derived from NT AccountItem declaration order vs live Sim101 callbacks:
# type 1 updates with CashValue, type 8 with GrossRealizedProfitLoss, type 18 with RealizedProfitLoss.
ACCOUNT_ITEM = {
    0: "BuyingPower",
    1: "CashValue",
    2: "Commission",
    3: "ExcessIntradayMargin",
    4: "ExcessInitialMargin",
    5: "ExcessMaintenanceMargin",
    6: "ExcessPositionMargin",
    7: "Fee",
    8: "GrossRealizedProfitLoss",
    9: "InitialMargin",
    10: "IntradayMargin",
    11: "LongOptionValue",
    12: "LookAheadMaintenanceMargin",
    13: "LongStockValue",
    14: "MaintenanceMargin",
    15: "NetLiquidation",
    16: "NetLiquidationByCurrency",
    17: "PositionMargin",
    18: "RealizedProfitLoss",
    19: "ShortOptionValue",
    20: "ShortStockValue",
    21: "SodCashValue",
    22: "SodLiquidatingValue",
    23: "UnrealizedProfitLoss",
    24: "TotalCashBalance",
}

_POS_RE = re.compile(
    r"Instrument='(?P<instr>[^']+)' Account='(?P<acct>[^']+)' Average price=(?P<avg>[0-9.]+) "
    r"Quantity=(?P<qty>\d+) Market position=(?P<pos>Flat|Long|Short)"
)
_SIM_FEED_RE = re.compile(r"simulated data feed", re.I)
_PLAYBACK_RE = re.compile(r"playback connection|\bplayback\b", re.I)
_DELAYED_FEED_RE = re.compile(r"delayed(?: data)? feed|end of day|\beod\b", re.I)
_GLOBAL_SIM_ON_RE = re.compile(r"Global simulation mode enabled", re.I)
_GLOBAL_SIM_OFF_RE = re.compile(r"Global simulation mode disabled", re.I)
_GLOBAL_SIM_RE = _GLOBAL_SIM_ON_RE
_ARTIFICIAL_NAMES = {"simulation", "simulation.txt"}
_FEED_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d+)\|\d+\|\d+\|"
    r"(?P<name>.*?): Primary connection=(?P<conn>Connected|Disconnected|Connecting|Disconnecting), "
    r"Price feed=(?P<feed>Connected|Disconnected|Connecting|Disconnecting)"
)
_ACCT_STATUS_RE = re.compile(
    r"account='(?P<acct>[^']+)'[^\n]* status=(?P<status>Connected|Disconnected|Connecting|Disconnecting)"
)
_ACCT_ITEM_RE = re.compile(
    r"account='(?P<acct>[^']+)' accountItem=(?P<item>\w+) currency=\w+ value=(?P<val>\S+)"
)
_LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(?P<ms>\d+)")
_CREATE_ACCT_RE = re.compile(r"CreateAccount: account='(?P<acct>[^']+)'")
ADDON_HEARTBEAT_SEC = 5.0

# NT OrderState: 1 appears as unfilled working; 2 filled; 9 rejected.
WORKING_ORDER_STATES = {1, 3, 4, 5}
QUARTER_MONTHS = (3, 6, 9, 12)
PROP_EXECUTION = False


def _is_simulator_backend(name: str, provider_type: str) -> bool:
    nl = (name or "").strip().lower()
    tl = (provider_type or "").strip().lower()
    if _SIM_FEED_RE.search(name or ""):
        return True
    if "simulatoroptions" in tl or tl == "simulator":
        return True
    if tl in ("artificial", "simulated"):
        return True
    return False


def _is_tradovate_backend(name: str, provider_type: str) -> bool:
    nl = (name or "").strip().lower()
    tl = (provider_type or "").strip().lower()
    if _is_simulator_backend(name, provider_type):
        return False
    if "tradovate" in tl or tl in ("tradovate", "tradovateoptions", "continuum"):
        return True
    if "continuum" in nl:
        return True
    if "ninjatrader" in nl and "simulated" not in nl:
        return True
    return False


def classify_market_provider(name: str, provider_type: str | None = None) -> dict[str, Any]:
    """Classify by backend identity. Display name Simulation is not Simulated Data Feed."""
    n = (name or "").strip()
    t = (provider_type or "").strip()
    nl = n.lower()
    tl = t.lower()
    blob = ("%s %s" % (n, t)).lower()
    if not n and not t:
        return {"kind": "UNKNOWN", "provider_kind": "UNKNOWN", "market_data": "UNKNOWN"}
    if _is_simulator_backend(n, t):
        return {"kind": "SIMULATED", "provider_kind": "ARTIFICIAL", "market_data": "SIMULATED"}
    if _PLAYBACK_RE.search(n) or "playback" in tl:
        return {"kind": "PLAYBACK", "provider_kind": "PLAYBACK", "market_data": "SIMULATED"}
    if _DELAYED_FEED_RE.search(n) or tl in ("delayed", "eod") or ("kinetick" in blob and ("end of day" in blob or "eod" in blob)):
        return {"kind": "DELAYED", "provider_kind": "DELAYED", "market_data": "DELAYED"}
    if _is_tradovate_backend(n, t):
        return {"kind": "LIVE", "provider_kind": "TRADOVATE", "market_data": "UNKNOWN"}
    if nl in _ARTIFICIAL_NAMES:
        return {"kind": "SIMULATED", "provider_kind": "ARTIFICIAL", "market_data": "SIMULATED"}
    if "cqg" in blob:
        return {"kind": "LIVE", "provider_kind": "CQG", "market_data": "UNKNOWN"}
    if "rithmic" in blob:
        return {"kind": "LIVE", "provider_kind": "RITHMIC", "market_data": "UNKNOWN"}
    if "kinetick" in blob:
        return {"kind": "LIVE", "provider_kind": "KINETICK", "market_data": "UNKNOWN"}
    if tl in ("tradovate", "cqg", "rithmic", "kinetick"):
        return {"kind": "LIVE", "provider_kind": tl.upper(), "market_data": "UNKNOWN"}
    return {"kind": "LIVE", "provider_kind": "OTHER", "market_data": "UNKNOWN"}


def _provider_kind(name: str) -> str:
    return classify_market_provider(name)["kind"]


def _provider_record(
    name: str,
    connected: bool,
    kind: str | None = None,
    status: str | None = None,
    provider_kind: str | None = None,
    provider_type: str | None = None,
    account_environment: str | None = None,
    provider_id: Any = None,
    provider_backend: str | None = None,
) -> dict[str, Any]:
    type_for_classify = provider_type or (
        provider_kind if str(provider_kind or "").lower() not in ("artificial", "simulated") else ""
    )
    info = classify_market_provider(name, type_for_classify)
    kind = kind or info["kind"]
    pk = info["provider_kind"]
    md = "SIMULATED" if kind in ("SIMULATED", "PLAYBACK") else ("DELAYED" if kind == "DELAYED" else ("UNKNOWN" if kind == "LIVE" else "UNKNOWN"))
    env = account_environment
    if env is None and pk == "TRADOVATE":
        env = "SIMULATION"
    backend = provider_backend or provider_type or ""
    return {
        "name": name,
        "provider": name,
        "provider_name": name,
        "provider_display_name": name,
        "provider_backend": backend or pk,
        "provider_id": provider_id,
        "status": status or ("Connected" if connected else "Disconnected"),
        "kind": kind,
        "provider_kind": pk,
        "connected": connected,
        "account_environment": env,
        "market_data": md,
        "account": (
            "SIMULATED"
            if kind == "SIMULATED"
            else ("NONE" if kind in ("DELAYED", "PLAYBACK") else ("YES" if connected and kind == "LIVE" else "UNKNOWN"))
        ),
    }


def _normalize_provider(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("provider") or raw.get("provider_name") or "")
    status = str(raw.get("status") or "")
    connected = raw.get("connected")
    if connected is None:
        connected = status.lower() == "connected"
    ptype = str(raw.get("provider_type") or raw.get("provider_backend") or "")
    rec = _provider_record(
        name,
        bool(connected),
        status=status or None,
        provider_type=ptype or None,
        provider_kind=str(raw.get("provider_kind") or "") or None,
        account_environment=raw.get("account_environment"),
        provider_id=raw.get("provider_id"),
        provider_backend=str(raw.get("provider_backend") or ptype or "") or None,
    )
    info = classify_market_provider(name, ptype or None)
    pk = str(info.get("provider_kind") or rec["provider_kind"] or "").upper()
    if pk in ("TRADOVATE", "CQG", "RITHMIC", "KINETICK"):
        rec["kind"] = "LIVE"
        rec["provider_kind"] = pk
    elif pk in ("ARTIFICIAL", "SIMULATED"):
        rec["kind"] = "SIMULATED"
        rec["provider_kind"] = "ARTIFICIAL"
        rec["market_data"] = "SIMULATED"
    elif pk == "PLAYBACK":
        rec["kind"] = "PLAYBACK"
        rec["provider_kind"] = "PLAYBACK"
        rec["market_data"] = "SIMULATED"
    elif pk in ("DELAYED", "EOD"):
        rec["kind"] = "DELAYED"
        rec["provider_kind"] = "DELAYED"
        rec["market_data"] = "DELAYED"
    if raw.get("account_environment"):
        rec["account_environment"] = str(raw.get("account_environment")).upper()
    rec["account"] = (
        "SIMULATED"
        if rec["kind"] == "SIMULATED"
        else ("NONE" if rec["kind"] in ("DELAYED", "PLAYBACK") else ("YES" if rec["connected"] and rec["kind"] == "LIVE" else "UNKNOWN"))
    )
    return rec


def parse_connection_dump(dump: Optional[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not dump or not str(dump).strip():
        return out
    for part in str(dump).split(";"):
        part = part.strip()
        if not part or part.lower().startswith("err="):
            continue
        if "=" in part:
            name, st = part.rsplit("=", 1)
        else:
            name, st = part, "Unknown"
        name = name.strip()
        st = st.strip()
        out.append(_provider_record(name, st.lower() == "connected", status=st))
    return out


def _apply_fn_simulation_identity(providers: list[dict[str, Any]], rt: Any) -> list[dict[str, Any]]:
    """Display name Simulation + FundedNext/Tradovate binding is not Simulated Data Feed."""
    if not providers or not isinstance(rt, dict):
        return providers
    if any("simulated data feed" in str(p.get("name") or "").lower() and p.get("connected") for p in providers):
        return providers
    fn = rt.get("fundednext") if isinstance(rt.get("fundednext"), dict) else {}
    backend = "%s %s" % (fn.get("connection_backend") or "", fn.get("connection_name") or "")
    acct_ok = fn.get("connected") is True and bool(FN_NAME_RE.match(str(fn.get("account_id") or "")))
    out: list[dict[str, Any]] = []
    for p in providers:
        rec = dict(p)
        name = str(rec.get("name") or "")
        ptype = str(rec.get("provider_type") or "")
        if rec.get("connected") and name.strip().lower() == "simulation" and rec.get("kind") == "SIMULATED":
            if _is_tradovate_backend(name, ptype) or "tradovate" in (ptype + " " + backend).lower() or acct_ok:
                rec["kind"] = "LIVE"
                rec["provider_kind"] = "TRADOVATE"
                rec["provider_backend"] = ptype if "tradovate" in ptype.lower() else "TradovateOptions"
                rec["provider_id"] = rec.get("provider_id") if rec.get("provider_id") not in (None, "") else 50
                rec["provider_display_name"] = name
                rec["account_environment"] = str(rec.get("account_environment") or "SIMULATION").upper()
                rec["market_data"] = "UNKNOWN"
                rec["account"] = "YES"
        out.append(rec)
    return out


def _nz_price(v: Any) -> Any:
    if v is None:
        return None
    try:
        if float(v) == 0:
            return None
    except (TypeError, ValueError):
        return None
    return v


def _extract_quote(block: Any, fallback: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    src = block if isinstance(block, dict) else {}
    fb = fallback if isinstance(fallback, dict) else {}
    last = _nz_price(src.get("last") if "last" in src else fb.get("last"))
    bid = src.get("bid") if "bid" in src else fb.get("bid")
    ask = src.get("ask") if "ask" in src else fb.get("ask")
    ts = src.get("timestamp") or src.get("last_update") or src.get("last_time") or fb.get("timestamp") or fb.get("last_update")
    epoch = _parse_iso_epoch(ts)
    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "timestamp": datetime.fromtimestamp(float(epoch), tz=UTC).isoformat() if epoch else None,
        "age_sec": max(0.0, time.time() - float(epoch)) if epoch else None,
        "instrument": src.get("instrument") or fb.get("instrument"),
        "complete": last is not None and bid is not None and ask is not None and epoch is not None,
    }


def _primary_provider(providers: list[dict[str, Any]]) -> dict[str, Any]:
    connected = [p for p in providers if p.get("connected")]
    for want in ("LIVE", "DELAYED", "PLAYBACK", "SIMULATED"):
        for p in connected:
            if p.get("kind") == want:
                return p
    if connected:
        return connected[0]
    return providers[0] if providers else {}


def classify_account_name(name: str) -> str:
    n = (name or "").strip()
    if n in SIM_ACCOUNT_NAMES or n.lower().startswith("sim"):
        return "SIM"
    if n.lower().startswith("playback"):
        return "SIM"
    if FN_NAME_RE.match(n):
        return "FUNDEDNEXT"
    return "OTHER"


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(days=14)


def resolve_nq_mnq_contracts(asof: Optional[datetime] = None) -> dict[str, Any]:
    """CME NQ/MNQ quarterly (Mar/Jun/Sep/Dec), front month until 3rd Friday expiry."""
    now = asof or datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    day = now.date()
    year, month = day.year, day.month
    candidates = []
    for y in (year, year + 1):
        for m in QUARTER_MONTHS:
            expiry = _third_friday(y, m)
            if expiry >= day:
                candidates.append((expiry, y, m))
            if len(candidates) >= 2:
                break
        if len(candidates) >= 2:
            break
    expiry, y, m = candidates[0]
    code = "%02d-%02d" % (m, y % 100)
    return {
        "nq": "NQ %s" % code,
        "mnq": "MNQ %s" % code,
        "expiry": expiry.isoformat(),
        "code": code,
        "signal_instrument": "NQ",
        "position_instrument": "MNQ",
        "mapping": "NQ_DRIFT_VWAP_PULLBACK signal on NQ → MNQ evaluation size",
        "rollover": "front quarterly until 3rd Friday expiry",
    }


def _parse_iso_epoch(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _dotnet_ticks_to_dt(ticks: Optional[int]) -> Optional[datetime]:
    if not ticks:
        return None
    try:
        return datetime(1, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(ticks) / 10)
    except Exception:
        return None


def _parse_nt_log_ts(stamp: str) -> Optional[datetime]:
    m = _LOG_TS_RE.match(stamp.strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        ms = int(m.group("ms")[:6].ljust(6, "0"))
        local = datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=local) + timedelta(microseconds=ms)
    except ValueError:
        return None


def resolve_nt_root(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        return explicit if explicit.exists() else None
    for p in (
        Path.home() / "OneDrive" / "Documents" / "NinjaTrader 8",
        Path.home() / "Documents" / "NinjaTrader 8",
    ):
        if (p / "incoming").is_dir() or (p / "db").is_dir() or (p / "log").is_dir():
            return p
    return None


class NTReadOnly:
    """Snapshot reader. Constructor never opens incoming for write."""

    def __init__(self, nt_root: Optional[Path] = None, *, sqlite_path: Optional[Path] = None):
        self.root = resolve_nt_root(nt_root)
        self.sqlite_path = sqlite_path
        if self.sqlite_path is None and self.root:
            cand = self.root / "db" / "NinjaTrader.sqlite"
            self.sqlite_path = cand if cand.exists() else None

    def incoming_dir(self) -> Optional[Path]:
        if not self.root:
            return None
        p = self.root / "incoming"
        return p if p.is_dir() else None

    def incoming_oif_files(self) -> list[str]:
        d = self.incoming_dir()
        if not d:
            return []
        return sorted(x.name for x in d.iterdir() if x.is_file() and x.name.lower().startswith("oif"))

    def _connect(self) -> Optional[sqlite3.Connection]:
        if not self.sqlite_path or not self.sqlite_path.exists():
            return None
        path = self.sqlite_path.resolve()
        uri = path.as_uri() + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=3)
        except sqlite3.Error:
            try:
                con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
            except sqlite3.Error:
                return None
        con.row_factory = sqlite3.Row
        return con

    def accounts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        con = self._connect()
        if con is not None:
            try:
                cur = con.cursor()
                for r in cur.execute(
                    "SELECT Id, Name, DisplayName, Provider, SimulatorInitialCash, "
                    "LossLimit, AccountStatus, LiquidationState FROM Accounts"
                ):
                    name = str(r["Name"] or "")
                    rows.append(
                        {
                            "id": r["Id"],
                            "name": name,
                            "display_name": r["DisplayName"] or name,
                            "provider": r["Provider"],
                            "kind": classify_account_name(name),
                            "source": "sqlite",
                        }
                    )
            except sqlite3.Error:
                rows = []
            finally:
                con.close()
        seen = {r["name"] for r in rows}
        for name, _status in self._trace_account_status().items():
            if name not in seen:
                rows.append(
                    {
                        "id": None,
                        "name": name,
                        "display_name": name,
                        "provider": None,
                        "kind": classify_account_name(name),
                        "source": "trace",
                    }
                )
        return rows

    def fundednext_account(self, expected: Optional[str] = None) -> Optional[dict[str, Any]]:
        accts = self.accounts()
        if expected and expected not in ("AUTO", "AUTO_FUNDEDNEXT", "SHADOW_FUNDEDNEXT_FLEX_50K"):
            for a in accts:
                if a["name"] == expected and a["kind"] != "SIM":
                    return a
        fn = [a for a in accts if a["kind"] == "FUNDEDNEXT"]
        if len(fn) == 1:
            return fn[0]
        if expected:
            for a in fn:
                if a["name"] == expected:
                    return a
        return fn[0] if fn else None

    def sim101_account(self) -> Optional[dict[str, Any]]:
        for a in self.accounts():
            if a["name"] == "Sim101":
                return a
        return None

    def account_items(self, account_id: Optional[int]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if account_id is None:
            return out
        con = self._connect()
        if con is None:
            return out
        try:
            for r in con.execute(
                "SELECT ItemType, Value, TimeUtc FROM AccountItems WHERE Account=? ORDER BY ItemType",
                (account_id,),
            ):
                key = ACCOUNT_ITEM.get(int(r["ItemType"]), f"Item{r['ItemType']}")
                ts = _dotnet_ticks_to_dt(r["TimeUtc"])
                out[key] = {
                    "value": float(r["Value"]) if r["Value"] is not None else None,
                    "time": ts.isoformat() if ts else None,
                    "item_type": int(r["ItemType"]),
                }
        except sqlite3.Error:
            return {}
        finally:
            con.close()
        return out

    def _trace_account_status(self) -> dict[str, str]:
        last: dict[str, str] = {}
        for line in self._iter_trace_and_log_lines():
            m = _ACCT_STATUS_RE.search(line)
            if m:
                last[m.group("acct")] = m.group("status")
            c = _CREATE_ACCT_RE.search(line)
            if c and c.group("acct") not in last:
                last[c.group("acct")] = "UNKNOWN"
        return last

    def _trace_account_items_numeric(self, account: str) -> dict[str, float]:
        """Unredacted trace values only. ***** is ignored."""
        last: dict[str, float] = {}
        for line in self._iter_trace_and_log_lines():
            m = _ACCT_ITEM_RE.search(line)
            if not m or m.group("acct") != account:
                continue
            raw = m.group("val")
            if raw.startswith("*") or raw in ("*****", "?"):
                continue
            try:
                last[m.group("item")] = float(raw.replace(",", ""))
            except ValueError:
                continue
        return last

    def _iter_trace_and_log_lines(self) -> list[str]:
        lines: list[str] = []
        if not self.root:
            return lines
        files: list[Path] = []
        for folder, glob in (("trace", "trace.*.txt"), ("log", "log.*.en.txt"), ("log", "log.*.txt")):
            d = self.root / folder
            if not d.is_dir():
                continue
            files.extend(d.glob(glob))
        seen: set[Path] = set()
        ordered = []
        for p in sorted(files, key=lambda x: x.stat().st_mtime):
            if p in seen:
                continue
            seen.add(p)
            ordered.append(p)
        for p in ordered[-8:]:
            try:
                lines.extend(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            except OSError:
                continue
        return lines

    def price_feed(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        if self.root:
            logs = list((self.root / "log").glob("log.*.en.txt"))
            if not logs:
                logs = list((self.root / "log").glob("log.*.txt"))
            for path in logs:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for line in text.splitlines():
                    m = _FEED_RE.match(line)
                    if not m:
                        continue
                    ts = _parse_nt_log_ts(line)
                    events.append(
                        {
                            "name": (m.group("name") or "").strip(),
                            "connection": m.group("conn"),
                            "price_feed": m.group("feed"),
                            "ts": ts.isoformat() if ts else None,
                            "age_sec": (datetime.now().astimezone() - ts).total_seconds() if ts else None,
                            "source_file": path.name,
                            "_ord": ts.timestamp() if ts else path.stat().st_mtime,
                        }
                    )
            # Simulation.txt is not a CME market-data provider. Ignore it for feed status.
        if not events:
            return {
                "name": None,
                "connection": "Disconnected",
                "price_feed": "Disconnected",
                "ts": None,
                "age_sec": None,
                "source_file": None,
                "ok": False,
            }
        last = max(events, key=lambda e: e.get("_ord") or 0)
        last = {k: v for k, v in last.items() if k != "_ord"}
        last["ok"] = last.get("price_feed") == "Connected" and last.get("connection") == "Connected"
        return last

    def runtime_snapshot(self) -> Optional[dict[str, Any]]:
        """Read-only JSON written by the AITRADE NT AddOn. Never an order file."""
        if not self.root:
            return None
        for p in (
            self.root / "outgoing" / "AITRADE_READONLY.json",
            self.root / "tmp" / "AITRADE_READONLY.json",
        ):
            if not p.is_file() or p.stat().st_size == 0:
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            doc["_path"] = str(p)
            doc["_mtime"] = p.stat().st_mtime
            return doc
        return None

    def global_simulation_state(self) -> dict[str, Any]:
        """Latest NT log line wins. An older 'enabled' must not override a later 'disabled'."""
        empty = {"global_simulation": None, "source": None, "ts": None}
        if not self.root:
            return empty
        log_dir = self.root / "log"
        if not log_dir.is_dir():
            return empty
        events: list[tuple[float, bool, str]] = []
        logs = sorted(log_dir.glob("log.*.txt"), key=lambda p: p.stat().st_mtime)
        for path in logs[-12:]:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                on = _GLOBAL_SIM_ON_RE.search(line)
                off = _GLOBAL_SIM_OFF_RE.search(line)
                if not on and not off:
                    continue
                ts = _parse_nt_log_ts(line)
                events.append((ts.timestamp() if ts else path.stat().st_mtime, bool(on), path.name))
        if not events:
            return empty
        ts, enabled, src = max(events, key=lambda x: x[0])
        return {
            "global_simulation": bool(enabled),
            "source": src,
            "ts": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
        }

    def connection_dump(self) -> str:
        rt = self.runtime_snapshot()
        if not isinstance(rt, dict):
            return ""
        diag = rt.get("diagnostics") if isinstance(rt.get("diagnostics"), dict) else {}
        return str(diag.get("connections") or "")

    def market_providers(self) -> list[dict[str, Any]]:
        rt = self.runtime_snapshot()
        diag = rt.get("diagnostics") if isinstance(rt, dict) else None
        providers: list[dict[str, Any]] = []
        dump_explicit = False
        if isinstance(diag, dict):
            structured = diag.get("providers")
            if isinstance(structured, list) and structured:
                providers = [_normalize_provider(p) for p in structured if isinstance(p, dict)]
                dump_explicit = True
            elif "connections" in diag:
                providers = parse_connection_dump(diag.get("connections"))
                dump_explicit = True
        if not dump_explicit and not providers:
            parsed = parse_connection_dump(self.connection_dump())
            if parsed:
                providers = parsed
        if not dump_explicit and not providers and isinstance(rt, dict):
            mkt = rt.get("market_data") if isinstance(rt.get("market_data"), dict) else {}
            conn = rt.get("connection") if isinstance(rt.get("connection"), dict) else {}
            q = str(mkt.get("quality") or rt.get("market_data_quality") or conn.get("quality") or "").upper()
            if q == "SIMULATED":
                providers = [_provider_record("Simulated Data Feed", True, "SIMULATED")]
            elif q in ("DELAYED", "EOD"):
                providers = [_provider_record("Kinetick – End Of Day (Free)", True, "DELAYED")]
            elif q == "PLAYBACK":
                providers = [_provider_record("Playback Connection", True, "PLAYBACK")]
        if not dump_explicit and not providers:
            feed = self.price_feed()
            name = str(feed.get("name") or "").strip()
            if name:
                providers = [_provider_record(name, bool(feed.get("ok")), status=str(feed.get("connection") or "Disconnected"))]
        return _apply_fn_simulation_identity(providers, rt)

    def feed_quality(self) -> str:
        providers = self.market_providers()
        connected = [p for p in providers if p.get("connected")]
        live = any(p.get("kind") == "LIVE" for p in connected)
        if live:
            return "UNKNOWN"
        if any(p.get("kind") == "DELAYED" for p in connected):
            return "DELAYED"
        if any(p.get("kind") == "PLAYBACK" for p in connected):
            return "PLAYBACK"
        if any(p.get("kind") == "SIMULATED" for p in connected):
            return "SIMULATED"
        sim = self.global_simulation_state()
        if sim.get("global_simulation") is True:
            return "SIMULATED"
        return "UNKNOWN"

    def addon_heartbeat_age(self) -> Optional[float]:
        """Age of the AddOn snapshot clock. This is not a quote timestamp."""
        rt = self.runtime_snapshot()
        if not rt:
            return None
        epoch = _parse_iso_epoch(rt.get("timestamp") or rt.get("ts"))
        if epoch:
            return max(0.0, time.time() - float(epoch))
        mt = rt.get("_mtime")
        if mt:
            return max(0.0, time.time() - float(mt))
        return None

    def nq_mnq_series(self) -> dict[str, Any]:
        """Runtime Last timestamp is authoritative. Cache .ntb mtime is never LIVE."""
        empty = {
            "ok": False, "age_sec": None, "path": None, "instrument": None,
            "mtime": None, "last_price": None, "bid": None, "ask": None,
            "source": None, "timestamp": None,
        }
        if not self.root:
            return empty
        contracts = resolve_nq_mnq_contracts()
        rt = self.runtime_snapshot()
        if rt:
            mkt = rt.get("market_data") if isinstance(rt.get("market_data"), dict) else {}
            if not mkt:
                mkt = rt.get("market") if isinstance(rt.get("market"), dict) else {}
            last_price = _nz_price(mkt.get("last"))
            epoch = _parse_iso_epoch(mkt.get("last_update") or mkt.get("last_time") or mkt.get("timestamp"))
            nq_q = _extract_quote(rt.get("nq") or mkt.get("nq"), {"instrument": contracts["nq"]})
            mnq_q = _extract_quote(rt.get("mnq") or mkt.get("mnq"), {"instrument": contracts["mnq"]})
            if last_price is None and mnq_q.get("last") is not None:
                last_price = mnq_q.get("last")
                epoch = _parse_iso_epoch(mnq_q.get("timestamp")) or epoch
            if last_price is None and nq_q.get("last") is not None:
                last_price = nq_q.get("last")
                epoch = _parse_iso_epoch(nq_q.get("timestamp")) or epoch
            bid = mkt.get("bid")
            ask = mkt.get("ask")
            if bid is None:
                bid = mnq_q.get("bid") if mnq_q.get("bid") is not None else nq_q.get("bid")
            if ask is None:
                ask = mnq_q.get("ask") if mnq_q.get("ask") is not None else nq_q.get("ask")
            base = {
                "path": str(rt.get("_path") or "AITRADE_READONLY.json"),
                "instrument": mkt.get("instrument") or contracts["mnq"],
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
                "source": "NINJATRADER_READ_ONLY",
                "nq": nq_q,
                "mnq": mnq_q,
            }
            if last_price is not None and epoch:
                iso = datetime.fromtimestamp(float(epoch), tz=UTC).isoformat()
                base.update({
                    "ok": True,
                    "age_sec": max(0.0, time.time() - float(epoch)),
                    "mtime": iso,
                    "timestamp": iso,
                })
                return base
            base.update({
                "ok": False,
                "age_sec": max(0.0, time.time() - float(epoch)) if epoch else None,
                "mtime": datetime.fromtimestamp(float(epoch), tz=UTC).isoformat() if epoch else None,
                "timestamp": datetime.fromtimestamp(float(epoch), tz=UTC).isoformat() if epoch else None,
            })
            return base
        artifact_mtime = None
        artifact_path = None
        for folder in (self.root / "db" / "tick", self.root / "db" / "minute"):
            if not folder.is_dir():
                continue
            for p in folder.rglob("*"):
                if not p.is_file():
                    continue
                parent = p.parent.name.upper()
                name = p.name.upper()
                if not (parent.startswith("MNQ") or parent.startswith("NQ ") or "MNQ" in name or name.startswith("NQ ")):
                    continue
                if p.suffix.lower() != ".ncd" or "LAST" not in name:
                    continue
                mt = p.stat().st_mtime
                if artifact_mtime is None or mt > artifact_mtime:
                    artifact_mtime = mt
                    artifact_path = p
        if artifact_mtime is not None and artifact_path is not None:
            return {
                "ok": True,
                "age_sec": max(0.0, time.time() - artifact_mtime),
                "path": str(artifact_path),
                "instrument": artifact_path.parent.name,
                "mtime": datetime.fromtimestamp(artifact_mtime, tz=UTC).isoformat(),
                "timestamp": datetime.fromtimestamp(artifact_mtime, tz=UTC).isoformat(),
                "last_price": None,
                "bid": None,
                "ask": None,
                "source": str(artifact_path),
                "mtime_only": True,
            }
        return empty

    def market_heartbeat(self, stale_sec: float = 120.0) -> dict[str, Any]:
        """LIVE only with a real Last/Bid/Ask print + timestamp. File mtime is not LIVE.

        NinjaTrader Account Type = Simulation is not Simulated Data Feed.
        """
        feed = self.price_feed()
        series = self.nq_mnq_series()
        providers = self.market_providers()
        quality = self.feed_quality()
        sim = self.global_simulation_state()
        primary = _primary_provider(providers)
        age = series.get("age_sec")
        has_print = series.get("last_price") is not None and series.get("source") == "NINJATRADER_READ_ONLY"
        has_baa = series.get("bid") is not None and series.get("ask") is not None
        live_prov = any(p.get("connected") and p.get("kind") == "LIVE" for p in providers)
        delayed_prov = any(p.get("connected") and p.get("kind") == "DELAYED" for p in providers)
        playback_prov = any(p.get("connected") and p.get("kind") == "PLAYBACK" for p in providers)
        sim_prov = any(p.get("connected") and p.get("kind") == "SIMULATED" for p in providers)
        provider_connected = bool(live_prov or delayed_prov)
        nq_q = series.get("nq") if isinstance(series.get("nq"), dict) else {}
        mnq_q = series.get("mnq") if isinstance(series.get("mnq"), dict) else {}
        nq_present = bool(nq_q.get("last") is not None or nq_q.get("complete"))
        mnq_present = bool(mnq_q.get("last") is not None or mnq_q.get("complete"))
        both_required = nq_present and mnq_present
        both_ok = (not both_required) or (bool(nq_q.get("complete")) and bool(mnq_q.get("complete")))
        genuine = (
            bool(has_print)
            and has_baa
            and age is not None
            and age <= float(stale_sec)
            and quality not in ("SIMULATED", "DELAYED", "PLAYBACK")
            and live_prov
            and both_ok
        )
        delayed_fresh = bool(has_print) and age is not None and age <= float(stale_sec) and (quality == "DELAYED" or delayed_prov) and not live_prov
        contracts = resolve_nq_mnq_contracts()
        rt = self.runtime_snapshot()
        dump = self.connection_dump().strip()
        conn = "DISCONNECTED"
        if live_prov or delayed_prov or playback_prov or (sim_prov and (sim.get("global_simulation") or quality == "SIMULATED")):
            conn = "CONNECTED"
        addon_age = self.addon_heartbeat_age()
        addon_alive = addon_age is not None and addon_age <= ADDON_HEARTBEAT_SEC
        instr = str(series.get("instrument") or contracts["mnq"] or "")
        contract_ok = ("MNQ" in instr.upper()) or instr.upper().startswith("NQ")
        reason = "OK"
        env = None
        if isinstance(rt, dict):
            env = rt.get("account_environment") or (rt.get("connection") or {}).get("account_environment")
        if not env:
            env = primary.get("account_environment")
        if not env and primary.get("provider_kind") == "TRADOVATE":
            env = "SIMULATION"
        env = str(env).upper() if env else None
        if genuine and contract_ok:
            status = "LIVE"
            freshness = "LIVE"
            quality = "LIVE"
            conn = "CONNECTED"
            reason = "FRESH_QUOTE"
        elif delayed_fresh:
            status = "DELAYED"
            freshness = "STALE"
            quality = "DELAYED"
            conn = "CONNECTED"
            reason = "DELAYED_FEED"
        elif quality == "PLAYBACK" or (playback_prov and not live_prov and not delayed_prov):
            status = "PLAYBACK"
            freshness = "STALE"
            quality = "PLAYBACK"
            conn = "CONNECTED"
            reason = "PLAYBACK_CONNECTION"
        elif quality == "SIMULATED" or (sim_prov and not live_prov and not delayed_prov):
            status = "SIMULATED"
            freshness = "STALE"
            quality = "SIMULATED"
            conn = "CONNECTED"
            reason = "GLOBAL_SIMULATION" if sim.get("global_simulation") else "SIMULATED_DATA_FEED"
        elif live_prov or delayed_prov:
            status = "CONNECTED_STALE"
            freshness = "STALE"
            conn = "CONNECTED"
            quality = "UNKNOWN"
            if both_required and not both_ok:
                reason = "INCOMPLETE_NQ_MNQ"
            elif not has_print:
                reason = "NO_QUOTE"
            elif not has_baa:
                reason = "MISSING_BID_ASK"
            else:
                reason = "STALE_QUOTE"
        else:
            status = "DISCONNECTED"
            freshness = "DISCONNECTED"
            conn = "DISCONNECTED"
            quality = quality if quality in ("SIMULATED", "DELAYED", "PLAYBACK") else "UNKNOWN"
            reason = "ADDON_MISSING" if rt is None else "NO_MARKET_DATA_CONNECTION"
        if genuine and not contract_ok:
            status = "CONNECTED_STALE"
            freshness = "STALE"
            quality = "UNKNOWN"
            genuine = False
            reason = "WRONG_CONTRACT"
        snap_ts = None
        if rt:
            snap_ts = rt.get("timestamp") or rt.get("ts")
        quote_ts = series.get("timestamp") if has_print else None
        return {
            "status": status,
            "freshness": freshness,
            "quality": quality,
            "market_data_quality": quality,
            "reason": reason,
            "source": series.get("source") or "NINJATRADER_READ_ONLY",
            "price_feed": feed,
            "nq_mnq": series,
            "nq": nq_q,
            "mnq": mnq_q,
            "age_sec": age if has_print else None,
            "last_price": series.get("last_price") if has_print else None,
            "bid": series.get("bid") if has_print else None,
            "ask": series.get("ask") if has_print else None,
            "instrument": series.get("instrument") or contracts["mnq"],
            "ok": bool(genuine),
            "fresh": bool(genuine),
            "stale_sec": float(stale_sec),
            "last_update": quote_ts,
            "timestamp": quote_ts,
            "quote_timestamp": quote_ts,
            "snapshot_timestamp": snap_ts,
            "addon_heartbeat_age_sec": addon_age,
            "addon_heartbeat_alive": bool(addon_alive),
            "global_simulation": sim.get("global_simulation"),
            "global_simulation_source": sim.get("source"),
            "providers": providers,
            "connection_dump": dump,
            "market_provider_connected": provider_connected,
            "ninjatrader_market_connection": conn,
            "provider_name": primary.get("provider_name") or primary.get("name"),
            "provider_display_name": primary.get("provider_display_name") or primary.get("name"),
            "provider_backend": primary.get("provider_backend") or primary.get("provider_type"),
            "provider_id": primary.get("provider_id"),
            "provider_kind": primary.get("provider_kind") or "UNKNOWN",
            "provider_status": "CONNECTED" if primary.get("connected") else str(primary.get("status") or "DISCONNECTED").upper(),
            "account_environment": env,
            "contracts": contracts,
            "signal_instrument": contracts.get("nq"),
            "position_instrument": contracts.get("mnq"),
            "PROP_EXECUTION": False,
        }

    def sqlite_positions(self, account_id: Optional[int]) -> list[dict[str, Any]]:
        if account_id is None:
            return []
        con = self._connect()
        if con is None:
            return []
        out = []
        try:
            for r in con.execute(
                "SELECT Instrument, AvgPrice, MarketPosition, Quantity FROM Positions WHERE Account=?",
                (account_id,),
            ):
                out.append(dict(r))
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return out

    def working_orders(self, account_id: Optional[int]) -> list[dict[str, Any]]:
        if account_id is None:
            return []
        con = self._connect()
        if con is None:
            return []
        out = []
        try:
            for r in con.execute(
                "SELECT OrderId, Name, OrderState, Filled, Quantity FROM Orders WHERE Account=?",
                (account_id,),
            ):
                if int(r["OrderState"]) in WORKING_ORDER_STATES and int(r["Filled"] or 0) < int(r["Quantity"] or 0):
                    out.append(dict(r))
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return out

    def log_position(self, account: str, *, prefer_mnq: bool = True) -> Optional[dict[str, Any]]:
        if not self.root:
            return None
        log_dir = self.root / "log"
        if not log_dir.is_dir():
            return None
        files = sorted(log_dir.glob("log.*.en.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
        last = None
        for path in reversed(files):
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                m = _POS_RE.search(line)
                if not m or m.group("acct") != account:
                    continue
                instr = m.group("instr")
                iu = instr.upper()
                if prefer_mnq and not (iu.startswith("MNQ") or iu.startswith("NQ")):
                    continue
                last = {
                    "account": account,
                    "instrument": instr,
                    "average_price": float(m.group("avg")),
                    "quantity": int(m.group("qty")),
                    "market_position": m.group("pos"),
                    "flat": m.group("pos") == "Flat" or int(m.group("qty")) == 0,
                    "source": "nt_log",
                    "known": True,
                }
        return last

    def position_for(self, account_name: str, account_id: Optional[int]) -> dict[str, Any]:
        rt = self.runtime_snapshot()
        if rt and classify_account_name(str((rt.get("account") or {}).get("id") or (rt.get("fundednext") or {}).get("account_id") or "")) == "FUNDEDNEXT":
            if str((rt.get("account") or {}).get("id") or (rt.get("fundednext") or {}).get("account_id") or "") == account_name:
                pos = rt.get("position") if isinstance(rt.get("position"), dict) else {}
                mp = str(pos.get("market_position") or pos.get("side") or "Flat")
                qty = int(pos.get("quantity") or 0)
                side = "FLAT" if mp.lower() == "flat" or qty == 0 else ("LONG" if "long" in mp.lower() else "SHORT")
                return {
                    "source": "ninjatrader_runtime_json",
                    "account": account_name,
                    "instrument": pos.get("instrument") or "MNQ",
                    "side": side,
                    "quantity": 0 if side == "FLAT" else abs(qty),
                    "entry": pos.get("average_price") if side != "FLAT" else None,
                    "flat": side == "FLAT",
                    "error": None,
                    "known": True,
                }
        logged = self.log_position(account_name)
        if logged:
            qty = int(logged["quantity"] or 0)
            mp = str(logged["market_position"])
            side = "FLAT" if logged["flat"] or qty == 0 else ("LONG" if "long" in mp.lower() else "SHORT")
            return {
                "source": "nt_log",
                "account": account_name,
                "instrument": logged["instrument"],
                "side": side,
                "quantity": 0 if side == "FLAT" else qty,
                "entry": logged["average_price"] if side != "FLAT" else None,
                "flat": side == "FLAT",
                "error": None,
                "known": True,
            }
        sql = self.sqlite_positions(account_id)
        if sql:
            r = sql[-1]
            qty = int(r.get("Quantity") or 0)
            mp = int(r.get("MarketPosition") or 0)
            # NT MarketPosition: 0 Flat, 1 Long, -1 / 2 Short depending on version; Quantity 0 => flat.
            if qty == 0 or mp == 0:
                side = "FLAT"
            elif mp > 0:
                side = "LONG"
            else:
                side = "SHORT"
            return {
                "source": "nt_sqlite",
                "account": account_name,
                "instrument": "MNQ",
                "side": side,
                "quantity": 0 if side == "FLAT" else abs(qty),
                "entry": r.get("AvgPrice"),
                "flat": side == "FLAT",
                "error": None,
                "known": True,
            }
        return {
            "source": "nt_empty",
            "account": account_name,
            "instrument": "MNQ",
            "side": "FLAT",
            "quantity": 0,
            "entry": None,
            "flat": True,
            "error": None,
            "known": True,
        }

    def snapshot_account(self, account_name: str, account_id: Optional[int]) -> dict[str, Any]:
        if classify_account_name(account_name) == "SIM":
            # Sim101/playback may be snapshotted for isolation tests only; callers must not use as FN.
            pass
        rt = self.runtime_snapshot()
        rt_fn = rt.get("fundednext") if rt and isinstance(rt.get("fundednext"), dict) else {}
        rt_acc = rt.get("account") if rt and isinstance(rt.get("account"), dict) else {}
        rt_id = str(rt_fn.get("account_id") or rt_acc.get("id") or "")
        if rt and rt_id == account_name and classify_account_name(account_name) == "FUNDEDNEXT":
            cash = rt_fn.get("cash_value") if rt_fn.get("cash_value") is not None else rt_acc.get("cash_value")
            net = rt_fn.get("net_liquidation") if rt_fn.get("net_liquidation") is not None else rt_acc.get("net_liquidation")
            try:
                cash_f = float(cash) if cash is not None else None
            except (TypeError, ValueError):
                cash_f = None
            try:
                net_f = float(net) if net is not None else None
            except (TypeError, ValueError):
                net_f = None
            if net_f is not None and abs(net_f) < 1e-9:
                net_f = None
            if cash_f is not None and abs(cash_f) < 1e-9:
                cash_f = None
            equity = net_f if net_f is not None else cash_f
            if rt_fn.get("value_source") == "UNAVAILABLE_FROM_NINJATRADER_RUNTIME":
                equity = None
                cash_f = None
                net_f = None
            ts = rt.get("timestamp") or rt.get("ts") or rt_acc.get("timestamp")
            age = None
            if rt.get("_mtime"):
                age = max(0.0, time.time() - float(rt["_mtime"]))
            realized = rt_fn.get("realized_pnl")
            if realized is None:
                realized = rt_acc.get("realized")
            unreal = rt_fn.get("unrealized_pnl")
            if unreal is None:
                unreal = rt_acc.get("unrealized")
            return {
                "account": account_name,
                "account_id": account_id,
                "equity": float(equity) if equity is not None else None,
                "cash_value": cash_f,
                "net_liquidation": net_f,
                "realized_pnl": realized,
                "unrealized_pnl": unreal,
                "today_pnl": rt_acc.get("today_pnl"),
                "items_present": equity is not None,
                "source": "ninjatrader_runtime_json" if equity is not None else "UNAVAILABLE_FROM_NINJATRADER_RUNTIME",
                "asof": ts,
                "fresh": bool(age is not None and age <= 30),
                "available_account_items": rt.get("available_account_items") or {},
                "sim101_excluded": True,
            }
        items = self.account_items(account_id) if account_id is not None else {}
        numeric = {k: v.get("value") for k, v in items.items()}
        trace_nums = self._trace_account_items_numeric(account_name)
        for k, v in trace_nums.items():
            if numeric.get(k) is None:
                numeric[k] = v
        cash = numeric.get("CashValue")
        net = numeric.get("NetLiquidation")
        equity = net if net not in (None, 0) else cash
        if not items and not trace_nums:
            return {
                "account": account_name,
                "account_id": account_id,
                "equity": None,
                "cash_value": None,
                "net_liquidation": None,
                "realized_pnl": None,
                "unrealized_pnl": None,
                "today_pnl": None,
                "items_present": False,
                "source": "ninjatrader_missing",
                "asof": None,
            }
        realized = numeric.get("RealizedProfitLoss")
        if realized is None:
            realized = numeric.get("GrossRealizedProfitLoss")
        unreal = numeric.get("UnrealizedProfitLoss")
        sod = numeric.get("SodCashValue")
        today = None
        if equity is not None and sod not in (None, 0):
            today = float(equity) - float(sod)
        elif realized is not None:
            today = float(realized) + float(unreal or 0)
        ts = None
        if items.get("CashValue", {}).get("time"):
            ts = items["CashValue"]["time"]
        elif items.get("NetLiquidation", {}).get("time"):
            ts = items["NetLiquidation"]["time"]
        return {
            "account": account_name,
            "account_id": account_id,
            "equity": float(equity) if equity is not None else None,
            "cash_value": cash,
            "net_liquidation": net,
            "realized_pnl": float(realized) if realized is not None else None,
            "unrealized_pnl": float(unreal) if unreal is not None else None,
            "today_pnl": today,
            "items_present": bool(items),
            "source": "ninjatrader_sqlite" if items else ("ninjatrader_trace" if trace_nums else "ninjatrader_missing"),
            "asof": ts,
        }

    def fundednext_account_state(self, expected: Optional[str] = None) -> dict[str, Any]:
        fn = self.fundednext_account(expected)
        if not fn:
            return {
                "account_id": None,
                "equity": None,
                "net_liquidation": None,
                "mll": None,
                "remaining_loss_buffer": None,
                "source": "ninjatrader_missing",
                "timestamp": None,
                "fresh": False,
            }
        raw = self.snapshot_account(fn["name"], fn.get("id"))
        if classify_account_name(fn["name"]) == "SIM" or fn["name"] == "Sim101":
            return {
                "account_id": fn["name"],
                "equity": None,
                "net_liquidation": None,
                "mll": None,
                "remaining_loss_buffer": None,
                "source": "rejected_sim101",
                "timestamp": None,
                "fresh": False,
            }
        equity = raw.get("equity")
        mll = None
        rem = None
        if equity is not None:
            from phase52_policy import MLL_LOCK_AT, START_EQUITY, MAX_LOSS, remaining_drawdown

            mll = float(MLL_LOCK_AT) if equity + 1e-9 >= MLL_LOCK_AT else float(START_EQUITY - MAX_LOSS)
            rem = remaining_drawdown(equity, mll)
        age = None
        if raw.get("asof"):
            try:
                age = max(0.0, time.time() - datetime.fromisoformat(str(raw["asof"]).replace("Z", "+00:00")).timestamp())
            except ValueError:
                age = None
        return {
            "account_id": fn["name"],
            "equity": equity,
            "net_liquidation": raw.get("net_liquidation"),
            "mll": mll,
            "remaining_loss_buffer": rem,
            "source": raw.get("source"),
            "timestamp": raw.get("asof"),
            "fresh": bool(equity is not None and (raw.get("fresh") or (age is not None and age <= 120))),
            "sim101_excluded": True,
        }
