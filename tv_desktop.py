"""TradingView Desktop discovery, CDP launch, and preflight (Windows)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222
CONFIG_PATH = Path(__file__).resolve().parent / "tv_desktop_config.json"

# Narrow candidate roots — never scan the whole filesystem.
_KNOWN_RELATIVE_HINTS = (
    r"TradingView\TradingView.exe",
    r"Programs\TradingView\TradingView.exe",
)


@dataclass(frozen=True)
class TradingViewDiscovery:
    found: bool
    executable: Optional[str] = None
    source: Optional[str] = None  # process | config | appx | known_path
    process_ids: list[int] = field(default_factory=list)
    package_full_name: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CdpPreflight:
    tradingview_process_found: bool
    executable: Optional[str]
    cdp_port: int
    cdp_reachable: bool
    browser_websocket: Optional[str]
    chart_targets: list[dict[str, Any]]
    discovery: dict[str, Any]
    error: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_configured_path(config_path: Path = CONFIG_PATH) -> Optional[str]:
    if not config_path.exists():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    path = raw.get("executable") or raw.get("path")
    if path and Path(path).is_file():
        return str(Path(path))
    return None


def save_configured_path(executable: str, config_path: Path = CONFIG_PATH) -> None:
    config_path.write_text(
        json.dumps(
            {
                "executable": str(executable),
                "cdp_port": DEFAULT_CDP_PORT,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _running_tradingview_exes() -> list[tuple[int, str]]:
    """Return (pid, exe_path) for running TradingView processes via PowerShell."""
    # Prefer PowerShell Get-Process (works for MSIX paths).
    ps = (
        "Get-Process -Name TradingView -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path } | "
        "ForEach-Object { '{0}|{1}' -f $_.Id, $_.Path }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: list[tuple[int, str]] = []
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_s, path = line.split("|", 1)
        try:
            out.append((int(pid_s), path.strip()))
        except ValueError:
            continue
    return out


def _appx_tradingview_exe() -> tuple[Optional[str], Optional[str]]:
    """Locate TradingView.exe via AppX package metadata (no full FS scan)."""
    ps = (
        "$p = Get-AppxPackage -Name '*TradingView*' -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        "if (-not $p) { return }; "
        "$exe = Join-Path $p.InstallLocation 'TradingView.exe'; "
        "if (Test-Path $exe) { '{0}|{1}' -f $p.PackageFullName, $exe }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None, None
    row = line[-1].strip()
    if "|" not in row:
        return None, None
    pkg, exe = row.split("|", 1)
    if Path(exe).is_file():
        return exe.strip(), pkg.strip()
    return None, pkg.strip() if pkg else None


def _known_path_candidates() -> list[str]:
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
    ]
    out = []
    for root in roots:
        if not root:
            continue
        for rel in _KNOWN_RELATIVE_HINTS:
            p = Path(root) / rel
            if p.is_file():
                out.append(str(p))
    # Common MSIX parent without enumerating all WindowsApps packages:
    winapps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    if winapps.is_dir():
        try:
            for child in winapps.iterdir():
                if not child.name.startswith("TradingView.Desktop"):
                    continue
                exe = child / "TradingView.exe"
                if exe.is_file():
                    out.append(str(exe))
                    break
        except PermissionError:
            pass
    return out


def discover_tradingview(
    *,
    config_path: Path = CONFIG_PATH,
    prefer_running: bool = True,
) -> TradingViewDiscovery:
    """
    Locate TradingView Desktop executable without a blind filesystem scan.

    Preference order:
      1. configured path (if valid)
      2. running process Path
      3. AppX package InstallLocation
      4. known relative paths / TradingView.Desktop* under WindowsApps
    """
    notes: list[str] = []
    configured = load_configured_path(config_path)
    if configured:
        notes.append(f"config_ok:{configured}")

    running = _running_tradingview_exes()
    pids = [p for p, _ in running]
    if prefer_running and running:
        exe = running[0][1]
        return TradingViewDiscovery(
            found=True,
            executable=exe,
            source="process",
            process_ids=pids,
            notes=notes + [f"running_count={len(running)}"],
        )

    if configured:
        return TradingViewDiscovery(
            found=True,
            executable=configured,
            source="config",
            process_ids=pids,
            notes=notes,
        )

    appx_exe, pkg = _appx_tradingview_exe()
    if appx_exe:
        return TradingViewDiscovery(
            found=True,
            executable=appx_exe,
            source="appx",
            process_ids=pids,
            package_full_name=pkg,
            notes=notes,
        )

    known = _known_path_candidates()
    if known:
        return TradingViewDiscovery(
            found=True,
            executable=known[0],
            source="known_path",
            process_ids=pids,
            notes=notes + [f"candidates={len(known)}"],
        )

    return TradingViewDiscovery(
        found=False,
        process_ids=pids,
        notes=notes + ["no_executable_located"],
    )


def cdp_version(host: str = DEFAULT_CDP_HOST, port: int = DEFAULT_CDP_PORT) -> dict[str, Any]:
    url = f"http://{host}:{port}/json/version"
    try:
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        return {"ok": True, "payload": resp.json()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def cdp_targets(host: str = DEFAULT_CDP_HOST, port: int = DEFAULT_CDP_PORT) -> dict[str, Any]:
    url = f"http://{host}:{port}/json"
    try:
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        rows = resp.json()
        charts = [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "url": t.get("url"),
                "type": t.get("type"),
                "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
            }
            for t in rows
            if isinstance(t, dict)
            and "tradingview.com/chart" in str(t.get("url") or "")
        ]
        return {"ok": True, "targets": rows, "chart_targets": charts}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "targets": [], "chart_targets": []}


def cdp_preflight(
    *,
    host: str = DEFAULT_CDP_HOST,
    port: int = DEFAULT_CDP_PORT,
    discovery: Optional[TradingViewDiscovery] = None,
) -> CdpPreflight:
    disc = discovery or discover_tradingview()
    ver = cdp_version(host, port)
    tg = cdp_targets(host, port) if ver.get("ok") else {
        "ok": False,
        "chart_targets": [],
        "error": ver.get("error"),
    }
    ws = None
    if ver.get("ok"):
        ws = (ver.get("payload") or {}).get("webSocketDebuggerUrl")
    err = None
    if not ver.get("ok"):
        err = ver.get("error") or "CDP unreachable"
    elif not (tg.get("chart_targets") or []):
        err = "CDP up but no TradingView chart target found"

    return CdpPreflight(
        tradingview_process_found=bool(disc.process_ids) or bool(disc.found),
        executable=disc.executable,
        cdp_port=port,
        cdp_reachable=bool(ver.get("ok")),
        browser_websocket=ws,
        chart_targets=list(tg.get("chart_targets") or []),
        discovery=disc.to_dict(),
        error=err,
        extras={
            "browser_version": (ver.get("payload") or {}).get("Browser"),
            "target_count": len(tg.get("targets") or []),
        },
    )


def launch_tradingview_with_cdp(
    *,
    executable: Optional[str] = None,
    port: int = DEFAULT_CDP_PORT,
    kill_existing: bool = False,
    wait_seconds: float = 12.0,
) -> dict[str, Any]:
    """
    Launch TradingView with --remote-debugging-port.

    Does not belong inside the analysis engine. Call explicitly before live work.
    If CDP is already reachable, returns reused=True without launching.
    """
    pre = cdp_preflight(port=port)
    if pre.cdp_reachable and pre.chart_targets:
        return {
            "ok": True,
            "reused": True,
            "launched": False,
            "preflight": pre.to_dict(),
        }

    disc = discover_tradingview()
    exe = executable or disc.executable
    if not exe or not Path(exe).is_file():
        return {
            "ok": False,
            "reused": False,
            "launched": False,
            "error": "TradingView executable not found",
            "discovery": disc.to_dict(),
            "preflight": pre.to_dict(),
        }

    save_configured_path(exe)

    if kill_existing and disc.process_ids:
        # Electron often ignores new debug flags if an instance is already up.
        try:
            subprocess.run(
                ["taskkill", "/IM", "TradingView.exe", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            time.sleep(2.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "error": f"failed to stop existing TradingView: {exc}",
                "executable": exe,
            }

    try:
        proc = subprocess.Popen(
            [exe, f"--remote-debugging-port={port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(exe).parent),
        )
    except OSError as exc:
        return {
            "ok": False,
            "reused": False,
            "launched": False,
            "error": str(exc),
            "executable": exe,
        }

    deadline = time.time() + max(1.0, wait_seconds)
    last = None
    while time.time() < deadline:
        last = cdp_preflight(port=port)
        if last.cdp_reachable:
            break
        time.sleep(0.75)

    return {
        "ok": bool(last and last.cdp_reachable),
        "reused": False,
        "launched": True,
        "pid": proc.pid,
        "executable": exe,
        "preflight": (last or cdp_preflight(port=port)).to_dict(),
        "note": (
            None
            if last and last.cdp_reachable
            else "Launched but CDP not reachable yet; chart may still be loading"
        ),
    }
