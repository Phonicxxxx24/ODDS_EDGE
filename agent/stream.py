r"""
agent/stream.py — TxLINE SSE stream consumer for the Sharp Money Detector.

Connects to /api/odds/stream with confirmed auth headers (SPEC.md 3.1, 3.4).
Feeds each OddsPayload event into SteamDetector and logs any fired signals
to terminal. Does NOT write to the database — that is a separate step.

Usage (from c:/txodds/sharp-detector/):
    python agent/stream.py [fixture_id]

    If fixture_id is given, only that fixture's odds are streamed.
    Without it, all World Cup fixtures on the free tier are streamed.

Reconnection (SPEC.md 3.4):
    On disconnect, waits RECONNECT_DELAY_SEC seconds then reconnects,
    sending Last-Event-ID: <last_seen_id> header for seamless resumption.
"""

import json
import sys
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# ── project root on sys.path ───────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.txline import _auth_headers, _TXLINE_BASE
from agent.detector import SteamDetector, SteamSignal
from agent.config import RECONNECT_DELAY_SEC


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------

def _parse_sse_stream(response: requests.Response):
    """
    Yields (event_type, event_id, data_str) tuples from a raw SSE response.
    Handles multi-line data fields and heartbeats.
    """
    event_type = "message"
    event_id   = None
    data_lines: list[str] = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        if raw_line == "":
            # Blank line = dispatch event
            if data_lines:
                yield event_type, event_id, "\n".join(data_lines)
            event_type = "message"
            data_lines = []
            continue

        if raw_line.startswith("event:"):
            event_type = raw_line[6:].strip()
        elif raw_line.startswith("id:"):
            event_id = raw_line[3:].strip()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line[5:].strip())
        # comment lines (":") are silently ignored


# ---------------------------------------------------------------------------
# Signal formatter — terminal output only (no DB writes)
# ---------------------------------------------------------------------------

def _fmt_signal(sig: SteamSignal, fixture_label: str) -> str:
    now_utc = datetime.fromtimestamp(sig.detected_at_ts / 1000, tz=timezone.utc)
    ts_str  = now_utc.strftime("%H:%M:%S UTC")
    conf_pct = int(sig.confidence_score * 100)
    return (
        f"\n  {'='*64}\n"
        f"  STEAM SIGNAL  [{ts_str}]  conf={conf_pct}%\n"
        f"  Fixture  : {fixture_label}\n"
        f"  Market   : {sig.market_type}\n"
        f"  Outcome  : {sig.outcome_name}  -->  {sig.direction}\n"
        f"  Δ prob   : {sig.pct_change:+.3f}%  (streak={sig.persistence_ticks} ticks)\n"
        f"  In-play  : {'YES min=' + str(sig.match_minute) if sig.is_in_play else 'NO (pre-match)'}\n"
        f"  {'='*64}"
    )


# ---------------------------------------------------------------------------
# Main stream loop
# ---------------------------------------------------------------------------

class StreamConsumer:
    """
    Connects to TxLINE /api/odds/stream, parses OddsPayload events,
    feeds them into SteamDetector, and prints any fired signals.

    NOTE: This version logs to terminal only. DB writes are added in the
    next step once stream → detector → signal is confirmed end-to-end.
    """

    def __init__(self, fixture_id: Optional[str] = None) -> None:
        self.fixture_id = fixture_id
        self.detector   = SteamDetector()
        self._last_event_id: Optional[str] = None
        self._fixture_labels: dict[str, str] = {}  # fixture_id -> "TeamA vs TeamB"

        # stats
        self.events_received = 0
        self.heartbeats      = 0
        self.signals_fired   = 0

    def _build_url(self) -> str:
        url = f"{_TXLINE_BASE}/api/odds/stream"
        if self.fixture_id:
            url += f"?fixtureId={self.fixture_id}"
        return url

    def _build_headers(self) -> dict:
        h = _auth_headers()
        h["Accept"] = "text/event-stream"
        h["Cache-Control"] = "no-cache"
        if self._last_event_id:
            h["Last-Event-ID"] = self._last_event_id
        return h

    def _label(self, fixture_id: str) -> str:
        return self._fixture_labels.get(fixture_id, f"fixture:{fixture_id}")

    def _process_payload(self, payload: dict) -> None:
        fixture_id   = str(payload.get("FixtureId", ""))
        market_type  = payload.get("SuperOddsType", "")
        price_names  = payload.get("PriceNames", [])
        prices       = payload.get("Prices", [])
        ts           = payload.get("Ts", int(time.time() * 1000))
        in_running   = payload.get("InRunning", False)

        # Build label if we have participant data (rarely included in stream)
        p1 = payload.get("Participant1", "")
        p2 = payload.get("Participant2", "")
        if p1 and p2 and fixture_id:
            self._fixture_labels[fixture_id] = f"{p1} vs {p2}"

        if not fixture_id or not prices or not price_names:
            return

        # Get start_time from fixtures_tracked database table
        start_time = None
        if fixture_id:
            try:
                import sqlite3
                db_path = os.path.join(_ROOT, "sharp_detector.db")
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute("SELECT start_time FROM fixtures_tracked WHERE fixture_id = ?", (fixture_id,)).fetchone()
                    if row:
                        start_time = row[0]
            except Exception:
                pass

        # Feed into detector
        signals = self.detector.process_odds_tick(
            fixture_id=fixture_id,
            market_type=market_type,
            price_names=price_names,
            prices=prices,
            ts=ts,
            in_running=in_running,
            start_time=start_time,
        )

        for sig in signals:
            self.signals_fired += 1
            print(_fmt_signal(sig, self._label(fixture_id)))

            # Save to database
            try:
                import sqlite3
                db_path = os.path.join(_ROOT, "sharp_detector.db")
                comp, p1, p2 = None, None, None
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT participant1, participant2, competition FROM fixtures_tracked WHERE fixture_id = ?", (fixture_id,)).fetchone()
                    if row:
                        comp = row["competition"]
                        p1 = row["participant1"]
                        p2 = row["participant2"]

                from agent.database import insert_signal, insert_paper_trade
                
                sig_id = insert_signal(
                    fixture_id=fixture_id,
                    competition=comp,
                    participant1=p1,
                    participant2=p2,
                    signal_type="STEAM",
                    market=sig.market_type,
                    txline_prob=sig.implied_prob,
                    pct_change=sig.pct_change,
                    direction=sig.outcome_name,
                    confidence_score=sig.confidence_score,
                    persistence_ticks=sig.persistence_ticks,
                    detected_at=datetime.fromtimestamp(sig.detected_at_ts / 1000, tz=timezone.utc).isoformat()
                )

                # Open a corresponding paper trade
                insert_paper_trade(
                    signal_id=sig_id,
                    stake=100.0,
                    odds_taken=sig.decimal_odds
                )
                print(f"[{_now()}] Persisted steam signal #{sig_id} and opened paper trade.")
            except Exception as e:
                print(f"[{_now()}] Error persisting steam signal to DB: {e}")

    def run_forever(self) -> None:
        """
        Main loop. Connects to the SSE stream and runs until interrupted.
        Reconnects automatically on any network error.
        """
        print(f"\nSharp Money Detector — SSE Stream Consumer")
        print(f"Stream URL: {self._build_url()}")
        print(f"Thresholds: movement>={5.0}%, persistence>={3} ticks")
        print("Waiting for odds updates (signals logged when they fire)...\n")

        while True:
            try:
                resp = requests.get(
                    self._build_url(),
                    headers=self._build_headers(),
                    stream=True,
                    timeout=(10, 600),  # (connect_timeout, read_timeout)
                )
                resp.raise_for_status()
                print(f"[{_now()}] Connected to stream (status={resp.status_code})")

                for event_type, event_id, data in _parse_sse_stream(resp):
                    if event_id:
                        self._last_event_id = event_id

                    if event_type == "heartbeat" or data.startswith('{"Ts"'):
                        self.heartbeats += 1
                        if self.heartbeats % 10 == 0:
                            print(f"[{_now()}] Heartbeat #{self.heartbeats}  "
                                  f"events={self.events_received}  "
                                  f"signals={self.signals_fired}  "
                                  f"windows={self.detector.window_count()}")
                        continue

                    # Parse OddsPayload
                    try:
                        payload = json.loads(data)
                        self.events_received += 1
                        self._process_payload(payload)
                    except json.JSONDecodeError as e:
                        print(f"[{_now()}] JSON parse error: {e}  data={data[:80]}")

            except KeyboardInterrupt:
                print(f"\n[{_now()}] Stopped by user. "
                      f"Events={self.events_received}, "
                      f"Signals={self.signals_fired}, "
                      f"Heartbeats={self.heartbeats}")
                break

            except requests.exceptions.RequestException as e:
                print(f"[{_now()}] Stream error: {e}")
                print(f"[{_now()}] Reconnecting in {RECONNECT_DELAY_SEC}s "
                      f"(Last-Event-ID: {self._last_event_id})")
                time.sleep(RECONNECT_DELAY_SEC)

            except Exception as e:
                print(f"[{_now()}] Unexpected error: {e}")
                time.sleep(RECONNECT_DELAY_SEC)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fixture_id = sys.argv[1] if len(sys.argv) > 1 else None
    if fixture_id:
        print(f"Filtering to fixture_id={fixture_id}")
    consumer = StreamConsumer(fixture_id=fixture_id)
    consumer.run_forever()
