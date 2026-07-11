"""
seed_fixtures.py — one-shot script to populate fixtures_tracked from TxLINE.

Fetches fixtures for today + 6 days ahead, filters to World Cup (CompetitionId=72)
and International Friendlies, then upserts them into the DB so the API and
frontend have real data immediately.

Run from the sharp-detector/ directory:
    python seed_fixtures.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.txline import fetch_fixtures, today_epoch_day
from agent.database import init_db, upsert_fixture, _connect
from datetime import datetime, timezone

WORLD_CUP_COMPETITION_ID = 72

def seed(days_ahead: int = 6):
    init_db()
    base_day = today_epoch_day()
    upserted = 0

    for offset in range(days_ahead + 1):
        epoch_day = base_day + offset
        print(f"\nFetching epoch_day={epoch_day} (day +{offset}) …", end=" ", flush=True)

        try:
            fixtures = fetch_fixtures(epoch_day)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        if not fixtures:
            print("no fixtures returned.")
            continue

        day_count = 0
        for fx in fixtures:
            # Only keep World Cup — CompetitionId 72 confirmed in SPEC.md 3.2
            if fx.get("CompetitionId") != WORLD_CUP_COMPETITION_ID:
                continue

            upsert_fixture(
                fixture_id=str(fx["FixtureId"]),
                competition=fx.get("Competition", "Unknown"),
                participant1=fx.get("Participant1", ""),
                participant2=fx.get("Participant2", ""),
                start_time=fx.get("StartTime", 0),
            )
            day_count += 1

        print(f"found {len(fixtures)} total, kept {day_count} World Cup fixtures.")
        upserted += day_count

    print(f"\nDone. {upserted} World Cup fixtures upserted into fixtures_tracked.")

    # Show what's in DB now
    with _connect() as conn:
        rows = conn.execute(
            "SELECT fixture_id, participant1, participant2, start_time, status "
            "FROM fixtures_tracked ORDER BY start_time ASC"
        ).fetchall()

    print("-" * 70)
    print(f"{'Fixture ID':<12} {'Match':<40} {'Kickoff (UTC)':<25} {'Status'}")
    print("-" * 70)
    for r in rows:
        kick = datetime.fromtimestamp(r['start_time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        match = f"{r['participant1']} vs {r['participant2']}"
        print(f"{r['fixture_id']:<12} {match:<40} {kick:<25} {r['status']}")
    print("-" * 70)
    print(f"Total: {len(rows)} fixtures in DB\n")


if __name__ == "__main__":
    seed()
