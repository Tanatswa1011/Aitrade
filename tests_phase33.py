"""Phase 33 look-ahead / blackout tests for post-news macro research."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from models import Bar
from nq_post_news_engine import (
    assert_no_blackout_actions,
    classify_regime,
    local_ts,
    replay_family,
    snapshot_event,
)
from nq_post_news_models import MacroEvent, PostNewsStrategyConfig

NY = ZoneInfo("America/New_York")


def _bar(t, o, h, l, c, v=1000.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def _synthetic_day(spike_at_1000: bool = False) -> tuple[list[Bar], list[Bar], MacroEvent]:
    """Build a 2024-03-08 (NFP Friday) 1m/5m path with a bullish 08:30-08:35 impulse."""
    bars_1m: list[Bar] = []
    # Overnight 18:00 previous day through 16:00
    t0 = _ts(2024, 3, 7, 18, 0)
    px = 18000.0
    for i in range(0, 22 * 60):  # 18:00 -> 16:00 next day
        t = t0 + i * 60
        dt = datetime.fromtimestamp(t, tz=NY)
        o = px
        # Default quiet
        h = px + 2
        l = px - 2
        c = px + 0.25
        if dt.hour == 8 and dt.minute == 29:
            c = 18000.0
            h, l = 18002.0, 17998.0
            px = 18000.0
        elif dt.hour == 8 and 30 <= dt.minute <= 34:
            # Bullish event impulse: +40 pts over 5 minutes
            c = 18000.0 + (dt.minute - 29) * 8
            h = c + 2
            l = 18000.0
            px = c
        elif spike_at_1000 and dt.hour == 10 and dt.minute == 0:
            c = px + 200
            h = c
            l = px
            px = c
        else:
            px = c
        bars_1m.append(_bar(t, o, h, l, c, v=50))
    # 5m from 1m
    buckets: dict[int, list[Bar]] = {}
    for b in bars_1m:
        dt = datetime.fromtimestamp(int(b.time), tz=NY)
        floored = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(int(floored.timestamp()), []).append(b)
    bars_5m = []
    for key in sorted(buckets):
        chunk = buckets[key]
        bars_5m.append(
            _bar(
                key,
                chunk[0].open,
                max(x.high for x in chunk),
                min(x.low for x in chunk),
                chunk[-1].close,
                sum(float(x.volume or 0) for x in chunk),
            )
        )
    event = MacroEvent(
        event_id="NFP|2024-03-08|08:30",
        event_family="NFP",
        publication_date="2024-03-08",
        release_local="08:30",
        source="synthetic",
        source_url="synthetic",
        actuals={"nfp_change": 200000.0},
        consensus={"status": "UNAVAILABLE"},
        surprise={"status": "UNAVAILABLE_NO_CONSENSUS"},
        data_quality={"synthetic": True},
    )
    return bars_1m, bars_5m, event


class LookAheadTests(unittest.TestCase):
    def test_0830_5m_bar_not_used_before_0835(self):
        bars_1m, bars_5m, event = _synthetic_day()
        cfg = PostNewsStrategyConfig()
        snap = snapshot_event(event, bars_1m, bars_5m, instrument="NQ", cfg=cfg)
        # Event close must equal 08:34 1m close, which is known at 08:35.
        t_834 = _ts(2024, 3, 8, 8, 34)
        bar_834 = next(b for b in bars_1m if int(b.time) == t_834)
        self.assertAlmostEqual(float(snap.event_close), float(bar_834.close))
        self.assertEqual(snap.blackout_end_ts, _ts(2024, 3, 8, 8, 35))
        self.assertGreaterEqual(snap.blackout_end_ts, t_834 + 60)

    def test_future_1000_spike_does_not_change_0835_regime(self):
        a1, a5, event = _synthetic_day(spike_at_1000=False)
        b1, b5, _ = _synthetic_day(spike_at_1000=True)
        cfg = PostNewsStrategyConfig()
        sa = snapshot_event(event, a1, a5, instrument="NQ", cfg=cfg)
        sb = snapshot_event(event, b1, b5, instrument="NQ", cfg=cfg)
        self.assertEqual(sa.regime, sb.regime)
        self.assertEqual(sa.event_close, sb.event_close)
        self.assertEqual(sa.event_high, sb.event_high)

    def test_no_entries_inside_blackout(self):
        bars_1m, bars_5m, event = _synthetic_day()
        cfg = PostNewsStrategyConfig(entry_family="C_5M_CLOSE_CONFIRM", delay_minutes=5)
        snap = snapshot_event(event, bars_1m, bars_5m, instrument="NQ", cfg=cfg)
        trade = replay_family(snap, bars_1m, bars_5m, cfg)
        if trade is not None:
            self.assertFalse(snap.blackout_start_ts <= trade.entry_timestamp < snap.blackout_end_ts)
            chk = assert_no_blackout_actions([trade], [snap])
            self.assertTrue(chk["ok"], chk)

    def test_neutral_when_move_tiny(self):
        cfg = PostNewsStrategyConfig(min_close_move_atr=50.0, min_range_atr=50.0)
        regime = classify_regime(1.0, 2.0, 20.0, 1.0, 18001.0, 18002.0, 17999.0, cfg)
        self.assertEqual(regime, "MACRO_NEUTRAL")

    def test_delay_10_does_not_use_pre_0840_close_as_entry(self):
        bars_1m, bars_5m, event = _synthetic_day()
        cfg = PostNewsStrategyConfig(entry_family="C_5M_CLOSE_CONFIRM", delay_minutes=10)
        snap = snapshot_event(event, bars_1m, bars_5m, instrument="NQ", cfg=cfg)
        trade = replay_family(snap, bars_1m, bars_5m, cfg)
        if trade is not None:
            self.assertGreaterEqual(trade.entry_timestamp, _ts(2024, 3, 8, 8, 40))


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        import hashlib
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent
        gc = json.loads((root / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((root / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        # File bytes also unchanged vs Phase 32 snapshot.
        self.assertEqual(
            hashlib.sha256((root / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_bytes()).hexdigest(),
            "12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f",
        )
        self.assertEqual(
            hashlib.sha256((root / "strategy_frozen" / "nq_dvp_phase30.json").read_bytes()).hexdigest(),
            "34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541",
        )


if __name__ == "__main__":
    unittest.main()
