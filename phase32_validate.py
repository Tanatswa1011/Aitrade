"""Phase 32 validation — freeze integrity, pause state, secret scan, test gate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VALIDATION_PATH = ROOT / "phase32_validation.json"
PAUSE_PATH = ROOT / "state" / "project_pause.json"

GC_EXPECTED = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
NQ_EXPECTED = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"

JOURNAL_DIRS = [
    ROOT / "journal" / "phase26_gc_vwap_v2_paper",
    ROOT / "journal" / "phase30_nq_dvp_paper",
    ROOT / "journal" / "phase31_nq_dvp_sim",
]

SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{16,})"), "credential_assignment"),
    (re.compile(r"(?i)(DATABENTO|TIINGO|RITHMIC|NINJATRADER)[_-]?(KEY|TOKEN|SECRET)\s*[:=]\s*\S+"), "vendor_credential"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-._~+/]+=*"), "bearer_token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai_style_key"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github_pat"),
]

SKIP_SCAN = {
    ".env",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def journal_fingerprints() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for d in JOURNAL_DIRS:
        rel = str(d.relative_to(ROOT))
        files: dict[str, str] = {}
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    files[str(p.relative_to(ROOT))] = file_sha256(p)
        out[rel] = {"files": files, "file_count": len(files)}
    return out


def verify_frozen_hashes() -> dict[str, Any]:
    gc_path = ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"
    nq_path = ROOT / "strategy_frozen" / "nq_dvp_phase30.json"
    gc = json.loads(gc_path.read_text(encoding="utf-8"))
    nq = json.loads(nq_path.read_text(encoding="utf-8"))
    gc_hash = gc.get("frozen_config_hash")
    nq_hash = nq.get("frozen_config_hash")
    gc_ok = gc_hash == GC_EXPECTED
    nq_ok = nq_hash == NQ_EXPECTED
    return {
        "gc": {"hash": gc_hash, "expected": GC_EXPECTED, "unchanged": gc_ok, "version": gc.get("strategy_version")},
        "nq": {"hash": nq_hash, "expected": NQ_EXPECTED, "unchanged": nq_ok, "version": nq.get("strategy_version")},
        "ok": gc_ok and nq_ok,
    }


def verify_pause_state() -> dict[str, Any]:
    if not PAUSE_PATH.exists():
        return {"ok": False, "error": "missing_project_pause"}
    doc = json.loads(PAUSE_PATH.read_text(encoding="utf-8"))
    ok = (
        bool(doc.get("paused"))
        and doc.get("execution_status") == "PAUSED"
        and doc.get("gc_frozen_hash") == GC_EXPECTED
        and doc.get("nq_frozen_hash") == NQ_EXPECTED
    )
    return {"ok": ok, "document": doc}


def _should_scan(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_SCAN:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    if path.name == ".env":
        return False
    return path.is_file()


def secret_scan() -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for p in ROOT.rglob("*"):
        if not _should_scan(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "your_" in stripped.lower() or stripped.endswith("="):
                if stripped.endswith("=") and "KEY" in stripped.upper():
                    continue
            for pat, kind in SECRET_PATTERNS:
                if pat.search(line):
                    # Skip placeholders and env var references
                    if "${" in line or "os.environ" in line or "getenv" in line or "credential_env_var" in line:
                        continue
                    if re.search(r"(?i)(example|placeholder|missing|never commit)", line):
                        continue
                    findings.append(
                        {
                            "file": str(p.relative_to(ROOT)),
                            "line": str(i),
                            "type": kind,
                            "remediation": "Remove secret; use environment variable reference",
                        }
                    )
                    break
    git_history = False
    if (ROOT / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "log", "-p", "--all", "-S", "DATABENTO_API_KEY="],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.stdout and len(proc.stdout.strip()) > 50:
                # Only flag if actual values appear, not just mentions
                if re.search(r"DATABENTO_API_KEY=[a-zA-Z0-9]{8,}", proc.stdout):
                    git_history = True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return {
        "ok": len(findings) == 0 and not git_history,
        "findings": findings,
        "git_history_secret": git_history,
        "verdict": "SECRET_IN_GIT_HISTORY" if git_history else ("CLEAN" if not findings else "FINDINGS"),
    }


def run_tests() -> dict[str, Any]:
    results: dict[str, Any] = {}
    for label, cmd in (
        ("phase_tests", [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "tests_phase*.py"]),
        ("nt_bridge_tests", [sys.executable, "-m", "unittest", "tests_ninjatrader_execution"]),
    ):
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        results[label] = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "tail": (proc.stderr or proc.stdout)[-2000:],
        }
    results["ok"] = all(r["ok"] for r in results.values() if isinstance(r, dict) and "ok" in r)
    return results


def git_info() -> dict[str, Any]:
    info: dict[str, Any] = {"initialized": (ROOT / ".git").exists()}
    if not info["initialized"]:
        return info
    for key, cmd in (
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        ("commit", ["git", "rev-parse", "HEAD"]),
        ("remote_url", ["git", "remote", "get-url", "origin"]),
        ("tag", ["git", "describe", "--tags", "--exact-match"]),
    ):
        try:
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                info[key] = proc.stdout.strip()
            else:
                info[key] = None
        except FileNotFoundError:
            info[key] = None
    return info


def build_validation(*, include_tests: bool = True) -> dict[str, Any]:
    frozen = verify_frozen_hashes()
    pause = verify_pause_state()
    secrets = secret_scan()
    tests = run_tests() if include_tests else {"ok": None, "skipped": True}
    journals = journal_fingerprints()

    blockers: list[str] = []
    if not frozen["ok"]:
        blockers.append("STOP_PHASE32_FREEZE_MISMATCH")
    if not pause["ok"]:
        blockers.append("EXECUTION_NOT_PAUSED")
    if not secrets["ok"]:
        if secrets.get("git_history_secret"):
            blockers.append("SECRET_IN_GIT_HISTORY")
        else:
            blockers.append("SECRET_SCAN_FINDINGS")
    if include_tests and not tests.get("ok"):
        blockers.append("TEST_REGRESSION")

    payload = {
        "ok": len(blockers) == 0,
        "phase": 32,
        "timestamp": utc_now(),
        "project_status": "PAUSED_DEPLOYMENT_PREP",
        "execution_status": "PAUSED",
        "frozen": frozen,
        "pause": pause,
        "journals": journals,
        "secret_scan": secrets,
        "tests": tests,
        "git": git_info(),
        "prop_firm_selected": False,
        "paid_live_data": False,
        "evaluation_purchased": False,
        "remaining_blockers": blockers
        + [
            "prop firm not selected",
            "no paid live/prop market data",
            "no evaluation purchased",
            "feed equivalence pending",
        ],
        "verdict": "AITRADE_PAUSED_AND_BACKED_UP" if len(blockers) == 0 else "PHASE32_ACTION_REQUIRED",
    }
    return payload


def main() -> int:
    payload = build_validation(include_tests=True)
    VALIDATION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
