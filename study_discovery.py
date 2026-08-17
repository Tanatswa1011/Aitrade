"""Semantic study discovery — prefer name/metadata over hardcoded IDs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class StudyIdentity:
    """Semantic study identity; id is cached only for the current chart state."""

    semantic_key: str
    name: str
    study_id: Optional[str] = None
    name_pattern: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Well-known AITRADE chart studies (Phase 13 trackers).
KNOWN_STUDIES = (
    StudyIdentity(
        semantic_key="ict_sessions",
        name="ICT Sessions & Killzones",
        name_pattern=r"ICT\s*Sessions",
    ),
    StudyIdentity(
        semantic_key="luxalgo_structure",
        name="LuxAlgo Market Structure with Inducements & Sweeps",
        name_pattern=r"LuxAlgo|Market Structure with Inducements",
    ),
)


def _normalize_name(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def find_study_by_semantics(
    studies: Sequence[dict[str, Any]],
    *,
    name_pattern: str,
    preferred_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Locate a study by name regex; optionally prefer preferred_id if still present
    and still matches the semantic pattern.
    """
    pat = re.compile(name_pattern, re.I)
    matched = [
        s
        for s in studies
        if pat.search(str(s.get("name") or s.get("title") or ""))
    ]
    if not matched:
        return None
    if preferred_id:
        for s in matched:
            if str(s.get("id") or s.get("entityId") or "") == str(preferred_id):
                return s
    return matched[0]


def rediscover_studies(
    studies: Sequence[dict[str, Any]],
    *,
    known: Sequence[StudyIdentity] = KNOWN_STUDIES,
    previous: Optional[Sequence[StudyIdentity]] = None,
) -> dict[str, Any]:
    """
    Map semantic keys → current study rows.

    Documents ID churn when names remain stable.
    """
    prev_by_key = {p.semantic_key: p for p in (previous or ())}
    found: dict[str, StudyIdentity] = {}
    missing: list[str] = []
    id_changes: list[dict[str, Any]] = []

    for spec in known:
        prev = prev_by_key.get(spec.semantic_key)
        hit = find_study_by_semantics(
            studies,
            name_pattern=spec.name_pattern or re.escape(spec.name),
            preferred_id=prev.study_id if prev else None,
        )
        if hit is None:
            missing.append(spec.semantic_key)
            continue
        sid = str(hit.get("id") or hit.get("entityId") or "") or None
        name = str(hit.get("name") or hit.get("title") or spec.name)
        identity = StudyIdentity(
            semantic_key=spec.semantic_key,
            name=name,
            study_id=sid,
            name_pattern=spec.name_pattern,
            extras={"raw": {"id": sid, "name": name}},
        )
        found[spec.semantic_key] = identity
        if prev and prev.study_id and sid and prev.study_id != sid:
            id_changes.append(
                {
                    "semantic_key": spec.semantic_key,
                    "name": name,
                    "previous_id": prev.study_id,
                    "current_id": sid,
                    "semantic_stable": _normalize_name(prev.name)
                    == _normalize_name(name)
                    or bool(
                        re.search(
                            spec.name_pattern or "",
                            name,
                            re.I,
                        )
                    ),
                }
            )

    return {
        "ok": len(missing) == 0,
        "found": {k: v.to_dict() for k, v in found.items()},
        "missing": missing,
        "id_changes": id_changes,
        "study_count": len(list(studies)),
    }


def compare_study_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compare two rediscover_studies() payloads across a TF switch."""
    b = before.get("found") or {}
    a = after.get("found") or {}
    keys = sorted(set(b) | set(a))
    rows = []
    for k in keys:
        left = b.get(k) or {}
        right = a.get(k) or {}
        rows.append(
            {
                "semantic_key": k,
                "present_before": k in b,
                "present_after": k in a,
                "name_before": left.get("name"),
                "name_after": right.get("name"),
                "id_before": left.get("study_id"),
                "id_after": right.get("study_id"),
                "id_changed": (left.get("study_id") or None)
                != (right.get("study_id") or None),
                "semantic_present": k in a,
            }
        )
    return {
        "all_tracked_present_after": all(r["present_after"] for r in rows) if rows else False,
        "any_id_changed": any(r["id_changed"] and r["present_after"] for r in rows),
        "rows": rows,
    }
