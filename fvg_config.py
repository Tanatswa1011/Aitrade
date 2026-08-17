"""Default FVG detector configuration (Phase 4)."""

from __future__ import annotations

from models import FVGConfig

# v1 defaults — displacement off; first setup-linked FVG only.
DEFAULT_FVG_CONFIG = FVGConfig(
    first_only=True,
    max_bars_after_confirmation=None,
    min_gap=0.0,
    min_gap_points=0.0,
    point_size=1.0,
    require_displacement=False,
)
