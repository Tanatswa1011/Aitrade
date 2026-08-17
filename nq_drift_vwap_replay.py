"""Phase 29 DVP replay helper (trades.jsonl already written by phase29_validate)."""

from __future__ import annotations

from typing import Sequence

from nq_drift_vwap_engine import replay_all_days
from nq_drift_vwap_models import DVP_ORIGINAL, DVPTrade
from models import Bar


def replay_dvp_original(
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
) -> list[DVPTrade]:
    trades, _guard = replay_all_days(bars_1m, bars_5m, bars_15m, DVP_ORIGINAL)
    return trades
