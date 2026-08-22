"""One-off Saturday recovery-gate probe. Read-only besides writing evidence JSON."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from execution_status import NQ_FROZEN_HASH, sim_only_execution_armed
from nt_readonly import NTReadOnly
from phase54_ops import PROP_EXECUTION, EngineSupervisor
from phase55_execution_bridge import PHASE_55A_MAX_QTY


def _iso():
    return datetime.now(timezone.utc).isoformat()


def _proc(name: str) -> list[dict]:
    rows = []
    try:
        import subprocess

        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter \"name='{name}.exe'\" | Select-Object ProcessId,CreationDate,Name | ConvertTo-Json -Compress",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for p in data or []:
            rows.append({"pid": p.get("ProcessId"), "name": p.get("Name"), "start": p.get("CreationDate")})
    except Exception:
        pass
    return rows


def snapshot_http(timeout=90):
    with urllib.request.urlopen("http://127.0.0.1:8765/api/snapshot", timeout=timeout) as r:
        return json.loads(r.read().decode())


def collect(label: str, *, http=True) -> dict:
    nt = NTReadOnly()
    rt = nt.runtime_snapshot() or {}
    dump_path = rt.get("_path")
    mtime = rt.get("_mtime")
    age = round(time.time() - float(mtime), 3) if mtime is not None else None
    sim = rt.get("sim101") if isinstance(rt.get("sim101"), dict) else {}
    pos = sim.get("position") if isinstance(sim.get("position"), dict) else {}
    oif = []
    if dump_path:
        out = Path(dump_path).parent
        oif = [p.name for p in list(out.glob("*.oif")) + list(out.glob("*.OIF"))]
        incoming = out.parent / "incoming"
        inc = [p.name for p in incoming.glob("*") if p.is_file()] if incoming.exists() else []
    else:
        inc = []
    eng = EngineSupervisor.status()
    doc = {
        "label": label,
        "ts": _iso(),
        "nt_processes": _proc("NinjaTrader"),
        "python_hint": _proc("python"),
        "engine": {
            "engine": eng.get("engine"),
            "entries_paused": eng.get("entries_paused"),
            "order_execution": eng.get("order_execution"),
            "PROP_EXECUTION": eng.get("PROP_EXECUTION"),
            "heartbeat_ts": eng.get("heartbeat_ts"),
            "started_at": eng.get("started_at"),
        },
        "flags": {
            "PROP_EXECUTION": PROP_EXECUTION,
            "sim_only_armed": sim_only_execution_armed(),
            "AITRADE_SIM_ONLY_EXECUTION": os.environ.get("AITRADE_SIM_ONLY_EXECUTION"),
            "qty_cap": PHASE_55A_MAX_QTY,
            "frozen_nq_hash": NQ_FROZEN_HASH,
        },
        "dump": {
            "path": dump_path,
            "age_sec": age,
            "timestamp": rt.get("timestamp") or rt.get("ts"),
            "schema": rt.get("schema"),
            "nq_bars_1m_status": rt.get("nq_bars_1m_status"),
            "nq_bars_1m_count": rt.get("nq_bars_1m_count"),
            "sim101_present": sim.get("present"),
            "sim101_excluded": sim.get("excluded"),
            "sim101_account": sim.get("account") or sim.get("id"),
            "sim101_position": pos,
            "fn_top_level_position": rt.get("position"),
        },
        "oif_outgoing": oif,
        "incoming_files": inc,
    }
    if http:
        try:
            s = snapshot_http()
            doc["snap"] = {
                "engine": s.get("engine"),
                "PROP_EXECUTION": s.get("PROP_EXECUTION"),
                "execution_arm": s.get("execution_arm"),
                "fundednext_connection": s.get("fundednext_connection"),
                "fundednext_permission": s.get("fundednext_permission"),
                "market_data_status": s.get("market_data_status"),
                "market_data_quality": s.get("market_data_quality"),
                "market_age_seconds": s.get("market_age_seconds"),
                "sim101": s.get("sim101"),
                "sim101_recovery": s.get("sim101_recovery"),
                "safe_start": (s.get("checks") or {}).get("safe_start_result"),
                "ok_to_run": (s.get("checks") or {}).get("ok_to_run_engine"),
                "signal_source": (s.get("decision") or {}).get("signal_source"),
                "last_live": (s.get("decision") or {}).get("last_live_signal"),
                "last_shadow_dir": ((s.get("last_shadow_signal") or {}) or {}).get("direction"),
                "last_shadow_source": ((s.get("last_shadow_signal") or {}) or {}).get("source"),
                "notifications": s.get("notifications"),
                "incoming_oif": (s.get("connection") or {}).get("incoming_oif"),
                "hash_nq": (s.get("hashes") or {}).get("nq"),
                "telemetry_dump": s.get("telemetry_dump"),
            }
        except Exception as exc:
            doc["snap_error"] = str(exc)
    return doc


def main():
    import sys

    label = sys.argv[1] if len(sys.argv) > 1 else "probe"
    http = "--no-http" not in sys.argv
    doc = collect(label, http=http)
    out = Path("journal/phase54_ops/saturday_recovery_gates.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, default=str) + "\n")
    print(json.dumps(doc, default=str, indent=2)[:8000])


if __name__ == "__main__":
    main()
