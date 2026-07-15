"""
agent/results.py — Auto Result Fetching and Signal Scoring (SPEC.md section 4.6)

Implements the background loop that:
  1. Scans fixtures_tracked for UPCOMING/LIVE fixtures past their estimated end time
  2. Fetches the historical scores SSE stream via fetch_scores_historical()
  3. Determines the match winner from the "game_finalised" action entry
  4. Calls mark_fixture_finished() to store the final result
  5. Scores every signal for that fixture via score_signal() — CORRECT/INCORRECT
  6. Settles all OPEN paper trades via settle_paper_trade()

Key findings from 2026-07-09 probe (documented in SPEC.md 3.5):
  - Historical endpoint returns SSE format (text/event-stream), NOT JSON array
  - Field names are PascalCase: GameState, Action, Score, Seq, Ts
  - Score field is "Score" not "scoreSoccer" — spec had this wrong
  - Finish is detected via Action == "game_finalised" (not GameState == "F")
  - Score.Participant1.Total.Goals / Score.Participant2.Total.Goals = final tally

Entry point: run_result_loop() — call this in a background thread or scheduled job.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from agent.config import RESULT_CHECK_INTERVAL_SEC, STAKE_PER_BET
from agent.database import (
    get_open_fixtures,
    mark_fixture_finished,
    score_signal,
    settle_paper_trade,
    get_open_trades_for_fixture,
    update_fixture_status,
    _connect,
)
from agent.txline import fetch_scores_historical

# ---------------------------------------------------------------------------
# Estimated match duration threshold
# We start checking for results ESTIMATED_DURATION_MS after start_time (kickoff).
# 4 hours = 2 hours match duration + 2 hours buffer after match finishes
# ---------------------------------------------------------------------------
ESTIMATED_DURATION_MS = 4 * 60 * 60 * 1000   # 14,400,000 ms


# ---------------------------------------------------------------------------
# Core: parse the "game_finalised" entry from historical SSE updates
# Returns (p1_goals, p2_goals) or None if not found yet
# ---------------------------------------------------------------------------

def _extract_final_score(updates: list[dict]) -> Optional[tuple[int, int]]:
    """
    Search all events for a 'game_finalised' Action and extract goals.

    IMPORTANT: iterates ALL events looking for game_finalised rather than
    stopping on the first parse error — the except block previously did
    'return None' which silently aborted the whole search if Score parsing
    failed on one event.
    """
    for event in updates:
        if event.get('Action') != 'game_finalised':
            continue
        try:
            score = event['Score']
            # API omits the 'Goals' key entirely when a team scored 0 goals —
            # never raise KeyError for it, default to 0.
            p1_total = score['Participant1']['Total']
            p2_total = score['Participant2']['Total']
            p1 = p1_total.get('Goals', 0)
            p2 = p2_total.get('Goals', 0)
            return (p1, p2)
        except (KeyError, TypeError) as exc:
            # Log the actual error and keep iterating — there may be another
            # game_finalised event later in the sequence with a valid Score.
            print(f"[Results] game_finalised event found but Score parse failed: {exc}  "
                  f"event keys={list(event.keys())}  "
                  f"Score={event.get('Score')}")
            continue
    return None


def _goals_to_result(p1: int, p2: int) -> str:
    """
    Map final goals to SPEC.md result string: 'part1' | 'draw' | 'part2'.
    """
    if p1 > p2:
        return "part1"
    elif p2 > p1:
        return "part2"
    else:
        return "draw"


# ---------------------------------------------------------------------------
# Score signals and settle trades for one fixture
# ---------------------------------------------------------------------------

def _get_signals_for_fixture(fixture_id: str) -> list[dict]:
    """
    Return all signals for a fixture regardless of outcome status.
    Used to (re)score signals after result comes in.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, direction, signal_type, confidence_score, outcome
            FROM signals
            WHERE fixture_id = ?
            """,
            (fixture_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def score_and_settle_fixture(
    fixture_id: str,
    final_result: str,  # 'part1' | 'draw' | 'part2'
    participant1: str,
    participant2: str,
) -> dict:
    """
    Score all signals and settle all OPEN paper trades for a finished fixture.

    Returns a summary dict with counts.
    """
    signals = _get_signals_for_fixture(fixture_id)
    trades  = get_open_trades_for_fixture(fixture_id)

    signal_outcomes: dict[int, str] = {}  # signal_id -> outcome

    # --- Score signals ---
    correct = incorrect = 0
    for sig in signals:
        if sig["outcome"] is not None:
            continue  # already scored (idempotent)

        direction = sig["direction"]
        # STEAM signals: direction is the PriceName ("part1"/"draw"/"part2")
        # TRI_SOURCE signals: direction is "home"/"away"/"draw"
        # Map "home"→"part1" and "away"→"part2" for consistency
        mapped = direction
        if direction == "home":
            mapped = "part1"
        elif direction == "away":
            mapped = "part2"

        outcome = "CORRECT" if mapped == final_result else "INCORRECT"
        score_signal(sig["id"], outcome)
        signal_outcomes[sig["id"]] = outcome

        if outcome == "CORRECT":
            correct += 1
        else:
            incorrect += 1

    # --- Settle paper trades ---
    settled_won = settled_lost = 0
    for trade in trades:
        signal_id = trade["signal_id"]
        # Use freshly determined outcome, or re-check
        if signal_id in signal_outcomes:
            outcome = signal_outcomes[signal_id]
        else:
            # Signal was already scored — check existing outcome
            row = _get_signals_for_fixture(fixture_id)
            existing = {s["id"]: s["outcome"] for s in row}
            outcome = existing.get(signal_id)
            if outcome is None:
                continue  # cannot determine — skip

        stake      = trade["stake"]
        odds_taken = trade["odds_taken"]

        if outcome == "CORRECT":
            profit_loss = round(stake * (odds_taken - 1), 2)
            settle_paper_trade(trade["trade_id"], "WON", profit_loss)
            settled_won += 1
        else:
            profit_loss = -stake
            settle_paper_trade(trade["trade_id"], "LOST", profit_loss)
            settled_lost += 1

    return {
        "fixture_id":   fixture_id,
        "final_result": final_result,
        "signals_scored":  correct + incorrect,
        "signals_correct": correct,
        "signals_incorrect": incorrect,
        "trades_won":  settled_won,
        "trades_lost": settled_lost,
    }


# ---------------------------------------------------------------------------
# Fetch result for one fixture
# Returns final_result string or None if match is not yet finished
# ---------------------------------------------------------------------------

def fetch_final_result(fixture_id: str) -> Optional[str]:
    """
    Fetch historical scores for one fixture and determine the final result.

    Returns 'part1' | 'draw' | 'part2' if confirmed, or None if:
      - Historical endpoint not yet available (< 2h after match finishes)
      - Match not yet finished (no 'game_finalised' entry)
      - HTTP error
    """
    try:
        updates = fetch_scores_historical(fixture_id)
    except Exception as e:
        print(f"[Results] Error fetching history for {fixture_id}: {e}")
        return None

    if not updates:
        print(f"[Results] No historical data for {fixture_id} yet")
        return None

    score = _extract_final_score(updates)
    if score is None:
        # No game_finalised action found — match may still be in progress
        # Log the distinct GameStates AND full action name set for clear debugging
        states   = {u.get("GameState") for u in updates if isinstance(u, dict)}
        actions  = {u.get("Action")    for u in updates if isinstance(u, dict)}
        has_gf   = "game_finalised" in actions
        gf_events = [u for u in updates
                     if isinstance(u, dict) and u.get("Action") == "game_finalised"]
        print(f"[Results] {fixture_id}: Result not extracted from {len(updates)} events. "
              f"game_finalised present={has_gf}  GameStates={states}")
        if has_gf:
            # game_finalised exists but Score parse failed — show raw Score for debugging
            for gfe in gf_events:
                print(f"[Results]   game_finalised Score field: {gfe.get('Score')}")
        else:
            print(f"[Results]   Action names seen: {sorted(a for a in actions if a)}")
        return None

    p1, p2 = score
    result = _goals_to_result(p1, p2)
    print(f"[Results] {fixture_id}: game_finalised action found  P1={p1}  P2={p2}  -> {result}")
    return result


# ---------------------------------------------------------------------------
# One scan pass — check all eligible fixtures
# ---------------------------------------------------------------------------

def run_one_result_pass() -> list[dict]:
    """
    Check all UPCOMING/LIVE fixtures in fixtures_tracked.
    For each that is past ESTIMATED_DURATION_MS since kickoff:
      1. Fetch historical result
      2. If confirmed: mark FINISHED, score signals, settle trades
      3. Print summary

    Returns list of summary dicts for each fixture processed.
    """
    now_ms  = int(time.time() * 1000)
    results = []

    fixtures = get_open_fixtures()
    if not fixtures:
        return []

    for fx in fixtures:
        fixture_id = str(fx["fixture_id"])
        start_ms   = fx["start_time"]
        p1         = fx.get("participant1", "?")
        p2         = fx.get("participant2", "?")

        # Not yet past estimated end time
        if now_ms < start_ms + ESTIMATED_DURATION_MS:
            continue

        elapsed_h = round((now_ms - start_ms) / 3_600_000, 1)
        print(f"\n[Results] Checking {p1} vs {p2} (id={fixture_id}, {elapsed_h}h since kickoff)")

        final_result = fetch_final_result(fixture_id)

        if final_result is None:
            print(f"[Results] Result not yet confirmed for {fixture_id}")
            continue

        # Mark fixture finished in DB
        mark_fixture_finished(fixture_id, final_result)
        print(f"[Results] {p1} vs {p2}: marked FINISHED, result={final_result}")

        # Score signals and settle trades
        summary = score_and_settle_fixture(
            fixture_id=fixture_id,
            final_result=final_result,
            participant1=p1,
            participant2=p2,
        )
        results.append(summary)

        print(
            f"[Results] Scored: {summary['signals_scored']} signals "
            f"({summary['signals_correct']} correct, {summary['signals_incorrect']} incorrect)  "
            f"Settled: {summary['trades_won']} WON, {summary['trades_lost']} LOST"
        )

    return results


# ---------------------------------------------------------------------------
# Background loop — call this in a thread
# ---------------------------------------------------------------------------

def run_result_loop() -> None:
    """
    Blocking loop: check results every RESULT_CHECK_INTERVAL_SEC seconds.
    Designed to run in a background daemon thread alongside the SSE stream.

    Example usage from main.py:
        import threading
        t = threading.Thread(target=run_result_loop, daemon=True)
        t.start()
    """
    print(f"[Results] Loop started (interval={RESULT_CHECK_INTERVAL_SEC}s)")
    while True:
        try:
            run_one_result_pass()
        except Exception as e:
            print(f"[Results] Unexpected error in result loop: {e}")
        time.sleep(RESULT_CHECK_INTERVAL_SEC)
