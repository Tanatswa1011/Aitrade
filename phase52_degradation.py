"""Phase 52 — transparent FAST→PROTECTED demotion. No ML. No auto-reactivate on one winner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from phase52_policy import FROZEN_AVG_LOSS_R, FROZEN_AVG_WIN_R, FROZEN_E_R, FROZEN_WR

MIN_SAMPLE = 20
ROLL = 30
WARN_ER_MULT = 0.50
HARD_ER = 0.0
WARN_WR_DROP = 0.08
HARD_WR_DROP = 0.12
HARD_STREAK = 5
WARN_STREAK = 4
HARD_FLIP_PROXY = 0.08  # realized WR collapse vs frozen as flip proxy
RECOVERY_TRADES = 40
RECOVERY_ER = 0.5 * FROZEN_E_R
RECOVERY_WR = FROZEN_WR - 0.04


@dataclass
class DegradationMonitor:
    r_hist: list[float] = field(default_factory=list)
    warning: bool = False
    demoted: bool = False
    demote_trade_index: int = -1
    recovery_ok_count: int = 0

    def observe(self, r: float) -> dict[str, Any]:
        self.r_hist.append(float(r))
        n = len(self.r_hist)
        window = self.r_hist[-ROLL:]
        wr = sum(1 for x in window if x > 0) / len(window)
        er = sum(window) / len(window)
        wins = [x for x in window if x > 0]
        losses = [x for x in window if x <= 0]
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = sum(losses) / len(losses) if losses else 0.0
        streak = 0
        for x in reversed(self.r_hist):
            if x < 0:
                streak += 1
            else:
                break
        ready = n >= MIN_SAMPLE
        warn = False
        hard = False
        reasons = []
        if ready:
            if er < WARN_ER_MULT * FROZEN_E_R:
                warn = True
                reasons.append("rolling_E[R]_below_50pct_frozen")
            if wr < FROZEN_WR - WARN_WR_DROP:
                warn = True
                reasons.append("rolling_WR_drop_8pp")
            if streak >= WARN_STREAK:
                warn = True
                reasons.append("loss_streak_4")
            if avg_w and avg_w < 0.70 * FROZEN_AVG_WIN_R:
                warn = True
                reasons.append("avg_winner_degraded")
            if avg_l and avg_l < 1.20 * FROZEN_AVG_LOSS_R:  # more negative
                warn = True
                reasons.append("avg_loser_expanded")
            # Hard: require a confirming signal so healthy frozen variance does not strip FAST.
            if er < HARD_ER and (wr < FROZEN_WR - WARN_WR_DROP or streak >= WARN_STREAK):
                hard = True
                reasons.append("rolling_E[R]<0_with_WR_or_streak")
            if wr < FROZEN_WR - HARD_WR_DROP and er < WARN_ER_MULT * FROZEN_E_R:
                hard = True
                reasons.append("rolling_WR_drop_12pp")
            if streak >= HARD_STREAK:
                hard = True
                reasons.append("loss_streak_5")
            if wr <= FROZEN_WR - 0.18:
                hard = True
                reasons.append("WR_collapse_flip_proxy")
        self.warning = warn and not hard
        if hard and not self.demoted:
            self.demoted = True
            self.demote_trade_index = n
            self.recovery_ok_count = 0
        if self.demoted:
            # hysteresis: FAST never returns on one winner
            rec = ready and er >= RECOVERY_ER and wr >= RECOVERY_WR and streak == 0
            self.recovery_ok_count = self.recovery_ok_count + 1 if rec else 0
            if self.recovery_ok_count >= RECOVERY_TRADES and (n - self.demote_trade_index) >= RECOVERY_TRADES:
                # still do not return to FAST during an evaluation — stay PROTECTED
                pass
        return {
            "n": n,
            "wr": wr if window else None,
            "er": er if window else None,
            "streak": streak,
            "warning": self.warning,
            "demoted": self.demoted,
            "reasons": reasons,
            "avg_win": avg_w,
            "avg_loss": avg_l,
        }
