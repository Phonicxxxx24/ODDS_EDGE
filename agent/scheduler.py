"""
agent/scheduler.py — Pre-kickoff trigger for tri-source divergence checks.

Implements the SPEC.md 3.9 / 4.2 strategy:
  "Runs once per fixture, in the 2-3hr pre-kickoff window (to conserve OddsPapi budget)"

Design:
  - scan_pre_kickoff_fixtures() is called periodically (e.g. every 5 minutes) from the
    main agent loop.
  - It checks fixtures in fixtures_tracked where start_time is within
    [ODDSPAPI_PRE_MATCH_WINDOW_HOURS, 1] hours from now.
  - For each eligible fixture that hasn't had an OddsPapi call yet (per
    oddspapi_usage table), it fetches a TxLINE odds snapshot to get the
    current Pct values, then calls run_tri_source_check().
  - Resulting DivergenceSignal objects are written to the signals table.

The caller (main agent loop) runs this on a timer — it does NOT block.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from agent.config import ODDSPAPI_PRE_MATCH_WINDOW_HOURS
from agent.database import (
    get_open_fixtures,
    get_oddspapi_usage_count,
    insert_signal,
    insert_paper_trade,
    _connect,
)
from agent.txline import fetch_odds_snapshot
from agent.oddspapi import run_tri_source_check, TXLINE_TO_ODDSPAPI


# ---------------------------------------------------------------------------
# TxLINE Pct extractor
# ---------------------------------------------------------------------------

def _extract_txline_pct_from_snapshot(fixture_id: int | str) -> Optional[dict[str, float]]:
    """
    Fetch the TxLINE odds snapshot for a fixture and return the 1X2 Pct values
    as a dict: {"part1": 43.38, "draw": 40.26, "part2": 16.37}.

    Uses the most recent OddsPayload for the 1X2_PARTICIPANT_RESULT market.
    SPEC.md 3.3 confirmed: Pct is already de-vigged; values sum to ~100 for 1X2.
    Returns None if no 1X2 data available.
    """
    try:
        entries = fetch_odds_snapshot(fixture_id)
    except Exception as e:
        print(f"[Scheduler] TxLINE snapshot error for {fixture_id}: {e}")
        return None

    # Find the most recent 1X2 tick
    best: Optional[dict] = None
    for entry in entries:
        if entry.get("SuperOddsType") != "1X2_PARTICIPANT_RESULT":
            continue
        pct_raw = entry.get("Pct", [])
        names   = entry.get("PriceNames", [])
        # Skip entries where all Pct values are "NA" (Asian Handicap lines)
        if not any(v != "NA" for v in pct_raw):
            continue
        if best is None or entry.get("Ts", 0) > best.get("Ts", 0):
            best = entry

    if best is None:
        return None

    names   = best.get("PriceNames", [])
    pct_raw = best.get("Pct", [])

    result: dict[str, float] = {}
    for name, val in zip(names, pct_raw):
        if val != "NA":
            try:
                result[name] = float(val)
            except (ValueError, TypeError):
                pass

    return result if len(result) >= 2 else None


# ---------------------------------------------------------------------------
# Check whether a fixture has already had its OddsPapi call
# ---------------------------------------------------------------------------

def _already_called_oddspapi(oddspapi_fixture_id: str) -> bool:
    """
    Return True if we already made an OddsPapi /odds call for this fixture.
    Uses the oddspapi_usage table.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM oddspapi_usage WHERE fixture_id = ?",
            (oddspapi_fixture_id,),
        ).fetchone()
        return (row[0] if row else 0) > 0


# ---------------------------------------------------------------------------
# Main scanner — call this periodically from the agent loop
# ---------------------------------------------------------------------------

def scan_pre_kickoff_fixtures() -> list:
    """
    Check all tracked fixtures and run tri-source divergence for any that:
      1. Have a mapped OddsPapi fixture ID
      2. Are in the [1hr, ODDSPAPI_PRE_MATCH_WINDOW_HOURS] pre-kickoff window
      3. Haven't had an OddsPapi call made yet
      4. Budget is not exhausted

    Returns a flat list of DivergenceSignal objects from all fixtures processed.
    """
    now_ts  = int(time.time() * 1000)  # milliseconds
    window_open_ms  = ODDSPAPI_PRE_MATCH_WINDOW_HOURS * 3600 * 1000
    window_close_ms = 1 * 3600 * 1000   # 1 hour before kickoff (lines may be pulled by then)

    fixtures = get_open_fixtures()
    if not fixtures:
        return []

    all_signals = []
    used = get_oddspapi_usage_count()

    for fx in fixtures:
        # fixtures_tracked has start_time as millisecond epoch
        start_ms = fx.get("start_time") or fx.get("StartTime")
        if start_ms is None:
            continue

        time_to_kick = start_ms - now_ts  # ms until kickoff (positive = future)

        in_window = window_close_ms <= time_to_kick <= window_open_ms
        if not in_window:
            continue

        fixture_id    = fx.get("fixture_id") or fx.get("FixtureId")
        oddspapi_id   = TXLINE_TO_ODDSPAPI.get(int(fixture_id))

        if not oddspapi_id:
            continue  # no OddsPapi mapping for this fixture

        if _already_called_oddspapi(oddspapi_id):
            continue  # already processed this fixture

        if used >= 250:
            print("[Scheduler] OddsPapi budget exhausted — stopping pre-kickoff scans.")
            break

        hours_left = round(time_to_kick / 3_600_000, 2)
        p1 = fx.get("participant1") or fx.get("Participant1", "?")
        p2 = fx.get("participant2") or fx.get("Participant2", "?")
        print(f"\n[Scheduler] PRE-KICKOFF WINDOW: {p1} vs {p2} "
              f"(fixture={fixture_id}, {hours_left}h to kickoff)")
        print(f"[Scheduler] Fetching TxLINE Pct snapshot...")

        txline_pct = _extract_txline_pct_from_snapshot(fixture_id)
        if txline_pct is None:
            print(f"[Scheduler] No TxLINE 1X2 Pct data for {fixture_id}, skipping.")
            continue

        print(f"[Scheduler] TxLINE Pct: {txline_pct}")
        print(f"[Scheduler] Running tri-source divergence check...")

        signals = run_tri_source_check(
            txline_fixture_id=str(fixture_id),
            txline_pct=txline_pct,
        )

        for sig in signals:
            sig_id = insert_signal(
                fixture_id=str(fixture_id),
                signal_type="TRI_SOURCE_DIVERGENCE",
                market="1X2_PARTICIPANT_RESULT",
                direction=sig.outcome_label,
                txline_prob=sig.txline_prob,
                pinnacle_prob=sig.pinnacle_prob,
                polymarket_prob=sig.polymarket_prob,
                divergence_pct=sig.max_divergence,
                outlier_source=sig.outlier_source,
                confidence_score=sig.confidence_score,
                participant1=p1,
                participant2=p2,
            )
            insert_paper_trade(
                signal_id=sig_id,
                stake=100.0,
                odds_taken=sig.pinnacle_odds,
            )

        all_signals.extend(signals)
        used += 1  # reflect the call we just made

    return all_signals
