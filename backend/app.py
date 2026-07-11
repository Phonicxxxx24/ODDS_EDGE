"""
backend/app.py — Flask API for the Sharp Money Detector.

Routes (SPEC.md section 7 — implemented exactly as specified):
    GET /api/signals              all signals, paginated (?limit=&offset=)
    GET /api/signals/live         signals from in-play (LIVE) fixtures only
    GET /api/signals/<fixture_id> all signals for one fixture
    GET /api/pnl                  cumulative P&L time series for equity curve
    GET /api/stats                overall accuracy %, high-confidence accuracy %, total signal count
    GET /api/fixtures             fixtures tracked and their status
    GET /api/usage                OddsPapi requests used / remaining

All routes return JSON. CORS enabled for the frontend origin.
"""

import os
import sys
import sqlite3
import json
import requests
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# ── path setup so we can import agent.* from the project root ──────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.database import (
    _DB_PATH,
    _connect,
    init_db,
    get_all_signals,
    get_pnl_summary,
    get_oddspapi_usage_count,
    get_signal_by_id,
    update_signal_commentary,
)
from agent.config import ODDSPAPI_TOTAL_BUDGET
from agent.commentary import generate_commentary

# ── app setup ──────────────────────────────────────────────────────────────
_FRONTEND = os.path.join(_ROOT, "frontend")
app = Flask(__name__, static_folder=_FRONTEND, static_url_path="")
CORS(app)   # CORS enabled for all origins (frontend served locally)


# Ensure tables exist even if agent hasn't run yet
init_db()


# ── Serve frontend ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(_FRONTEND, "index.html")


@app.route("/docs")
def docs():
    return send_from_directory(_FRONTEND, "docs.html")


# ── helpers ────────────────────────────────────────────────────────────────

def _rows(query: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT query and return all rows as plain dicts."""
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def _one(query: str, params: tuple = ()):
    """Run a SELECT query and return the first row as a dict (or None)."""
    with _connect() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None


# ── GET /api/signals ───────────────────────────────────────────────────────

@app.route("/api/signals", methods=["GET"])
def api_signals():
    """
    All signals, newest first.  Supports ?limit= and ?offset= pagination.
    Default: limit=100, offset=0.
    """
    try:
        limit  = int(request.args.get("limit",  100))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    signals = get_all_signals(limit=limit, offset=offset)
    return jsonify({"signals": signals})


# ── GET /api/signals/live ──────────────────────────────────────────────────

@app.route("/api/signals/live", methods=["GET"])
def api_signals_live():
    """
    Signals belonging to fixtures that are currently LIVE (in-play).
    """
    signals = _rows(
        """
        SELECT s.*
        FROM   signals s
        JOIN   fixtures_tracked f ON f.fixture_id = s.fixture_id
        WHERE  f.status = 'LIVE'
        ORDER  BY s.detected_at DESC
        """
    )
    return jsonify({"signals": signals})


# ── GET /api/signals/<fixture_id> ─────────────────────────────────────────

@app.route("/api/signals/<fixture_id>", methods=["GET"])
def api_signals_for_fixture(fixture_id: str):
    """
    All signals for a single fixture, newest first.
    """
    signals = _rows(
        "SELECT * FROM signals WHERE fixture_id = ? ORDER BY detected_at DESC",
        (fixture_id,),
    )
    return jsonify({"fixture_id": fixture_id, "signals": signals})


# ── GET /api/pnl ───────────────────────────────────────────────────────────

@app.route("/api/pnl", methods=["GET"])
def api_pnl():
    """
    Cumulative P&L time series for the equity curve (Chart.js feed).
    Returns settled trades ordered by settled_at ASC, each with a
    running cumulative_pnl field.
    """
    series = get_pnl_summary()
    filtered_series = [
        {"settled_at": row["settled_at"], "cumulative_pnl": row["cumulative_pnl"]}
        for row in series
    ]
    return jsonify({"pnl_series": filtered_series})


# ── GET /api/stats ─────────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """
    Accuracy stats as defined in SPEC.md:
      - total signal count
      - overall accuracy % (scored signals only)
      - high-confidence accuracy % (confidence_score >= 0.7, scored signals only)
    """
    total = _one("SELECT COUNT(*) AS cnt FROM signals")["cnt"]

    # Overall scored signals
    overall = _one(
        """
        SELECT
            COUNT(*) AS scored,
            SUM(CASE WHEN outcome = 'CORRECT' THEN 1 ELSE 0 END) AS correct
        FROM signals
        WHERE outcome IS NOT NULL
        """
    )
    scored   = overall["scored"]   or 0
    correct  = overall["correct"]  or 0
    accuracy = round(correct / scored * 100, 2) if scored > 0 else None

    # High-confidence (>= 0.7) scored signals
    hc = _one(
        """
        SELECT
            COUNT(*) AS scored,
            SUM(CASE WHEN outcome = 'CORRECT' THEN 1 ELSE 0 END) AS correct
        FROM signals
        WHERE outcome IS NOT NULL AND confidence_score >= 0.7
        """
    )
    hc_scored   = hc["scored"]   or 0
    hc_correct  = hc["correct"]  or 0
    hc_accuracy = round(hc_correct / hc_scored * 100, 2) if hc_scored > 0 else None

    # Steam scored signals
    steam = _one(
        """
        SELECT
            COUNT(*) AS scored,
            SUM(CASE WHEN outcome = 'CORRECT' THEN 1 ELSE 0 END) AS correct
        FROM signals
        WHERE outcome IS NOT NULL AND signal_type = 'STEAM'
        """
    )
    steam_scored  = steam["scored"] or 0
    steam_correct = steam["correct"] or 0
    steam_accuracy = round(steam_correct / steam_scored * 100, 2) if steam_scored > 0 else None

    # Tri-Source scored signals
    trisource = _one(
        """
        SELECT
            COUNT(*) AS scored,
            SUM(CASE WHEN outcome = 'CORRECT' THEN 1 ELSE 0 END) AS correct
        FROM signals
        WHERE outcome IS NOT NULL AND signal_type = 'TRI_SOURCE_DIVERGENCE'
        """
    )
    trisource_scored  = trisource["scored"] or 0
    trisource_correct = trisource["correct"] or 0
    trisource_accuracy = round(trisource_correct / trisource_scored * 100, 2) if trisource_scored > 0 else None

    # Total Paper P&L from paper_trades
    pnl_row = _one(
        """
        SELECT SUM(profit_loss) AS total_pnl
        FROM paper_trades
        WHERE status IN ('WON', 'LOST')
        """
    )
    total_pnl = round(pnl_row["total_pnl"], 2) if pnl_row and pnl_row["total_pnl"] is not None else 0.0

    return jsonify({
        "total_signals":          total,
        "scored_signals":         scored,
        "correct_signals":        correct,
        "overall_accuracy_pct":   accuracy,
        "high_conf_accuracy_pct": hc_accuracy,
        "steam_accuracy_pct":     steam_accuracy,
        "trisource_accuracy_pct": trisource_accuracy,
        "total_pnl":              total_pnl,
    })


# ── GET /api/fixtures ──────────────────────────────────────────────────────

@app.route("/api/fixtures", methods=["GET"])
def api_fixtures():
    """
    All tracked fixtures and their current status (UPCOMING / LIVE / FINISHED).
    """
    fixtures = _rows(
        "SELECT * FROM fixtures_tracked ORDER BY start_time ASC"
    )
    return jsonify({"fixtures": fixtures})


# ── GET /api/usage ─────────────────────────────────────────────────────────

@app.route("/api/usage", methods=["GET"])
def api_usage():
    """
    OddsPapi budget status.
    Returns calls used, total budget, and remaining calls.
    """
    used      = get_oddspapi_usage_count()
    remaining = max(0, ODDSPAPI_TOTAL_BUDGET - used)
    return jsonify({
        "calls_used":      used,
        "total_budget":    ODDSPAPI_TOTAL_BUDGET,
        "calls_remaining": remaining,
    })


# ── POST /api/signals/<signal_id>/commentary ──────────────────────────────
# Checkpoint 6: on-demand Gemini AI commentary.
# Does NOT auto-generate — only called explicitly via this endpoint.

@app.route("/api/signals/<int:signal_id>/commentary", methods=["POST"])
def api_signal_commentary(signal_id: int):
    """
    Generate (or return cached) Gemini AI commentary for a single signal.
    """
    signal = get_signal_by_id(signal_id)
    if signal is None:
        return jsonify({"error": f"Signal {signal_id} not found"}), 404

    # --- Cache hit: return existing commentary without calling Gemini ---
    if signal.get("ai_commentary"):
        return jsonify({
            "signal_id":   signal_id,
            "commentary":  signal["ai_commentary"],
            "cached":      True,
        })

    # --- Generate via Gemini ---
    commentary = generate_commentary(signal)

    if commentary is None:
        # Key not set or call failed - distinguish the two cases for the client
        from agent.commentary import _resolve_api_key
        key_set = _resolve_api_key() is not None
        if not key_set:
            return jsonify({
                "error": "GEMINI_API_KEY not configured",
                "hint":  "Set GEMINI_API_KEY env var or add 'gemini_key' to tokens.json",
            }), 503
        return jsonify({"error": "Gemini call failed - see server log"}), 500

    # --- Save to DB and return ---
    update_signal_commentary(signal_id, commentary)
    return jsonify({
        "signal_id":  signal_id,
        "commentary": commentary,
        "cached":     False,
    })


# ── GET /api/signals/<signal_id>/merkle ──────────────────────────────────
# Fix 5: Retrieve Merkle proof validation from TxLINE

@app.route("/api/signals/<int:signal_id>/merkle", methods=["GET"])
def api_signal_merkle(signal_id: int):
    """
    Lookup a signal, resolve its MessageId and Ts if not stored,
    and fetch its cryptographically verified Merkle proof from TxLINE.
    """
    try:
        # 1. Look up signal in DB
        signal = get_signal_by_id(signal_id)
        if not signal:
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": None,
                "verified": False,
                "error": f"Signal {signal_id} not found"
            }), 404

        fixture_id = signal.get("fixture_id")
        detected_at = signal.get("detected_at")
        market = signal.get("market")
        message_id = signal.get("MessageId") or signal.get("message_id")

        # 2. Load tokens from tokens.json
        tokens_path = os.path.join(os.path.dirname(_ROOT), "tokens.json")
        if not os.path.exists(tokens_path):
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": None,
                "verified": False,
                "error": "tokens.json file not found"
            }), 500
        
        with open(tokens_path, "r") as f:
            tokens = json.load(f)
        
        jwt = tokens.get("jwt")
        api_token = tokens.get("apiToken")
        if not jwt or not api_token:
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": None,
                "verified": False,
                "error": "TxLINE tokens not configured in tokens.json"
            }), 500

        headers = {
            "Authorization": f"Bearer {jwt}",
            "X-Api-Token": api_token,
            "Accept": "application/json"
        }

        # 3. If MessageId is not stored, resolve it via historical updates
        ts = None
        if not message_id:
            try:
                dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
                dt_utc = dt.astimezone(timezone.utc)
                epoch_day = (dt_utc.date() - datetime(1970, 1, 1, tzinfo=timezone.utc).date()).days
                hour_of_day = dt_utc.hour
                interval = dt_utc.minute // 5
                
                updates_url = f"https://txline.txodds.com/api/odds/updates/{epoch_day}/{hour_of_day}/{interval}?fixtureId={fixture_id}"
                resp = requests.get(updates_url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    return jsonify({
                        "signal_id": signal_id,
                        "merkle_proof": None,
                        "verified": False,
                        "error": f"Failed to fetch updates from TxLINE (HTTP {resp.status_code})"
                    }), 500
                
                items = resp.json()
                if not isinstance(items, list) or len(items) == 0:
                    return jsonify({
                        "signal_id": signal_id,
                        "merkle_proof": None,
                        "verified": False,
                        "error": f"No odds updates found in the 5-minute interval for fixture {fixture_id}"
                    }), 404
                
                # Filter by market type
                matching_items = [item for item in items if item.get("SuperOddsType") == market]
                if not matching_items:
                    matching_items = items
                
                # Find item closest to signal detected_at timestamp
                sig_ts_ms = int(dt_utc.timestamp() * 1000)
                closest_item = min(matching_items, key=lambda x: abs(x.get("Ts", 0) - sig_ts_ms))
                message_id = closest_item.get("MessageId")
                ts = closest_item.get("Ts")
                
            except Exception as e:
                return jsonify({
                    "signal_id": signal_id,
                    "merkle_proof": None,
                    "verified": False,
                    "error": f"Error resolving MessageId from historical updates: {str(e)}"
                }), 500

        # 4. If we still don't have a message_id or ts, return error
        if not message_id or not ts:
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": None,
                "verified": False,
                "error": "Could not find MessageId or Ts for validation"
            }), 404

        # 5. Fetch Merkle proof from TxLINE
        validation_url = f"https://txline.txodds.com/api/odds/validation?messageId={message_id}&ts={ts}"
        val_resp = requests.get(validation_url, headers=headers, timeout=10)
        
        if val_resp.status_code == 200:
            proof_data = val_resp.json()
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": proof_data,
                "verified": True
            })
        else:
            error_msg = f"TxLINE validation endpoint returned HTTP {val_resp.status_code}"
            try:
                error_msg += f": {val_resp.text}"
            except Exception:
                pass
            return jsonify({
                "signal_id": signal_id,
                "merkle_proof": None,
                "verified": False,
                "error": error_msg
            }), val_resp.status_code

    except Exception as e:
        return jsonify({
            "signal_id": signal_id,
            "merkle_proof": None,
            "verified": False,
            "error": f"Unexpected error: {str(e)}"
        }), 500


# ── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Development only — production would use gunicorn or similar
    app.run(host="0.0.0.0", port=5000, debug=True)





