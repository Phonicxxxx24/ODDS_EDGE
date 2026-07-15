"""
agent/database.py — SQLite persistence layer for the Sharp Money Detector.

Schema and function signatures are defined exactly as in SPEC.md section 5
and the CHECKPOINT 1 requirement.  No deviations from the spec.

Tables
------
signals          — every detected signal (STEAM or TRI_SOURCE_DIVERGENCE)
paper_trades     — paper-trading positions linked to signals
fixtures_tracked — fixtures being monitored, with status lifecycle
oddspapi_usage   — log of every OddsPapi call (budget guard)

Functions
---------
init_db()
insert_signal()
insert_paper_trade()
log_oddspapi_call()
get_oddspapi_usage_count()
get_open_fixtures()
mark_fixture_finished()
score_signal()
settle_paper_trade()
get_all_signals()
get_pnl_summary()
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Database location — project root / sharp_detector.db
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sharp_detector.db")


def _connect() -> sqlite3.Connection:
    """Open a connection with row_factory so rows behave like dicts."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")   # safer for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Create all tables if they don't already exist.
    Safe to call on every startup — uses IF NOT EXISTS.
    """
    with _connect() as conn:
        conn.executescript("""
            -- ── signals ──────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS signals (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id           TEXT    NOT NULL,
                competition          TEXT,
                participant1         TEXT,
                participant2         TEXT,
                signal_type          TEXT    NOT NULL,   -- STEAM | TRI_SOURCE_DIVERGENCE
                market               TEXT,               -- e.g. 1X2_PARTICIPANT_RESULT
                txline_prob          REAL,               -- nullable
                pinnacle_prob        REAL,               -- nullable, tri-source only
                polymarket_prob      REAL,               -- nullable, tri-source only
                pct_change           REAL,               -- steam signals
                divergence_pct       REAL,               -- tri-source signals
                direction            TEXT,               -- which outcome the signal favors
                outlier_source       TEXT,               -- nullable
                confidence_score     REAL,               -- 0-1
                persistence_ticks    INTEGER,
                detected_at          TEXT    NOT NULL,   -- ISO timestamp
                match_minute         INTEGER,            -- nullable (in-play)
                ai_commentary        TEXT,
                cross_market_group_id TEXT,              -- nullable
                outcome              TEXT,               -- CORRECT | INCORRECT | NULL
                scored_at            TEXT                -- nullable
            );

            -- ── paper_trades ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS paper_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id   INTEGER NOT NULL REFERENCES signals(id),
                stake       REAL    NOT NULL,
                odds_taken  REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | WON | LOST
                profit_loss REAL,                             -- NULL until settled
                settled_at  TEXT                              -- nullable ISO timestamp
            );

            -- ── fixtures_tracked ──────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS fixtures_tracked (
                fixture_id             TEXT PRIMARY KEY,
                competition            TEXT,
                participant1           TEXT,
                participant2           TEXT,
                start_time             INTEGER NOT NULL,  -- Unix ms
                status                 TEXT    NOT NULL DEFAULT 'UPCOMING',  -- UPCOMING | LIVE | FINISHED
                final_result           TEXT,              -- nullable: 'part1' | 'draw' | 'part2'
                last_oddspapi_call_at  TEXT,              -- nullable ISO timestamp
                last_checked_at        TEXT               -- nullable ISO timestamp
            );

            -- ── oddspapi_usage ─────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS oddspapi_usage (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at                  TEXT    NOT NULL,  -- ISO timestamp
                endpoint                   TEXT    NOT NULL,
                fixture_id                 TEXT,
                requests_remaining_estimate INTEGER
            );
        """)


# ---------------------------------------------------------------------------
# insert_signal
# ---------------------------------------------------------------------------

def insert_signal(
    fixture_id: str,
    signal_type: str,
    detected_at: Optional[str] = None,
    competition: Optional[str] = None,
    participant1: Optional[str] = None,
    participant2: Optional[str] = None,
    market: Optional[str] = None,
    txline_prob: Optional[float] = None,
    pinnacle_prob: Optional[float] = None,
    polymarket_prob: Optional[float] = None,
    pct_change: Optional[float] = None,
    divergence_pct: Optional[float] = None,
    direction: Optional[str] = None,
    outlier_source: Optional[str] = None,
    confidence_score: Optional[float] = None,
    persistence_ticks: Optional[int] = None,
    match_minute: Optional[int] = None,
    ai_commentary: Optional[str] = None,
    cross_market_group_id: Optional[str] = None,
) -> int:
    """
    Insert a new signal row.  Returns the new signal's integer id.
    detected_at defaults to the current UTC ISO timestamp if not supplied.
    """
    if detected_at is None:
        detected_at = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                fixture_id, competition, participant1, participant2,
                signal_type, market,
                txline_prob, pinnacle_prob, polymarket_prob,
                pct_change, divergence_pct,
                direction, outlier_source,
                confidence_score, persistence_ticks,
                detected_at, match_minute, ai_commentary,
                cross_market_group_id,
                outcome, scored_at
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?,
                NULL, NULL
            )
            """,
            (
                fixture_id, competition, participant1, participant2,
                signal_type, market,
                txline_prob, pinnacle_prob, polymarket_prob,
                pct_change, divergence_pct,
                direction, outlier_source,
                confidence_score, persistence_ticks,
                detected_at, match_minute, ai_commentary,
                cross_market_group_id,
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# insert_paper_trade
# ---------------------------------------------------------------------------

def insert_paper_trade(
    signal_id: int,
    stake: float,
    odds_taken: float,
) -> int:
    """
    Open a new paper trade for a given signal.
    status starts as 'OPEN'; profit_loss and settled_at are NULL.
    Returns the new trade's integer id.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades (signal_id, stake, odds_taken, status)
            VALUES (?, ?, ?, 'OPEN')
            """,
            (signal_id, stake, odds_taken),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# log_oddspapi_call
# ---------------------------------------------------------------------------

def log_oddspapi_call(
    endpoint: str,
    fixture_id: Optional[str] = None,
    requests_remaining_estimate: Optional[int] = None,
) -> None:
    """
    Log an OddsPapi API call immediately.
    Per SPEC.md: "Query this before making any new call to confirm budget remains."
    """
    called_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO oddspapi_usage (called_at, endpoint, fixture_id, requests_remaining_estimate)
            VALUES (?, ?, ?, ?)
            """,
            (called_at, endpoint, fixture_id, requests_remaining_estimate),
        )


# ---------------------------------------------------------------------------
# get_oddspapi_usage_count
# ---------------------------------------------------------------------------

def get_oddspapi_usage_count() -> int:
    """
    Return the total number of OddsPapi calls logged so far.
    Compare against ODDSPAPI_TOTAL_BUDGET (250) before making any new call.
    """
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM oddspapi_usage").fetchone()
        return row[0]


# ---------------------------------------------------------------------------
# get_open_fixtures
# ---------------------------------------------------------------------------

def get_open_fixtures() -> list[dict]:
    """
    Return all fixtures with status UPCOMING or LIVE.
    Used by the auto-result-fetching loop (SPEC.md 4.6).
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM fixtures_tracked
            WHERE status IN ('UPCOMING', 'LIVE')
            ORDER BY start_time ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# mark_fixture_finished
# ---------------------------------------------------------------------------

def mark_fixture_finished(fixture_id: str, final_result: str) -> None:
    """
    Mark a fixture as FINISHED and store its final result.
    final_result should be one of: 'part1', 'draw', 'part2'.
    """
    last_checked_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE fixtures_tracked
            SET status = 'FINISHED',
                final_result = ?,
                last_checked_at = ?
            WHERE fixture_id = ?
            """,
            (final_result, last_checked_at, fixture_id),
        )


# ---------------------------------------------------------------------------
# score_signal
# ---------------------------------------------------------------------------

def score_signal(signal_id: int, outcome: str) -> None:
    """
    Record whether a signal was CORRECT or INCORRECT.
    outcome must be 'CORRECT' or 'INCORRECT'.
    scored_at is set to the current UTC timestamp.
    """
    scored_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE signals
            SET outcome = ?, scored_at = ?
            WHERE id = ?
            """,
            (outcome, scored_at, signal_id),
        )


# ---------------------------------------------------------------------------
# settle_paper_trade
# ---------------------------------------------------------------------------

def settle_paper_trade(trade_id: int, status: str, profit_loss: float) -> None:
    """
    Settle a paper trade as WON or LOST.
    profit_loss formula per SPEC.md 4.5:
        WON  → stake * (odds_taken - 1)
        LOST → -stake
    """
    settled_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE paper_trades
            SET status = ?, profit_loss = ?, settled_at = ?
            WHERE id = ?
            """,
            (status, profit_loss, settled_at, trade_id),
        )


# ---------------------------------------------------------------------------
# get_all_signals
# ---------------------------------------------------------------------------

def get_all_signals(limit: int = 100, offset: int = 0) -> list[dict]:
    """
    Return all signals ordered by detected_at DESC, with optional pagination.
    Used by the Flask /api/signals endpoint.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signals
            ORDER BY detected_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# get_pnl_summary
# ---------------------------------------------------------------------------

def get_pnl_summary() -> list[dict]:
    """
    Return the cumulative P&L time series for the equity curve.
    Each row: { settled_at, profit_loss, cumulative_pnl }
    Only settled (WON/LOST) trades are included, ordered by signals.detected_at ASC.
    Used by the Flask /api/pnl endpoint.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.detected_at AS settled_at,
                pt.profit_loss,
                SUM(pt.profit_loss) OVER (
                    ORDER BY s.detected_at ASC
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_pnl,
                s.fixture_id,
                s.participant1,
                s.participant2,
                s.signal_type,
                s.direction,
                s.confidence_score
            FROM paper_trades pt
            JOIN signals s ON s.id = pt.signal_id
            WHERE pt.status IN ('WON', 'LOST')
            ORDER BY s.detected_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Convenience helpers used elsewhere in the agent
# ---------------------------------------------------------------------------

def upsert_fixture(
    fixture_id: str,
    competition: str,
    participant1: str,
    participant2: str,
    start_time: int,
) -> None:
    """
    Insert a fixture into fixtures_tracked if it doesn't exist yet.
    Silently skips if already present (IGNORE conflict strategy).
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO fixtures_tracked
                (fixture_id, competition, participant1, participant2, start_time, status)
            VALUES (?, ?, ?, ?, ?, 'UPCOMING')
            """,
            (fixture_id, competition, participant1, participant2, start_time),
        )


def update_fixture_status(fixture_id: str, status: str) -> None:
    """Update the live status of a tracked fixture (UPCOMING → LIVE → FINISHED)."""
    last_checked_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE fixtures_tracked
            SET status = ?, last_checked_at = ?
            WHERE fixture_id = ?
            """,
            (status, last_checked_at, fixture_id),
        )


def get_open_trades_for_fixture(fixture_id: str) -> list[dict]:
    """
    Return all OPEN paper trades for signals belonging to a given fixture.
    Used by the auto-result-fetching loop to settle trades after a result.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT pt.id AS trade_id, pt.stake, pt.odds_taken,
                   s.id AS signal_id, s.direction, s.outcome
            FROM paper_trades pt
            JOIN signals s ON s.id = pt.signal_id
            WHERE s.fixture_id = ? AND pt.status = 'OPEN'
            """,
            (fixture_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# get_signal_by_id  (Checkpoint 6 — commentary)
# ---------------------------------------------------------------------------

def get_signal_by_id(signal_id: int) -> Optional[dict]:
    """
    Return a single signal row as a dict, or None if not found.
    Used by the commentary endpoint to fetch the signal for prompt building
    and to check whether ai_commentary has already been generated.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM signals WHERE id = ?",
            (signal_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# update_signal_commentary  (Checkpoint 6 — commentary)
# ---------------------------------------------------------------------------

def update_signal_commentary(signal_id: int, commentary: str) -> None:
    """
    Save a Gemini-generated commentary string to signals.ai_commentary.
    Called by the POST /api/signals/<id>/commentary Flask route after a
    successful Gemini call.  Idempotent — safe to call multiple times,
    always overwrites with the latest text.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE signals SET ai_commentary = ? WHERE id = ?",
            (commentary, signal_id),
        )
