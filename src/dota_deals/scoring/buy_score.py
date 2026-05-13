"""Composite buy score and ranking.

Weights:

* ``price_zscore``      0.35
* ``supply_velocity``   0.35
* ``event_proximity``   0.20
* ``comparables_delta`` 0.10

Null handling: if any signal is null, the remaining weights are renormalized
so they still sum to 1.0. If three or more signals are null, no score is
emitted.
"""

from __future__ import annotations

from dota_deals.models.domain import BuyScore, Signal, SignalName

WEIGHTS: dict[SignalName, float] = {
    "price_zscore": 0.35,
    "supply_velocity": 0.35,
    "event_proximity": 0.20,
    "comparables_delta": 0.10,
}


def compute_buy_score(signals: list[Signal]) -> BuyScore | None:
    """Compose a :class:`BuyScore` from a list of signals for one item.

    Returns ``None`` if three or more signal values are null. The returned
    score's ``components`` dict carries every signal value (including nulls)
    so the display layer can show the full picture. ``explanation`` cites the
    strongest contributing signal in plain English.

    :raises ValueError: if ``signals`` is empty or mixes item ids/dates.
    """
    raise NotImplementedError


def rank_top_n(scores: list[BuyScore], n: int) -> list[BuyScore]:
    """Return the ``n`` highest-scoring entries from ``scores``.

    Ties are broken by ``item_id`` ascending so the output is stable across
    runs.

    :raises ValueError: if ``n`` is negative.
    """
    raise NotImplementedError
