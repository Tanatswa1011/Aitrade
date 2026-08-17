"""JSONL setup journal persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from journal_models import SetupJournalRecord


def journal_path(root: str | Path = "journal") -> Path:
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p / "setups.jsonl"


def append_journal_records(
    records: Iterable[SetupJournalRecord],
    *,
    root: str | Path = "journal",
    path: Optional[Path] = None,
) -> Path:
    """
    Append records keyed by (setup_id, config_hash).

    Same setup_id under a new config_hash is a new line (rules changed).
    Duplicate (setup_id, config_hash) lines are skipped.
    """
    out = path or journal_path(root)
    existing: set[tuple[str, str]] = set()
    if out.exists():
        with out.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    existing.add((str(row.get("setup_id")), str(row.get("config_hash"))))
                except json.JSONDecodeError:
                    continue

    with out.open("a", encoding="utf-8") as fh:
        for rec in records:
            key = (rec.setup_id, rec.config_hash)
            if key in existing:
                continue
            fh.write(json.dumps(rec.to_dict(), default=str) + "\n")
            existing.add(key)
    return out


def load_journal_records(
    *, root: str | Path = "journal", path: Optional[Path] = None
) -> list[dict]:
    out = path or journal_path(root)
    if not out.exists():
        return []
    rows = []
    with out.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows
