"""Default entry-candidate configuration (Phase 5)."""

from __future__ import annotations

from models import EntryConfig, EntryMode

DEFAULT_ENTRY_CONFIG = EntryConfig(
    mode=EntryMode.FIRST_TOUCH.value,
    allow_full_fill=True,
    max_bars_after_fvg=None,
)

COMPARE_ENTRY_MODES = (
    EntryMode.FIRST_TOUCH.value,
    EntryMode.BOUNDARY.value,
    EntryMode.CE.value,
)
