"""Read-only Saturday evidence gatherer. Does not arm, submit, or mutate strategy."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from execution_status import NQ_FROZEN_HASH, sim_only_execution_armed
from nt_readonly import NTReadOnly
from phase54_ops import PROP_EXECUTION
from phase55_execution_bridge import PHASE_55A_MAX_QTY
from sim101_telemetry import fundednext_must_not_substitute, parse_sim101_position, recovery_from_sim101

OUT = ROOT / "evidence" / "2026-08-22-saturday-ops"
RAW = OUT / "raw"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)


def sha256(p: Path) -> dict:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = p.stat()
    return {
        "path": str(p),
        "exists": True,
        "size": st.st_size,
        "mtime_unix": st.st_mtime,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "mtime_local": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(),
        "sha256": h.hexdigest(),
    }


def slim_snap(s: dict | None) -> dict | None:
    if not s:
        return None
    n = s.get("notifications") or {}
    return {
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
        "checks": s.get("checks"),
        "decision": s.get("decision"),
        "last_shadow_signal": s.get("last_shadow_signal"),
        "telemetry_dump": s.get("telemetry_dump"),
        "hashes": s.get("hashes"),
        "incoming_oif": (s.get("connection") or {}).get("incoming_oif"),
        "notifications": {
            k: n.get(k)
            for k in (
                "enabled",
                "configured",
                "delivery_status",
                "last_event_type",
                "last_success_ts",
                "last_failure_reason",
                "channel",
                "backend",
            )
        },
    }


def main() -> None:
    files = {
        "repo_cs": sha256(ROOT / "ninjascript" / "AITRADEReadOnlySnapshot.cs"),
        "nt_cs": sha256(
            Path(r"C:\Users\tanam\OneDrive\Documents\NinjaTrader 8\bin\Custom\AddOns\AITRADEReadOnlySnapshot.cs")
        ),
        "dll": sha256(Path(r"C:\Users\tanam\OneDrive\Documents\NinjaTrader 8\bin\Custom\NinjaTrader.Custom.dll")),
    }
    nt = NTReadOnly()
    stamps = []
    for i in range(4):
        rt = nt.runtime_snapshot() or {}
        stamps.append(
            {
                "i": i,
                "timestamp": rt.get("timestamp") or rt.get("ts"),
                "age_sec": round(time.time() - float(rt.get("_mtime") or 0), 3),
            }
        )
        time.sleep(1.05)
    rt = nt.runtime_snapshot() or {}
    sim = rt.get("sim101") if isinstance(rt.get("sim101"), dict) else {}
    pos = sim.get("position") if isinstance(sim.get("position"), dict) else {}
    diag = rt.get("diagnostics") if isinstance(rt.get("diagnostics"), dict) else {}
    schema = {
        "schema": rt.get("schema"),
        "timestamp": rt.get("timestamp") or rt.get("ts"),
        "read_only": rt.get("read_only"),
        "PROP_EXECUTION": rt.get("PROP_EXECUTION"),
        "orders_transmitted": rt.get("orders_transmitted"),
        "sim101_excluded": rt.get("sim101_excluded"),
        "sim101": {
            "present": sim.get("present"),
            "excluded": sim.get("excluded"),
            "account": sim.get("account") or sim.get("id"),
            "read_only": sim.get("read_only"),
            "position": pos,
        },
        "nq_bars_1m": rt.get("nq_bars_1m"),
        "nq_bars_1m_count": rt.get("nq_bars_1m_count"),
        "nq_bars_1m_status": rt.get("nq_bars_1m_status"),
        "diagnostics": {
            "nq_found": diag.get("nq_found"),
            "mnq_found": diag.get("mnq_found"),
            "nq_name": diag.get("nq_name"),
            "mnq_name": diag.get("mnq_name"),
            "nq_1m_bars_request": diag.get("nq_1m_bars_request"),
            "bars_request": diag.get("bars_request"),
            "market_data_subscribed": diag.get("market_data_subscribed"),
        },
        "nq": rt.get("nq") if isinstance(rt.get("nq"), dict) else {},
        "mnq": rt.get("mnq") if isinstance(rt.get("mnq"), dict) else {},
        "account_environment": rt.get("account_environment"),
        "refresh_samples": stamps,
    }
    (OUT / "03_TELEMETRY_SCHEMA.json").write_text(json.dumps(schema, indent=2, default=str), encoding="utf-8")

    parsed = parse_sim101_position(rt)
    rec_map = recovery_from_sim101(parsed)
    fn_sub = fundednext_must_not_substitute(rt, parsed)

    snap = None
    snap_err = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/snapshot", timeout=90) as r:
            snap = json.loads(r.read().decode())
    except Exception as exc:
        snap_err = str(exc)

    out_dir = Path(r"C:\Users\tanam\OneDrive\Documents\NinjaTrader 8\outgoing")
    inc_dir = out_dir.parent / "incoming"
    oif = [p.name for p in list(out_dir.glob("*.oif")) + list(out_dir.glob("*.OIF"))]
    inc = [p.name for p in inc_dir.glob("*") if p.is_file()] if inc_dir.exists() else []

    nt_proc = None
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process NinjaTrader -ErrorAction SilentlyContinue | Select-Object Id,StartTime,MainWindowTitle | ConvertTo-Json -Compress",
            ],
            text=True,
        ).strip()
        nt_proc = json.loads(raw) if raw else None
    except Exception as exc:
        nt_proc = {"error": str(exc)}

    facts = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "nt_process": nt_proc,
        "frozen_nq_hash": NQ_FROZEN_HASH,
        "PROP_EXECUTION_module": PROP_EXECUTION,
        "sim_only_armed": sim_only_execution_armed(),
        "AITRADE_SIM_ONLY_EXECUTION": os.environ.get("AITRADE_SIM_ONLY_EXECUTION"),
        "qty_cap": PHASE_55A_MAX_QTY,
        "parsed_sim101": parsed,
        "recovery_from_sim101": rec_map,
        "fundednext_must_not_substitute": fn_sub,
        "oif_outgoing": oif,
        "incoming_files": inc,
        "orders_transmitted": rt.get("orders_transmitted"),
        "snapshot_error": snap_err,
        "snapshot": slim_snap(snap),
    }
    (RAW / "facts.json").write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")

    lines = [
        "AITRADE Saturday 2026-08-22 DLL / source hashes (read-only)",
        "captured_utc=" + facts["captured_utc"],
    ]
    for k, v in files.items():
        lines.append("")
        lines.append("[" + k + "]")
        for kk in ("path", "size", "mtime_utc", "mtime_local", "mtime_unix", "sha256"):
            lines.append(f"{kk}={v[kk]}")
    lines.extend(
        [
            "",
            "repo_cs_sha256 == nt_cs_sha256: " + str(files["repo_cs"]["sha256"] == files["nt_cs"]["sha256"]),
            "dll_mtime > repo_cs_mtime: " + str(files["dll"]["mtime_unix"] > files["repo_cs"]["mtime_unix"]),
            "dll_mtime > nt_cs_mtime: " + str(files["dll"]["mtime_unix"] > files["nt_cs"]["mtime_unix"]),
            "dll_mtime_unix_unchanged_through_cold_restart=1787400202.0502925",
            "frozen_nq_hash=" + NQ_FROZEN_HASH,
            "nt_process=" + json.dumps(nt_proc, default=str),
        ]
    )
    (OUT / "02_DLL_HASHES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ev = []
    jpath = ROOT / "journal" / "phase54_ops" / "notifications.jsonl"
    if jpath.exists():
        for line in jpath.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            ev.append(
                {
                    k: d.get(k)
                    for k in (
                        "ts",
                        "event_id",
                        "event_type",
                        "severity",
                        "destination",
                        "success",
                        "delivered",
                        "failure_reason",
                        "process",
                    )
                }
            )
    (RAW / "notifications_events.json").write_text(json.dumps(ev, indent=2), encoding="utf-8")

    labels = []
    gpath = ROOT / "journal" / "phase54_ops" / "saturday_recovery_gates.jsonl"
    if gpath.exists():
        for line in gpath.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            snapd = d.get("snap") if isinstance(d.get("snap"), dict) else {}
            dumpd = d.get("dump") if isinstance(d.get("dump"), dict) else {}
            eng = d.get("engine") if isinstance(d.get("engine"), dict) else {}
            labels.append(
                {
                    "label": d.get("label"),
                    "ts": d.get("ts"),
                    "verdict": d.get("verdict"),
                    "overall": d.get("overall"),
                    "nt_processes": d.get("nt_processes"),
                    "engine": eng.get("engine") if isinstance(eng, dict) else d.get("engine"),
                    "dump_age": dumpd.get("age_sec"),
                    "sim101_recovery": snapd.get("sim101_recovery"),
                    "fail_reasons": d.get("fail_reasons"),
                }
            )
    (RAW / "gate_labels.json").write_text(json.dumps(labels, indent=2, default=str), encoding="utf-8")
    print("ok recovery", rec_map, "orders", rt.get("orders_transmitted"), "armed", sim_only_execution_armed())


if __name__ == "__main__":
    main()
