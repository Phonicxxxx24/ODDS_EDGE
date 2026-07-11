"""
agent/config.py — Configuration constants for the Sharp Money Detector.

All values are the starting points defined in SPEC.md section 6.
Document any changes with your reasoning — judges may ask why.
"""

# ── TxLINE steam detection ────────────────────────────────────────────────────
MOVEMENT_THRESHOLD_PCT = 5.0      # % price move that triggers a steam signal
PERSISTENCE_MIN_TICKS  = 3        # consecutive same-direction ticks required

# ── Tri-source divergence detection ──────────────────────────────────────────
DIVERGENCE_THRESHOLD_PCT = 5.0    # % spread between max/min of 3 sources

# ── Paper trading ─────────────────────────────────────────────────────────────
STAKE_PER_BET = 100               # flat stake per signal in virtual units

# ── Auto result fetching ─────────────────────────────────────────────────────
RESULT_CHECK_INTERVAL_SEC = 60    # how often to poll for match results

# ── Stream reconnection ──────────────────────────────────────────────────────
RECONNECT_DELAY_SEC = 5           # seconds to wait before reconnecting SSE

# ── OddsPapi budget management ───────────────────────────────────────────────
ODDSPAPI_TOTAL_BUDGET        = 250  # total requests available for the account
ODDSPAPI_PRE_MATCH_WINDOW_HOURS = 3 # hours before kickoff to make the single call
