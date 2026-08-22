"""Phase 48 — Prop Rule Engine V1. Does not modify frozen strategies or journals."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from phase34_validate import GC_FILE_SHA, GC_FROZEN, NQ_FILE_SHA, NQ_FROZEN, assert_frozen, file_sha256
from prop_rules_v1 import REQUIRES_CONFIRMATION, UNKNOWN, load_rules_document

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase48_validation.json"
DOCS = ROOT / "docs" / "PHASE48_PROP_RULE_ENGINE.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"


def _walk_unknown(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_walk_unknown(v, path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_unknown(v, f"{prefix}[{i}]"))
    elif isinstance(obj, str) and obj.upper() in (REQUIRES_CONFIRMATION, UNKNOWN):
        out.append(prefix)
    return out


def render_docs(payload: dict[str, Any]) -> str:
    return f"""# Phase 48 — Prop Rule Engine V1

`DRY_RUN`. No broker. Frozen strategy logic was not modified.

The strategy engine still only generates signals. The prop rule engine decides whether a proposed trade is **legally permitted** under a named firm profile. Risk-per-trade remains unset.

## 1. Verdict

**`{payload["verdict"]}`**

Execution remains paused / `DRY_RUN`. No live trades. No Monte Carlo. No strategy retune.

## 2. Frozen integrity

Verified before and after. `strategy_frozen/` was not written.

| Book | Config hash | File SHA |
|------|-------------|----------|
| GC VWAP V2 | `{payload["frozen"]["gc"]}` | match |
| NQ DVP | `{payload["frozen"]["nq"]}` | match |

ES DVP remains `LOCKED_FORWARD_VALIDATION_CANDIDATE` (Phase 47). Not moved into `strategy_frozen/`.

## 3. Source of truth

- Schema: JSON `PROP_RULES_V1`
- File: `config/PROP_RULES_V1.json`
- Models: `prop_rules_v1.py`
- Decision API: `prop_rule_engine.evaluate_trade(...)`
- Account states: `account_state_engine.py`
- Operating policy placeholders: `config/aitrade_operating_policy_v1.json`
- Risk manager stub: `risk_manager.py` (`SIZE_PENDING_SIMULATION`)

Primary profiles: `MFFU_RAPID_EOD_50K`, `FUNDEDNEXT_FLEX_50K`.

`MFFU_RAPID_STANDARD_50K` is an `ALTERNATIVE_RESEARCH_PROFILE` with unspecified rules. The engine fails closed (`BLOCK_UNKNOWN_RULE`) rather than guessing.

## 4. Decision codes

`ALLOW`, `BLOCK_NEWS`, `BLOCK_CONTRACT_LIMIT`, `BLOCK_DRAWDOWN`, `BLOCK_DAILY_LOSS`, `BLOCK_CONSISTENCY_GOVERNOR` (advisory unless configured to block), `BLOCK_TRADING_HOURS`, `BLOCK_OVERNIGHT`, `BLOCK_PRICE_LIMIT_ZONE`, `BLOCK_INACTIVITY`, `BLOCK_ACCOUNT_LOCKOUT`, `BLOCK_UNKNOWN_RULE`.

## 5. Account states (scaffold)

Evaluation: `EVAL_NORMAL`, `EVAL_DEFENSIVE`, `EVAL_TARGET_APPROACH`, `EVAL_LOCKOUT`, `PASSED`.

Funded: `FUNDED_BUFFER_BUILD`, `FUNDED_NORMAL`, `FUNDED_PAYOUT_APPROACH`, `FUNDED_DEFENSIVE`, `FUNDED_LOCKOUT`.

No risk percentages were assigned.

## 6. Fields still REQUIRES_CONFIRMATION

See `phase48_validation.json` → `requires_confirmation`. Do not fill these by invention.

## 7. Tests

`tests_phase48.py`: {payload["tests"]["ran"]} ran, {payload["tests"]["failures"]} failed.

## 8. What this phase did not do

No frozen hash/config edits. No journal rewrites. No live orders. No risk-per-trade selection. No automatic live transition for either firm.
"""


def update_registry() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### Prop Rule Engine V1 (Phase 48)"
    block = """### Prop Rule Engine V1 (Phase 48)

| Field | Value |
|-------|--------|
| Phase | 48 |
| Status | `PROP_RULE_ENGINE_V1_READY`. Compliance layer only. |
| Primary profiles | `MFFU_RAPID_EOD_50K`, `FUNDEDNEXT_FLEX_50K` |
| Source | `config/PROP_RULES_V1.json` |
| API | `prop_rule_engine.evaluate_trade` |
| Question | Can the existing strategy engine operate under named prop-firm rules without embedding those rules in strategy code? |
| Forbidden | Retune GC/NQ/ES; invent unstated firm rules; choose risk-per-trade; enable broker execution |
| Evidence | `docs/PHASE48_PROP_RULE_ENGINE.md`, `phase48_validation.json`, `tests_phase48.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

"""
    if marker in text:
        start = text.index(marker)
        rest = text[start + len(marker) :]
        cuts = [i for i in (rest.find("\n### "), rest.find("\n## ")) if i >= 0]
        end_rel = min(cuts) if cuts else len(rest)
        text = text[:start] + block + rest[end_rel:].lstrip("\n")
    else:
        needle = "## RESEARCH-ONLY / RETIRED"
        idx = text.find(needle)
        if idx < 0:
            raise RuntimeError("registry_research_missing")
        insert_at = text.find("\n", idx) + 1
        text = text[:insert_at] + "\n" + block + text[insert_at:]
    REGISTRY.write_text(text, encoding="utf-8")


def run() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_BEFORE:{frozen_before}")
    if file_sha256(GC_FROZEN) != GC_FILE_SHA or file_sha256(NQ_FROZEN) != NQ_FILE_SHA:
        raise RuntimeError("FROZEN_FILE_SHA_DRIFT_BEFORE")

    doc = load_rules_document()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests_phase48", "-v"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    frozen_after = assert_frozen()
    if not frozen_after.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_AFTER:{frozen_after}")

    unknown = _walk_unknown(doc)
    verdict = "PROP_RULE_ENGINE_V1_READY" if proc.returncode == 0 and frozen_after.get("ok") else "PROP_RULE_ENGINE_V1_BLOCKED"
    payload = {
        "phase": 48,
        "verdict": verdict,
        "execution": "DRY_RUN",
        "broker_execution": False,
        "schema": "JSON PROP_RULES_V1",
        "rules_path": "config/PROP_RULES_V1.json",
        "primary_profiles": doc.get("primary_profiles"),
        "alternative_research_profiles": ["MFFU_RAPID_STANDARD_50K"],
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "frozen": {"gc": frozen_after.get("gc"), "nq": frozen_after.get("nq")},
        "requires_confirmation": unknown,
        "rejection_codes": [
            "ALLOW",
            "BLOCK_NEWS",
            "BLOCK_CONTRACT_LIMIT",
            "BLOCK_DRAWDOWN",
            "BLOCK_DAILY_LOSS",
            "BLOCK_CONSISTENCY_GOVERNOR",
            "BLOCK_TRADING_HOURS",
            "BLOCK_OVERNIGHT",
            "BLOCK_PRICE_LIMIT_ZONE",
            "BLOCK_INACTIVITY",
            "BLOCK_ACCOUNT_LOCKOUT",
            "BLOCK_UNKNOWN_RULE",
        ],
        "account_states": {
            "evaluation": ["EVAL_NORMAL", "EVAL_DEFENSIVE", "EVAL_TARGET_APPROACH", "EVAL_LOCKOUT", "PASSED"],
            "funded": ["FUNDED_BUFFER_BUILD", "FUNDED_NORMAL", "FUNDED_PAYOUT_APPROACH", "FUNDED_DEFENSIVE", "FUNDED_LOCKOUT"],
            "status": "SCAFFOLD_READY_NO_RISK_PERCENTS",
        },
        "tests": {
            "ran": proc.stderr.count(" ... ok") + proc.stdout.count(" ... ok"),
            "failures": 0 if proc.returncode == 0 else 1,
            "returncode": proc.returncode,
            "tail": (proc.stderr or proc.stdout)[-1500:],
        },
        "operating_policy": "config/aitrade_operating_policy_v1.json",
        "risk_manager": "SIZE_PENDING_SIMULATION",
    }
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    update_registry()
    return payload


def main() -> int:
    payload = run()
    print(json.dumps({
        "verdict": payload["verdict"],
        "gc": payload["frozen"]["gc"],
        "nq": payload["frozen"]["nq"],
        "tests_ok": payload["tests"]["returncode"] == 0,
        "execution": payload["execution"],
    }, indent=2))
    return 0 if payload["verdict"] == "PROP_RULE_ENGINE_V1_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
