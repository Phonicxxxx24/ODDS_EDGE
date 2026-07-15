"""
agent/txline.py — TxLINE API client.

Handles auth-header injection and all HTTP calls to the TxLINE REST API.
Credentials are loaded once from tokens.json at the project root.

Endpoints used (all [CONFIRMED] in SPEC.md):
    GET /api/fixtures/snapshot?startEpochDay=N   → array of Fixture objects
    GET /api/odds/snapshot/{fixtureId}           → array of OddsPayload objects
    GET /api/scores/snapshot/{fixtureId}         → array of Scores objects
    GET /api/scores/historical/{fixtureId}       → full score history post-match
    GET /api/odds/stream                         → SSE stream (handled separately)
    GET /api/scores/stream                       → SSE stream (handled separately)
"""

import json
import os
from datetime import date, datetime, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Tokens — loaded from project root tokens.json
# ---------------------------------------------------------------------------
_TOKENS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "tokens.json")
_TXLINE_BASE = "https://txline.txodds.com"


def _load_tokens() -> dict:
    path = os.path.abspath(_TOKENS_PATH)
    with open(path, "r") as f:
        return json.load(f)


def _auth_headers() -> dict:
    """Return the two required TxLINE auth headers (SPEC.md 3.1)."""
    tokens = _load_tokens()
    return {
        "Authorization": f"Bearer {tokens['jwt']}",
        "X-Api-Token": tokens["apiToken"],
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# epoch_day helper
# ---------------------------------------------------------------------------

def today_epoch_day() -> int:
    """Days since 1970-01-01 (SPEC.md 3.2 formula)."""
    return (date.today() - date(1970, 1, 1)).days


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def fetch_fixtures(start_epoch_day: Optional[int] = None) -> list[dict]:
    """
    Fetch fixture snapshot for a given epoch day (defaults to today).
    Returns the raw list of Fixture dicts from the API.
    """
    if start_epoch_day is None:
        start_epoch_day = today_epoch_day()

    resp = requests.get(
        f"{_TXLINE_BASE}/api/fixtures/snapshot",
        headers=_auth_headers(),
        params={"startEpochDay": start_epoch_day},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Odds snapshot
# ---------------------------------------------------------------------------

def fetch_odds_snapshot(fixture_id: int | str) -> list[dict]:
    """
    Fetch latest odds for a fixture (SPEC.md 3.3).
    Returns array of OddsPayload objects.
    """
    resp = requests.get(
        f"{_TXLINE_BASE}/api/odds/snapshot/{fixture_id}",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Scores snapshot / historical
# ---------------------------------------------------------------------------

def fetch_scores_snapshot(fixture_id: int | str) -> list[dict]:
    """
    Fetch latest score per action for a fixture (SPEC.md 3.5 — live).
    """
    resp = requests.get(
        f"{_TXLINE_BASE}/api/scores/snapshot/{fixture_id}",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_scores_historical(fixture_id: int | str) -> list[dict]:
    """
    Fetch full score history for a finished fixture (SPEC.md 3.5).

    [CONFIRMED 2026-07-09]: Returns SSE format (text/event-stream), NOT JSON array.
    Each line of the form `data: {...}` contains one score update JSON object.
    Only available for fixtures that finished between 2h and 2 weeks ago.
    """
    import json as _json
    resp = requests.get(
        f"{_TXLINE_BASE}/api/scores/historical/{fixture_id}",
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()

    # Parse SSE text — each `data: {...}` line is one score update
    updates: list[dict] = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            try:
                updates.append(_json.loads(line[6:]))
            except _json.JSONDecodeError:
                pass
    return updates
