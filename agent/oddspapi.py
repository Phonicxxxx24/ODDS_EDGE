"""
agent/oddspapi.py — OddsPapi client + Tri-Source Divergence Detection.

Implements SPEC.md sections 3.6, 3.7, 3.8, 3.9, and 4.2.

Hard rules (never break these):
  - ALWAYS call log_oddspapi_call() BEFORE making any HTTP request to OddsPapi
  - ALWAYS call get_oddspapi_usage_count() first; refuse if >= ODDSPAPI_TOTAL_BUDGET
  - Called only in the 2-3 hour pre-kickoff window per fixture (not on a timer)
  - One call per fixture is the default; a second is allowed only if budget > 200 remaining

Confirmed field paths (from SPEC.md 3.8, verified 2026-07-08 against Spain vs Belgium):
  Pinnacle decimal odds:
    bookmakerOdds.pinnacle.markets.{id}.outcomes.{id}.players.0.price
    (3-outcome markets with mainLine=true are the 1X2 match result markets)

  Polymarket implied probability (0-1 scale):
    bookmakerOdds.polymarket.markets.{id}.outcomes.{id}.players.0.exchangeMeta.back[0].cents
    (Polymarket is an EXCHANGE — do NOT use .price, do NOT de-vig .cents)
    (Binary only — no draw market in knockout rounds)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from agent.config import ODDSPAPI_TOTAL_BUDGET, DIVERGENCE_THRESHOLD_PCT
from agent.database import get_oddspapi_usage_count, log_oddspapi_call
from agent.detector import compute_confidence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
_TOKENS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "tokens.json",
)

# OddsPapi fixture IDs for World Cup fixtures with confirmed hasOdds=True (2026-07-08)
# Maps TxLINE fixture_id (int) -> OddsPapi fixture_id (str)
TXLINE_TO_ODDSPAPI: dict[int, str] = {
    18209181: "id1000001653452525",  # France vs Morocco  (2026-07-09)
    18218149: "id1000001653452527",  # Spain vs Belgium   (2026-07-10)
    18213979: "id1000001653452529",  # Norway vs England  (2026-07-11)
}

# ---------------------------------------------------------------------------
# Token loading
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    with open(_TOKENS_PATH) as f:
        tokens = json.load(f)
    key = tokens.get("oddspapi_key", "")
    if not key:
        raise RuntimeError("oddspapi_key is empty in tokens.json")
    return key


# ---------------------------------------------------------------------------
# Budget guard — call before EVERY request
# ---------------------------------------------------------------------------

def _budget_check(endpoint: str, fixture_id: Optional[str] = None) -> bool:
    """
    Check budget, log the call, return True if safe to proceed.
    Returns False if budget exhausted — caller must skip the HTTP request.
    """
    used = get_oddspapi_usage_count()
    remaining = ODDSPAPI_TOTAL_BUDGET - used
    if used >= ODDSPAPI_TOTAL_BUDGET:
        print(f"[OddsPapi] BUDGET EXHAUSTED ({used}/{ODDSPAPI_TOTAL_BUDGET}). "
              f"Skipping call to {endpoint}.")
        return False

    # Log BEFORE the request (SPEC.md rule)
    log_oddspapi_call(
        endpoint=endpoint,
        fixture_id=fixture_id,
        requests_remaining_estimate=remaining - 1,
    )
    print(f"[OddsPapi] Budget: {used+1}/{ODDSPAPI_TOTAL_BUDGET} (remaining after this call: {remaining-1})")
    return True


# ---------------------------------------------------------------------------
# Raw /odds fetch
# ---------------------------------------------------------------------------

def fetch_raw_odds(oddspapi_fixture_id: str) -> Optional[dict]:
    """
    Fetch GET /odds for a single fixture. Logs to oddspapi_usage before calling.
    Returns the parsed JSON response, or None on budget exhaustion / HTTP error.

    SPEC.md 3.8: GET /odds?apiKey={key}&fixtureId={id}
    """
    endpoint = f"/odds?fixtureId={oddspapi_fixture_id}"
    if not _budget_check(endpoint, fixture_id=oddspapi_fixture_id):
        return None

    key = _get_api_key()
    try:
        resp = requests.get(
            f"{_ODDSPAPI_BASE}/odds",
            params={"apiKey": key, "fixtureId": oddspapi_fixture_id},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("hasOdds"):
            print(f"[OddsPapi] {oddspapi_fixture_id}: hasOdds=False — no odds available yet.")
            return None
        return data
    except requests.RequestException as e:
        print(f"[OddsPapi] HTTP error for {oddspapi_fixture_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Pinnacle extractor
# SPEC.md 3.8: 3-outcome markets with mainLine=True = 1X2 match result
# price is decimal odds; de-vig with overround removal
# ---------------------------------------------------------------------------

@dataclass
class PinnacleProbs:
    market_id: str
    outcome_ids: list[str]           # ordered: [home_id, draw_id, away_id] or [home, away]
    decimal_odds: list[float]        # raw decimal odds per outcome
    fair_probs: list[float]          # de-vigged fair probability % (sums to ~100)
    overround: float                 # original overround before de-vig


def extract_pinnacle_probs(
    data: dict,
    txline_probs_hint: Optional[list[float]] = None,
) -> Optional[PinnacleProbs]:
    """
    Extract de-vigged Pinnacle probabilities for the main 1X2 (or binary) match market.
    Returns None if Pinnacle not in response or no suitable market found.

    SPEC.md 3.8 confirmed:
      - path: bookmakerOdds.pinnacle.markets.{id}.outcomes.{id}.players.0.price
      - 3-outcome market with mainLine=True and prices in realistic football range = 1X2

    txline_probs_hint: optional sorted list of TxLINE de-vigged probs (ascending).
      When provided, we pick the 3-outcome market whose de-vigged probs are closest
      to TxLINE (minimise sum of squared differences after sorting both ascending).
      This is the most reliable way to identify the true 1X2 vs other 3-outcome markets.
    """
    bm = data.get("bookmakerOdds", {})
    pin = bm.get("pinnacle", {})
    if not pin or not pin.get("bookmakerIsActive"):
        return None

    markets = pin.get("markets", {})

    # Collect all valid candidate markets
    candidates: list[tuple[float, str, list[tuple[str, float]]]] = []
    # (match_score, market_id, [(outcome_id, price)])

    for market_id, mkt in markets.items():
        outcomes = mkt.get("outcomes", {})
        if len(outcomes) not in (2, 3):
            continue

        main_prices: list[tuple[str, float]] = []
        for oid, out in outcomes.items():
            for player in out.get("players", {}).values():
                if player.get("mainLine") and player.get("price"):
                    main_prices.append((oid, float(player["price"])))
                    break

        if len(main_prices) != len(outcomes):
            continue

        odds = [p for _, p in main_prices]
        # All odds must be in realistic football range
        if not all(1.0 < o < 20 for o in odds):
            continue

        # Overround must be in a Pinnacle-typical range
        try:
            ovrd = sum(1.0 / o for o in odds)
        except ZeroDivisionError:
            continue
        if not (1.01 <= ovrd <= 1.20):
            continue

        # Build de-vigged probs for this candidate
        implied = [1.0 / o for o in odds]
        fair    = [round((r / ovrd) * 100, 4) for r in implied]

        # Score: prefer 3-outcome; if TxLINE hint provided, also score by similarity
        score = 10.0 if len(main_prices) == 3 else 5.0

        if txline_probs_hint and len(txline_probs_hint) == len(fair):
            # Sort both ascending, compute sum of squared differences
            diff_sq = sum(
                (a - b) ** 2
                for a, b in zip(sorted(fair), sorted(txline_probs_hint))
            )
            # Invert to score (lower diff = higher score), cap at 50
            similarity = max(0, 50.0 - diff_sq)
            score += similarity

        candidates.append((score, market_id, main_prices))

    if not candidates:
        return None

    # Best candidate = highest score
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_market_id, best_pairs = candidates[0]

    outcome_ids  = [oid for oid, _ in best_pairs]
    decimal_odds = [price for _, price in best_pairs]

    implied_raw = [1.0 / d for d in decimal_odds]
    overround   = sum(implied_raw)
    fair_probs  = [round((r / overround) * 100, 4) for r in implied_raw]

    return PinnacleProbs(
        market_id=best_market_id,
        outcome_ids=outcome_ids,
        decimal_odds=decimal_odds,
        fair_probs=fair_probs,
        overround=round(overround, 5),
    )


# ---------------------------------------------------------------------------
# Polymarket extractor
# SPEC.md 3.8: exchangeMeta.back[0].cents is the implied prob (0-1 scale)
# Do NOT de-vig; do NOT use .price field.
# ---------------------------------------------------------------------------

@dataclass
class PolymarketProbs:
    market_id: str
    outcome_ids: list[str]
    cents_raw: list[float]       # 0–1 scale as returned
    probs_pct: list[float]       # cents * 100 (percentage)


def extract_polymarket_probs(
    data: dict,
    txline_probs_hint: Optional[list[float]] = None,
) -> Optional[PolymarketProbs]:
    """
    Extract Polymarket implied probabilities from exchange cents field.

    SPEC.md 3.8 confirmed:
      path: bookmakerOdds.polymarket.markets.{id}.outcomes.{id}.players.0
              .exchangeMeta.back[0].cents
      Format: 0–1 scale (e.g. 0.63 = 63%). Multiply by 100 for %.
      Do NOT de-vig. Polymarket is an exchange — cents is already probability.

    txline_probs_hint: optional list of TxLINE de-vigged probs.
      When provided, pick the market whose cents probs are closest to TxLINE.
    """
    bm = data.get("bookmakerOdds", {})
    poly = bm.get("polymarket", {})
    if not poly or not poly.get("bookmakerIsActive"):
        return None

    candidates: list[tuple[float, str, list[tuple[str, float]]]] = []

    for market_id, mkt in poly.get("markets", {}).items():
        outcomes = mkt.get("outcomes", {})
        pairs: list[tuple[str, float]] = []

        for oid, out in outcomes.items():
            for player in out.get("players", {}).values():
                if not player.get("mainLine"):
                    continue
                em = player.get("exchangeMeta", {})
                back = em.get("back", [])
                if not back:
                    continue
                cents = back[0].get("cents")
                if cents is not None:
                    pairs.append((oid, float(cents)))
                    break

        if not pairs:
            continue

        # Score: prefer more outcomes (3 > 2); if TxLINE hint, also score by similarity
        probs_pct = [round(c * 100, 4) for _, c in pairs]
        score = 5.0 + len(pairs) * 2.0  # base score: 2-outcome=9, 3-outcome=11

        if txline_probs_hint and len(txline_probs_hint) == len(probs_pct):
            diff_sq = sum(
                (a - b) ** 2
                for a, b in zip(sorted(probs_pct), sorted(txline_probs_hint))
            )
            similarity = max(0, 50.0 - diff_sq)
            score += similarity

        candidates.append((score, market_id, pairs))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_market_id, best_pairs = candidates[0]

    outcome_ids = [oid for oid, _ in best_pairs]
    cents_raw   = [c for _, c in best_pairs]
    probs_pct   = [round(c * 100, 4) for c in cents_raw]

    return PolymarketProbs(
        market_id=best_market_id,
        outcome_ids=outcome_ids,
        cents_raw=cents_raw,
        probs_pct=probs_pct,
    )


# ---------------------------------------------------------------------------
# Divergence Signal
# ---------------------------------------------------------------------------

@dataclass
class DivergenceSignal:
    fixture_id: str                  # TxLINE fixture ID
    oddspapi_fixture_id: str
    outcome_label: str               # e.g. "home" / "away" / "draw"
    txline_prob: float               # TxLINE de-vigged Pct (%)
    pinnacle_prob: float             # Pinnacle de-vigged fair prob (%)
    polymarket_prob: Optional[float] # Polymarket cents*100 (%) — None if no Poly market
    max_divergence: float            # max - min across available sources (%)
    outlier_source: str              # "txline" | "pinnacle" | "polymarket"
    num_sources: int                 # 2 or 3
    confidence_score: float
    detected_at: str                 # ISO 8601 UTC
    pinnacle_odds: float = 1.0


# ---------------------------------------------------------------------------
# Core tri-source divergence logic  (SPEC.md section 4.2)
# ---------------------------------------------------------------------------

def _identify_outlier(
    label: str,
    txline: float,
    pinnacle: float,
    polymarket: Optional[float],
) -> tuple[str, float]:
    """
    Return (outlier_source, max_divergence).
    Outlier is the source furthest from the average of the other two.
    If Polymarket is None (no market), compare only TxLINE vs Pinnacle.
    """
    if polymarket is not None:
        sources = {"txline": txline, "pinnacle": pinnacle, "polymarket": polymarket}
        avg_without: dict[str, float] = {}
        for src, val in sources.items():
            others = [v for k, v in sources.items() if k != src]
            avg_without[src] = abs(val - sum(others) / len(others))
        outlier = max(avg_without, key=avg_without.get)  # type: ignore
        max_div = max(sources.values()) - min(sources.values())
    else:
        max_div  = abs(txline - pinnacle)
        outlier = "txline" if txline > pinnacle else "pinnacle"

    return outlier, round(max_div, 4)


def compute_divergence_signals(
    txline_fixture_id: str,
    oddspapi_fixture_id: str,
    txline_pct: dict[str, float],   # {"part1": 43.38, "draw": 40.26, "part2": 16.37}
    pinnacle_probs: PinnacleProbs,
    polymarket_probs: Optional[PolymarketProbs],
) -> list[DivergenceSignal]:
    """
    Run SPEC.md 4.2 tri-source divergence for a single fixture.

    txline_pct keys are TxLINE PriceNames ("part1"/"draw"/"part2").
    Pinnacle has outcome_ids but no labels — we assume ordering:
        [home, draw, away] for 3-outcome or [home, away] for 2-outcome.
    Polymarket is binary (home, away) — mapped by outcome index (0=home, 1=away).

    Returns list of DivergenceSignal (one per outcome that exceeds threshold).
    """
    signals: list[DivergenceSignal] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build TxLINE probability list in canonical order
    txline_ordered: list[tuple[str, float]] = []
    for name in ("part1", "draw", "part2"):
        if name in txline_pct:
            txline_ordered.append((name, txline_pct[name]))

    # Canonical labels
    label_map = {0: "home", 1: "draw", 2: "away"}
    binary_label_map = {0: "home", 1: "away"}

    # Detect if Polymarket has 3 outcomes (including draw) or just 2
    poly_is_3way = polymarket_probs is not None and len(polymarket_probs.probs_pct) == 3
    is_binary = len(pinnacle_probs.fair_probs) == 2

    # Build Polymarket lookup by position (0=home, 1=draw/away, [2=away for 3-way])
    poly_by_pos: dict[int, float] = {}
    if polymarket_probs:
        for i, prob in enumerate(polymarket_probs.probs_pct):
            poly_by_pos[i] = prob

    # Build Pinnacle lookup by position
    pin_by_pos: dict[int, float] = {}
    for i, prob in enumerate(pinnacle_probs.fair_probs):
        pin_by_pos[i] = prob

    # Map TxLINE outcomes to Pinnacle/Polymarket
    # TxLINE: part1=home(0), draw=draw(1), part2=away(2)
    for tx_idx, (tx_name, tx_prob) in enumerate(txline_ordered):
        canonical_idx = tx_idx  # 0=home, 1=draw, 2=away

        # Skip draw for binary Pinnacle markets (no draw = knockout binary)
        if is_binary and canonical_idx == 1:
            continue

        # Map binary Pinnacle index: home=0, away=1 (draw skipped above)
        pin_idx = 0 if canonical_idx == 0 else (1 if is_binary else canonical_idx)
        pin_prob = pin_by_pos.get(pin_idx)
        if pin_prob is None:
            continue

        # Polymarket index mapping:
        #   3-way market: use canonical_idx directly (0=home, 1=draw, 2=away)
        #   binary market: skip draw (idx 1), map home=0, away=1
        if polymarket_probs and poly_is_3way:
            poly_prob = poly_by_pos.get(canonical_idx)
        elif polymarket_probs and canonical_idx != 1:
            poly_idx = 0 if canonical_idx == 0 else 1
            poly_prob = poly_by_pos.get(poly_idx)
        else:
            poly_prob = None

        label = binary_label_map[min(canonical_idx, 1)] if is_binary else label_map[canonical_idx]

        outlier, max_div = _identify_outlier(label, tx_prob, pin_prob, poly_prob)
        num_sources = 3 if poly_prob is not None else 2

        if max_div < DIVERGENCE_THRESHOLD_PCT:
            continue  # below threshold

        # Confidence for tri-source: persistence_ticks=0 (single snapshot),
        # source_agreement = (num_sources - 1) / num_sources (outlier disagrees)
        conf = compute_confidence(
            pct_change=max_div,
            persistence_ticks=0,
            is_in_play=False,
            match_minute=None,
            num_sources_agreeing=num_sources - 1,
        )

        pin_odds = pinnacle_probs.decimal_odds[pin_idx] if pin_idx < len(pinnacle_probs.decimal_odds) else 1.0

        signals.append(DivergenceSignal(
            fixture_id=txline_fixture_id,
            oddspapi_fixture_id=oddspapi_fixture_id,
            outcome_label=label,
            txline_prob=round(tx_prob, 4),
            pinnacle_prob=round(pin_prob, 4),
            polymarket_prob=round(poly_prob, 4) if poly_prob is not None else None,
            max_divergence=max_div,
            outlier_source=outlier,
            num_sources=num_sources,
            confidence_score=conf,
            detected_at=now_iso,
            pinnacle_odds=round(pin_odds, 4),
        ))

    return signals


# ---------------------------------------------------------------------------
# High-level entry point — the one function the scheduler calls
# ---------------------------------------------------------------------------

def run_tri_source_check(
    txline_fixture_id: str,
    txline_pct: dict[str, float],
) -> list[DivergenceSignal]:
    """
    Full tri-source divergence check for one fixture.

    1. Look up the OddsPapi fixture ID from TXLINE_TO_ODDSPAPI.
    2. Budget check + log.
    3. Fetch /odds.
    4. Extract Pinnacle + Polymarket.
    5. Compute divergence per SPEC.md 4.2.
    6. Return signals (caller writes to DB).

    Returns [] if:
      - No OddsPapi mapping for this fixture
      - Budget exhausted
      - HTTP error
      - Pinnacle not available
      - No outcomes exceed DIVERGENCE_THRESHOLD_PCT
    """
    fid_int = int(txline_fixture_id) if str(txline_fixture_id).isdigit() else None

    # Support both int and str keys
    oddspapi_id = TXLINE_TO_ODDSPAPI.get(fid_int) or TXLINE_TO_ODDSPAPI.get(txline_fixture_id)  # type: ignore
    if not oddspapi_id:
        print(f"[OddsPapi] No OddsPapi ID mapped for TxLINE fixture {txline_fixture_id}. Skipping.")
        return []

    raw_data = fetch_raw_odds(oddspapi_id)
    if raw_data is None:
        return []

    # Pass TxLINE probs as a hint so Pinnacle can find the matching 1X2 market
    txline_hint = list(txline_pct.values())  # [part1_pct, draw_pct, part2_pct]
    pin = extract_pinnacle_probs(raw_data, txline_probs_hint=txline_hint)
    if pin is None:
        print(f"[OddsPapi] Could not extract Pinnacle probs for {oddspapi_id}")
        return []

    poly = extract_polymarket_probs(raw_data, txline_probs_hint=txline_hint)

    signals = compute_divergence_signals(
        txline_fixture_id=str(txline_fixture_id),
        oddspapi_fixture_id=oddspapi_id,
        txline_pct=txline_pct,
        pinnacle_probs=pin,
        polymarket_probs=poly,
    )

    # Print summary
    _print_tri_source_summary(
        fixture_id=str(txline_fixture_id),
        oddspapi_id=oddspapi_id,
        txline_pct=txline_pct,
        pin=pin,
        poly=poly,
        signals=signals,
    )

    return signals


# ---------------------------------------------------------------------------
# Terminal output formatter
# ---------------------------------------------------------------------------

def _print_tri_source_summary(
    fixture_id: str,
    oddspapi_id: str,
    txline_pct: dict[str, float],
    pin: PinnacleProbs,
    poly: Optional[PolymarketProbs],
    signals: list[DivergenceSignal],
) -> None:
    SEP = "=" * 70
    print(f"\n{SEP}")
    print(f"TRI-SOURCE DIVERGENCE ANALYSIS")
    print(f"TxLINE fixture: {fixture_id}  OddsPapi: {oddspapi_id}")
    print(SEP)

    print(f"\nTxLINE probabilities (de-vigged from Pct field):")
    for name, pct in txline_pct.items():
        print(f"  {name:<10} {pct:.3f}%")

    print(f"\nPinnacle probabilities (de-vigged, market_id={pin.market_id}, overround={pin.overround:.4f}):")
    for i, prob in enumerate(pin.fair_probs):
        label = ["home", "draw", "away"][i] if len(pin.fair_probs) == 3 else ["home", "away"][i]
        print(f"  {label:<10} {prob:.3f}%  (raw odds: {pin.decimal_odds[i]})")

    if poly:
        print(f"\nPolymarket probabilities (exchange cents × 100, market_id={poly.market_id}):")
        labels = ["home", "away"]
        for i, prob in enumerate(poly.probs_pct):
            lbl = labels[i] if i < len(labels) else str(i)
            print(f"  {lbl:<10} {prob:.3f}%  (raw cents: {poly.cents_raw[i]})")
    else:
        print("\nPolymarket: no odds available for this fixture")

    if signals:
        print(f"\n{'─'*70}".encode('ascii', 'replace').decode('ascii'))
        print(f"+++ DIVERGENCE SIGNALS FIRED ({len(signals)}) +++")
        for s in signals:
            poly_str = f"{s.polymarket_prob:.3f}%" if s.polymarket_prob is not None else "N/A"
            print(f"\n  Outcome  : {s.outcome_label}")
            print(f"  TxLINE   : {s.txline_prob:.3f}%")
            print(f"  Pinnacle : {s.pinnacle_prob:.3f}%")
            print(f"  Polymarket: {poly_str}")
            print(f"  Divergence: {s.max_divergence:.3f}% (threshold={DIVERGENCE_THRESHOLD_PCT}%)")
            print(f"  Outlier  : {s.outlier_source}")
            print(f"  Confidence: {int(s.confidence_score * 100)}%")
    else:
        print(f"\nNo divergence signals (all outcomes within {DIVERGENCE_THRESHOLD_PCT}% of each other)")

    print(f"\n{SEP}\n")
