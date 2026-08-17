"""Phase 27 — GC London-session VWAP mean-reversion models (isolated family)."""

from __future__ import annotations

from gc_vwap_models import (
    ConfirmationMode,
    EntryMode,
    GCVWAPStrategyConfig,
    VwapSessionSpec,
)

STRATEGY_FAMILY = "gc_vwap_london_mean_reversion_v1"
STRATEGY_VERSION = "v1.phase27"
INSTRUMENT = "GC"

LONDON_TIMEZONE = "Europe/London"
SESSION_START_LOCAL = "08:00"
SESSION_END_LOCAL = "12:00"
NO_NEW_SETUP_AFTER_LOCAL = "11:00"
SESSION_NOTE = (
    "Research London VWAP window 08:00–12:00 Europe/London; "
    "independent falsification of Phase 25 NY VWAP 2σ reclaim/retest. "
    "Not a retune of frozen Phase 26 NY V2."
)

MIN_VWAP_BARS = 6
SIGMA_THRESHOLD = 2.0
MAX_ENTRY_BARS = 6

LONDON_SESSION = VwapSessionSpec(
    timezone=LONDON_TIMEZONE,
    start_local=SESSION_START_LOCAL,
    end_local=SESSION_END_LOCAL,
    no_new_setups_after=NO_NEW_SETUP_AFTER_LOCAL,
    min_vwap_bars=MIN_VWAP_BARS,
    event_prefix="VWAP2S_LON",
    session_note=SESSION_NOTE,
)

PHASE27_CANDIDATES: tuple[GCVWAPStrategyConfig, ...] = (
    GCVWAPStrategyConfig(
        strategy_family=STRATEGY_FAMILY,
        candidate_id="L0_NAIVE_2SIG_FADE",
        confirmation_mode=ConfirmationMode.NONE.value,
        entry_mode=EntryMode.IMMEDIATE_2SIG_CLOSE.value,
        sigma_threshold=SIGMA_THRESHOLD,
        max_entry_bars=MAX_ENTRY_BARS,
        min_vwap_bars=MIN_VWAP_BARS,
        volume_filter=False,
        extras={"role": "control"},
    ),
    GCVWAPStrategyConfig(
        strategy_family=STRATEGY_FAMILY,
        candidate_id="L1_BAND_RECLAIM_CLOSE",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        sigma_threshold=SIGMA_THRESHOLD,
        max_entry_bars=MAX_ENTRY_BARS,
        min_vwap_bars=MIN_VWAP_BARS,
        volume_filter=False,
    ),
    GCVWAPStrategyConfig(
        strategy_family=STRATEGY_FAMILY,
        candidate_id="L2_BAND_RECLAIM_2SIG_RETEST",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.FROZEN_2SIG_RETEST.value,
        sigma_threshold=SIGMA_THRESHOLD,
        max_entry_bars=MAX_ENTRY_BARS,
        min_vwap_bars=MIN_VWAP_BARS,
        volume_filter=False,
        extras={"role": "primary_hypothesis"},
    ),
    GCVWAPStrategyConfig(
        strategy_family=STRATEGY_FAMILY,
        candidate_id="L3_BAND_RECLAIM_EXT_MID",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.EXTENSION_MIDPOINT.value,
        sigma_threshold=SIGMA_THRESHOLD,
        max_entry_bars=MAX_ENTRY_BARS,
        min_vwap_bars=MIN_VWAP_BARS,
        volume_filter=False,
        extras={"role": "optional_comparison"},
    ),
)
