"""
agent/detector.py — Steam detection logic for the Sharp Money Detector.

Implements SPEC.md sections 4.1 (steam/movement detection) and 4.4 (confidence scoring).
This module is intentionally pure logic — no I/O, no database, no SSE.
The SSE consumer in agent/stream.py calls into this module.

Key design decisions (per SPEC.md):
    - Prices are decimal odds x1000 [CONFIRMED] — divide by 1000 before any math
    - One PriceWindow per (fixture_id, market_type, outcome_index) tuple
    - Persistence = consecutive ticks moving in the same direction
    - pct_change computed on implied_prob (1/decimal_odds*100), not raw Prices,
      because that's what makes economic sense for comparing across markets
    - confidence formula exactly as in SPEC.md section 4.4
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from agent.config import (
    MOVEMENT_THRESHOLD_PCT,
    PERSISTENCE_MIN_TICKS,
)

# Global hard cooldown dict to prevent duplicate signals across reconnects
_last_signal: dict[tuple[str, str, str], datetime] = {}


# ---------------------------------------------------------------------------
# Scaling — confirmed in SPEC.md 3.3 (2026-07-08 via verify_prices.py)
# ---------------------------------------------------------------------------

def price_to_decimal(raw: int) -> float:
    """Convert raw TxLINE integer price to decimal odds. Prices = decimal x 1000."""
    return raw / 1000.0


def price_to_implied_prob(raw: int) -> float:
    """Convert raw TxLINE integer price to implied probability (0-100)."""
    decimal = price_to_decimal(raw)
    if decimal <= 0:
        return 0.0
    return (1.0 / decimal) * 100.0


# ---------------------------------------------------------------------------
# Tick — one price update for a single outcome
# ---------------------------------------------------------------------------

@dataclass
class Tick:
    ts: int               # millisecond Unix timestamp from TxLINE
    raw_price: int        # raw integer from Prices[]
    decimal_odds: float   # = raw_price / 1000
    implied_prob: float   # = (1 / decimal_odds) * 100


# ---------------------------------------------------------------------------
# PriceWindow — rolling history for one (fixture, market, outcome) tuple
# ---------------------------------------------------------------------------

@dataclass
class PriceWindow:
    fixture_id:     str
    market_type:    str   # e.g. "1X2_PARTICIPANT_RESULT"
    outcome_name:   str   # e.g. "part1", "draw", "part2"
    outcome_index:  int

    # Rolling buffer of recent ticks — maxlen keeps memory bounded
    ticks: deque = field(default_factory=lambda: deque(maxlen=50))

    # Streak tracking
    direction: Optional[str] = None   # "UP" | "DOWN" | None
    streak: int = 0                   # consecutive same-direction ticks

    def add_tick(self, tick: Tick) -> None:
        self.ticks.append(tick)

    @property
    def previous_tick(self) -> Optional[Tick]:
        if len(self.ticks) < 2:
            return None
        return self.ticks[-2]

    @property
    def latest_tick(self) -> Optional[Tick]:
        if not self.ticks:
            return None
        return self.ticks[-1]


# ---------------------------------------------------------------------------
# SteamSignal — output of the detector
# ---------------------------------------------------------------------------

@dataclass
class SteamSignal:
    fixture_id:        str
    market_type:       str
    direction:         str          # "UP" | "DOWN"
    outcome_name:      str
    pct_change:        float        # % change in implied_prob
    persistence_ticks: int
    confidence_score:  float        # 0.0 – 1.0 per SPEC.md 4.4
    detected_at_ts:    int          # ms timestamp when signal fired
    match_minute:      Optional[int] = None
    is_in_play:        bool = False
    implied_prob:      float = 0.0
    decimal_odds:      float = 0.0


# ---------------------------------------------------------------------------
# Confidence scoring — exactly per SPEC.md section 4.4
# ---------------------------------------------------------------------------

def compute_confidence(
    pct_change: float,
    persistence_ticks: int,
    is_in_play: bool,
    match_minute: Optional[int],
    num_sources_agreeing: int = 1,  # 1 for pure steam; 2-3 for tri-source
) -> float:
    """
    SPEC.md 4.4:
        magnitude_score    = min(movement_pct / 15.0, 1.0)
        persistence_score  = min(persistence_ticks / 10.0, 1.0)
        source_agreement   = num_sources_agreeing / 3  (1.0 for pure steam)
        timing_score       = 1.0 pre-match | 0.6 if minute < 60 | 0.3 if minute >= 60
        confidence         = mag*0.35 + per*0.25 + src*0.25 + timing*0.15
    """
    magnitude_score    = min(abs(pct_change) / 15.0, 1.0)
    persistence_score  = min(persistence_ticks / 10.0, 1.0)
    source_score       = num_sources_agreeing / 3.0

    if not is_in_play:
        timing_score = 1.0
    elif match_minute is not None and match_minute < 60:
        timing_score = 0.6
    else:
        timing_score = 0.3

    return round(
        magnitude_score   * 0.35 +
        persistence_score * 0.25 +
        source_score      * 0.25 +
        timing_score      * 0.15,
        4,
    )


# ---------------------------------------------------------------------------
# SteamDetector — stateful detector managing all price windows
# ---------------------------------------------------------------------------

class SteamDetector:
    """
    Maintains a PriceWindow per (fixture_id, market_type, outcome_index) tuple.
    Call process_odds_tick() for each arriving OddsPayload from the SSE stream.
    Returns a list of SteamSignal objects (empty if no signal fired).

    Thresholds from config.py (SPEC.md section 6):
        MOVEMENT_THRESHOLD_PCT = 5.0
        PERSISTENCE_MIN_TICKS  = 3
    """

    def __init__(self) -> None:
        # Key: (fixture_id, market_type, outcome_index)
        self._windows: dict[tuple, PriceWindow] = {}
        # Key: (fixture_id, market_type, direction) -> divergence (abs pct_change)
        self._last_signal_divergence: dict[tuple[str, str, str], float] = {}

    def _get_window(
        self,
        fixture_id: str,
        market_type: str,
        outcome_index: int,
        outcome_name: str,
    ) -> PriceWindow:
        key = (fixture_id, market_type, outcome_index)
        if key not in self._windows:
            self._windows[key] = PriceWindow(
                fixture_id=fixture_id,
                market_type=market_type,
                outcome_name=outcome_name,
                outcome_index=outcome_index,
            )
        return self._windows[key]

    def process_odds_tick(
        self,
        fixture_id: str,
        market_type: str,
        price_names: list[str],
        prices: list[int],
        ts: int,
        in_running: bool = False,
        match_minute: Optional[int] = None,
        start_time: Optional[int] = None,
    ) -> list[SteamSignal]:
        """
        Process one OddsPayload from the SSE stream or snapshot.
        Returns any SteamSignal objects that fired on this tick.
        """
        # Filter: only process pre-match if kickoff is within 2 hours
        if not in_running and start_time is not None:
            if start_time - ts > 2 * 60 * 60 * 1000:
                return []

        # Filter: skip OVERUNDER market entirely — steam on over/under lines
        # reflects bookmaker line-adjustment, not sharp directional conviction.
        # Data: 0/24 correct across all matches (0% accuracy, -$2400 PnL).
        if market_type == "OVERUNDER_PARTICIPANT_GOALS":
            return []

        signals: list[SteamSignal] = []

        for i, (name, raw) in enumerate(zip(price_names, prices)):
            if raw <= 0:
                continue

            window = self._get_window(fixture_id, market_type, i, name)
            tick = Tick(
                ts=ts,
                raw_price=raw,
                decimal_odds=price_to_decimal(raw),
                implied_prob=price_to_implied_prob(raw),
            )
            window.add_tick(tick)

            prev = window.previous_tick
            if prev is None:
                continue  # need at least 2 ticks to measure movement

            # ── pct_change on implied_prob (economic measure) ──────────
            old_prob = prev.implied_prob
            new_prob = tick.implied_prob
            if old_prob == 0:
                continue
            pct_change = (new_prob - old_prob) / old_prob * 100

            # ── direction + streak tracking ────────────────────────────
            new_dir = "UP" if pct_change > 0 else "DOWN" if pct_change < 0 else None
            if new_dir is None:
                window.direction = None
                window.streak = 0
                continue

            if new_dir == window.direction:
                window.streak += 1
            else:
                window.direction = new_dir
                window.streak = 1

            # ── threshold + persistence gate ───────────────────────────
            if (
                abs(pct_change) >= MOVEMENT_THRESHOLD_PCT
                and window.streak >= PERSISTENCE_MIN_TICKS
            ):
                cooldown_key = (fixture_id, market_type, new_dir)
                
                # Rule 1: Hard cooldown check (30 minutes = 1800 seconds) using system time
                now = datetime.now()
                if cooldown_key in _last_signal:
                    if (now - _last_signal[cooldown_key]).total_seconds() < 1800:
                        continue

                # Rule 2: Minimum divergence threshold increase (+2 percentage points)
                last_div = self._last_signal_divergence.get(cooldown_key)
                current_div = abs(pct_change)
                if last_div is not None and current_div < (last_div + 2.0):
                    continue

                # Rule 3: Check if a signal with the same fixture_id, market, and detected_at timestamp already exists in the current batch
                if any(
                    s.fixture_id == fixture_id
                    and s.market_type == market_type
                    and s.detected_at_ts == ts
                    for s in signals
                ):
                    continue

                # Passed all filters — fire!
                _last_signal[cooldown_key] = now
                self._last_signal_divergence[cooldown_key] = current_div

                conf = compute_confidence(
                    pct_change=pct_change,
                    persistence_ticks=window.streak,
                    is_in_play=in_running,
                    match_minute=match_minute,
                )
                signals.append(SteamSignal(
                    fixture_id=fixture_id,
                    market_type=market_type,
                    direction=new_dir,
                    outcome_name=name,
                    pct_change=round(pct_change, 4),
                    persistence_ticks=window.streak,
                    confidence_score=conf,
                    detected_at_ts=ts,
                    match_minute=match_minute,
                    is_in_play=in_running,
                    implied_prob=round(tick.implied_prob, 4),
                    decimal_odds=round(tick.decimal_odds, 4),
                ))

                # Filter: skip 'draw' direction — 0% accuracy in data (4/4 losses).
                # Steam on draw probability is too noisy to be actionable.
                if name == "draw":
                    signals.pop()  # remove the signal we just added

        # opposite-direction deduplication for the same market on the same tick
        # Keep the HIGHEST confidence signal when multiple fire simultaneously.
        filtered_signals: list[SteamSignal] = []
        by_market: dict[tuple[str, str], list[SteamSignal]] = {}
        for sig in signals:
            key = (sig.fixture_id, sig.market_type)
            if key not in by_market:
                by_market[key] = []
            by_market[key].append(sig)

        for key, sig_list in by_market.items():
            if len(sig_list) > 1:
                # Multiple signals on same market same tick — keep the one with
                # highest confidence score (most persistent / biggest move).
                best = max(sig_list, key=lambda s: s.confidence_score)
                filtered_signals.append(best)
            else:
                filtered_signals.extend(sig_list)

        return filtered_signals

    def window_count(self) -> int:
        return len(self._windows)

    def reset(self) -> None:
        """Clear all state — useful between test runs."""
        self._windows.clear()
        _last_signal.clear()
        self._last_signal_divergence.clear()
