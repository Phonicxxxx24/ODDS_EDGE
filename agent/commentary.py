"""
agent/commentary.py — Gemini AI Commentary (SPEC.md section 4.7)

Design: on-demand only — commentary is NOT generated automatically during
signal detection.  It is only generated when the Flask endpoint
POST /api/signals/<signal_id>/commentary is called.

Key rules:
  - GEMINI_API_KEY loaded from os.environ.get('GEMINI_API_KEY').
    Fallback: if env var not set, try the 'gemini_key' field in tokens.json.
    This matches the established pattern used by other modules in this project.
  - If the key is absent, empty, or the Gemini call fails for any reason,
    generate_commentary() returns None — never raises, never crashes the server.
  - Two prompt branches per SPEC.md 4.7: TRI_SOURCE_DIVERGENCE and STEAM.
  - The function is a pure helper; all DB writes (cache) happen in the caller.
"""

from __future__ import annotations

import os
import json
from typing import Optional

# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

_TOKENS_PATH = os.path.join(
    os.path.dirname(                                   # txodds/
        os.path.dirname(                               # sharp-detector/
            os.path.dirname(os.path.abspath(__file__)) # agent/
        )
    ),
    "tokens.json",
)

def _resolve_api_key() -> Optional[str]:
    """
    Resolve the Gemini API key.
    Priority:
      1. GEMINI_API_KEY environment variable
      2. 'gemini_key' field in tokens.json (project-local fallback)
    Returns None if neither is set or non-empty.
    """
    # 1. Environment variable (SPEC.md 4.7: "GEMINI_API_KEY stored as env var")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key

    # 2. tokens.json fallback (consistent with other modules in this project)
    try:
        with open(_TOKENS_PATH) as f:
            data = json.load(f)
        key = data.get("gemini_key", "").strip()
        return key if key else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Prompt builders — SPEC.md section 4.7, verbatim with signal dict fields
# ---------------------------------------------------------------------------

def _build_prompt(signal: dict) -> str:
    """
    Build the Gemini prompt for a signal dict as per SPEC.md 4.7.
    signal is a row from the signals table (dict with all columns).
    """
    p1 = signal.get("participant1") or "Team A"
    p2 = signal.get("participant2") or "Team B"

    if signal.get("signal_type") == "TRI_SOURCE_DIVERGENCE":
        return (
            f"A tri-source divergence signal was detected in a World Cup match.\n"
            f"Match: {p1} vs {p2}\n"
            f"TxLINE StablePrice implied probability: {signal.get('txline_prob')}%\n"
            f"Pinnacle (de-vigged) implied probability: {signal.get('pinnacle_prob')}%\n"
            f"Polymarket implied probability: {signal.get('polymarket_prob')}%\n"
            f"Outlier source: {signal.get('outlier_source')}\n"
            f"Confidence score: {signal.get('confidence_score')}\n\n"
            "Write one clear, specific sentence explaining what this divergence "
            "means for a sports trader. No fluff, no hedging language."
        )
    else:
        # STEAM signal
        return (
            f"A sharp money steam signal was detected.\n"
            f"Match: {p1} vs {p2}\n"
            f"Market: {signal.get('market') or '1X2'}\n"
            f"Price moved {signal.get('pct_change')}% in the sharp consensus, "
            f"held for {signal.get('persistence_ticks')} consecutive updates.\n"
            f"Confidence score: {signal.get('confidence_score')}\n\n"
            "Write one clear, specific sentence explaining what this movement "
            "means for a sports trader. No fluff, no hedging language."
        )


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def generate_commentary(signal: dict) -> Optional[str]:
    """
    Generate a one-sentence Gemini AI commentary for the given signal.

    Args:
        signal: A dict with signal row fields (from get_signal_by_id or
                similar).  Must contain at least 'signal_type'.

    Returns:
        A trimmed string on success, or None if:
          - GEMINI_API_KEY is not set / empty
          - Gemini API call fails (network error, quota, etc.)
          - Response is empty

    Never raises — failures are logged to stdout and return None.
    """
    api_key = _resolve_api_key()
    if not api_key:
        print("[Commentary] GEMINI_API_KEY not set — skipping commentary generation")
        return None

    try:
        import google.generativeai as genai  # noqa: E402  (lazy import — not always needed)
    except ImportError:
        print("[Commentary] google-generativeai package not installed")
        return None

    prompt = _build_prompt(signal)

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        text = response.text.strip() if response and response.text else None
        if text:
            print(f"[Commentary] Generated for signal {signal.get('id')}: {text[:80]}...")
            return text
        else:
            print(f"[Commentary] Empty response from Gemini for signal {signal.get('id')}")
            return None
    except Exception as exc:
        print(f"[Commentary] Gemini call failed for signal {signal.get('id')}: {exc}")
        return None
