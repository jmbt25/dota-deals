"""Composite buy score and ranking.

Weights (from SPEC.md):

* ``price_zscore``      0.35
* ``supply_velocity``   0.35
* ``event_proximity``   0.20
* ``comparables_delta`` 0.10

Null handling
-------------
Any signal with ``value=None`` is dropped from the weighted sum and the
remaining signals' weights are renormalized so they still sum to 1.0.
Concretely, with two nulls the remaining two share the full weight in their
original ratio. With three or more nulls the score is not emitted at all
(:func:`compute_buy_score` returns ``None``).

The Phase 5 convention is that ``event_proximity`` returns ``None`` (not
``0.0``) when no event is within the 60-day lookahead — so most-of-year
items renormalize naturally instead of having 20% of weight pinned to
zero. See SPEC.md and docs/SCORING.md for the rationale.

Display rule
------------
SPEC.md is firm: "every published buy score must show its four component
values and a one-line explanation citing the strongest contributing
signal." ``compute_buy_score`` constructs that explanation by picking the
signal with the largest absolute *renormalized* contribution and pairing
its direction with a fixed plain-English template (see
:data:`_EXPLANATIONS`). The templates intentionally describe *what is
true*, not *what to do*.
"""

from __future__ import annotations

from collections.abc import Mapping

from dota_deals.models.domain import BuyScore, Signal, SignalName

WEIGHTS: dict[SignalName, float] = {
    "price_zscore": 0.35,
    "supply_velocity": 0.35,
    "event_proximity": 0.20,
    "comparables_delta": 0.10,
}

_MAX_NULLS = 2  # 3+ nulls → no score
_ALL_SIGNAL_NAMES: tuple[SignalName, ...] = (
    "price_zscore",
    "supply_velocity",
    "event_proximity",
    "comparables_delta",
)

# Direction-keyed explanation templates. Keys are (signal_name, "positive"|"negative").
_EXPLANATIONS: Mapping[tuple[SignalName, str], str] = {
    ("price_zscore", "positive"): "Priced below recent baseline",
    ("price_zscore", "negative"): "Priced above recent baseline",
    ("supply_velocity", "positive"): "Listings contracting",
    ("supply_velocity", "negative"): "Listings expanding",
    ("event_proximity", "positive"): ("Historically appreciates before upcoming event"),
    ("event_proximity", "negative"): ("Historically depreciates before upcoming event"),
    ("comparables_delta", "positive"): "Priced below same-category peers",
    ("comparables_delta", "negative"): "Priced above same-category peers",
}


def compute_buy_score(signals: list[Signal]) -> BuyScore | None:
    """Compose a :class:`BuyScore` from the four signals for one item.

    Returns ``None`` if three or more signals have ``value=None``. The
    returned score's ``components`` dict carries every signal value
    (including nulls) so the display layer can show the full picture;
    ``data_quality.null_signals`` lists the nulls. ``explanation`` cites
    the strongest renormalized contributor.

    :raises ValueError: if ``signals`` is empty, if it mixes ``item_id`` or
        ``computed_for``, or contains an unknown ``signal_name`` —
        contract violations indicating a runner bug.
    """
    if not signals:
        raise ValueError("compute_buy_score requires at least one signal")
    item_id = signals[0].item_id
    computed_for = signals[0].computed_for
    for sig in signals[1:]:
        if sig.item_id != item_id or sig.computed_for != computed_for:
            raise ValueError(
                f"compute_buy_score: signals must share (item_id, computed_for); "
                f"got mixed (item_id={sig.item_id}, computed_for={sig.computed_for})"
            )

    components: dict[SignalName, float | None] = {name: None for name in _ALL_SIGNAL_NAMES}
    for sig in signals:
        if sig.signal_name not in WEIGHTS:
            raise ValueError(f"unknown signal_name: {sig.signal_name!r}")
        components[sig.signal_name] = sig.value

    null_signals: list[SignalName] = [name for name, val in components.items() if val is None]
    if len(null_signals) > _MAX_NULLS:
        return None

    non_null_weight_sum = sum(WEIGHTS[name] for name, val in components.items() if val is not None)
    # Guaranteed > 0 by the null-count check, but be defensive against
    # weights that sum to zero (a future-edit foot-gun).
    if non_null_weight_sum == 0:
        return None

    score: float = sum(
        (
            (WEIGHTS[name] / non_null_weight_sum) * val
            for name, val in components.items()
            if val is not None
        ),
        0.0,
    )
    # Clamp to model bounds; arithmetic above is mathematically within
    # [-1, 1] but float drift can produce -1.0000000000000002.
    score = max(-1.0, min(1.0, score))

    explanation = _choose_explanation(components, non_null_weight_sum)

    return BuyScore(
        item_id=item_id,
        computed_for=computed_for,
        score=score,
        components=components,
        explanation=explanation,
        data_quality={"null_signals": null_signals},
    )


def rank_top_n(scores: list[BuyScore], n: int) -> list[BuyScore]:
    """Return the ``n`` highest-scoring entries.

    Sort is by ``score`` descending; ties break on ``item_id`` ascending
    so the output is deterministic across runs.

    :raises ValueError: if ``n`` is negative.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if n == 0:
        return []
    return sorted(scores, key=lambda s: (-s.score, s.item_id))[:n]


def _choose_explanation(
    components: dict[SignalName, float | None],
    non_null_weight_sum: float,
) -> str:
    """Pick the signal with the largest absolute renormalized contribution
    and look up the matching template."""
    best_name: SignalName | None = None
    best_contribution = -1.0
    best_sign = "positive"
    for name in _ALL_SIGNAL_NAMES:
        val = components[name]
        if val is None:
            continue
        contribution = abs((WEIGHTS[name] / non_null_weight_sum) * val)
        if contribution > best_contribution:
            best_contribution = contribution
            best_name = name
            best_sign = "positive" if val >= 0 else "negative"
    if best_name is None:
        # Should be unreachable: null-count check guarantees ≥ 1 non-null.
        return "Score has no contributing signals"
    return _EXPLANATIONS[(best_name, best_sign)]
