"""Tests for :mod:`dota_deals.scoring.buy_score`.

Math here is hand-checkable; every test asserts an exact value so a future
refactor can't silently shift the score curve.
"""

from __future__ import annotations

from datetime import date

import pytest

from dota_deals.models.domain import BuyScore, Signal, SignalName
from dota_deals.scoring import buy_score

ITEM_ID = 42
AS_OF = date(2026, 5, 12)


def _sig(name: SignalName, value: float | None) -> Signal:
    return Signal(item_id=ITEM_ID, computed_for=AS_OF, signal_name=name, value=value, metadata={})


def test_weights_sum_to_one() -> None:
    """Sanity: weights must add up to 1.0 or every interpretation is off."""
    assert sum(buy_score.WEIGHTS.values()) == pytest.approx(1.0, abs=1e-12)


def test_all_four_signals_weighted_sum() -> None:
    """0.35·0.5 + 0.35·0.4 + 0.20·0.3 + 0.10·0.2 = 0.395."""
    signals = [
        _sig("price_zscore", 0.5),
        _sig("supply_velocity", 0.4),
        _sig("event_proximity", 0.3),
        _sig("comparables_delta", 0.2),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.score == pytest.approx(0.395, abs=1e-12)
    assert score.components == {
        "price_zscore": 0.5,
        "supply_velocity": 0.4,
        "event_proximity": 0.3,
        "comparables_delta": 0.2,
    }


def test_all_signals_at_plus_half_yields_exactly_plus_half() -> None:
    """When every signal is +0.5, the weighted sum is +0.5 regardless of weights.

    Sanity check that the renormalization isn't doing anything weird in the
    all-present case.
    """
    signals = [_sig(name, 0.5) for name in buy_score.WEIGHTS]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.score == pytest.approx(0.5, abs=1e-12)


def test_one_null_renormalizes_remaining_three() -> None:
    """price=0.5, supply=0.4, event=null, peers=0.2.

    Effective weights: 0.35/0.80 = 0.4375, 0.35/0.80 = 0.4375, 0.10/0.80 = 0.125.
    Score = 0.4375·0.5 + 0.4375·0.4 + 0.125·0.2 = 0.41875.
    """
    signals = [
        _sig("price_zscore", 0.5),
        _sig("supply_velocity", 0.4),
        _sig("event_proximity", None),
        _sig("comparables_delta", 0.2),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.score == pytest.approx(0.41875, abs=1e-12)
    assert score.data_quality["null_signals"] == ["event_proximity"]


def test_two_nulls_still_computes_from_remaining_two() -> None:
    """Only price + supply contribute; weights split 0.5 / 0.5.

    price=0.6, supply=0.2, event=null, peers=null → 0.5·0.6 + 0.5·0.2 = 0.4.
    """
    signals = [
        _sig("price_zscore", 0.6),
        _sig("supply_velocity", 0.2),
        _sig("event_proximity", None),
        _sig("comparables_delta", None),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.score == pytest.approx(0.4, abs=1e-12)
    null_signals = score.data_quality["null_signals"]
    assert isinstance(null_signals, list)
    assert sorted(null_signals) == sorted(["event_proximity", "comparables_delta"])


def test_three_nulls_returns_none() -> None:
    signals = [
        _sig("price_zscore", 0.6),
        _sig("supply_velocity", None),
        _sig("event_proximity", None),
        _sig("comparables_delta", None),
    ]
    assert buy_score.compute_buy_score(signals) is None


def test_event_proximity_null_does_not_force_zero() -> None:
    """Phase 5 decision: event_proximity returns null (not 0.0) when no event
    is in window. The scorer must renormalize, not multiply by 0.0.

    Same buy thesis with event treated two different ways:
    - As 0.0 with full 20% weight → 0.35·0.6 + 0.35·0.5 + 0.20·0 + 0.10·0.3 = 0.41
    - As null with renormalization → see below
    Renormalized weights: 0.35/0.80 = 0.4375, 0.4375, 0.10/0.80 = 0.125.
    Score = 0.4375·0.6 + 0.4375·0.5 + 0.125·0.3 = 0.2625 + 0.21875 + 0.0375
          = 0.51875.

    The test asserts the higher (renormalized) value — proving the
    convention is enforced end-to-end.
    """
    signals = [
        _sig("price_zscore", 0.6),
        _sig("supply_velocity", 0.5),
        _sig("event_proximity", None),  # SPEC: no event in window → null
        _sig("comparables_delta", 0.3),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.score == pytest.approx(0.51875, abs=1e-12)


def test_components_dict_contains_every_signal_even_when_null() -> None:
    """Display layer needs the full picture; nulls stay in components."""
    signals = [
        _sig("price_zscore", 0.6),
        _sig("supply_velocity", None),
        _sig("event_proximity", 0.4),
        _sig("comparables_delta", None),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert set(score.components) == {
        "price_zscore",
        "supply_velocity",
        "event_proximity",
        "comparables_delta",
    }
    assert score.components["supply_velocity"] is None
    assert score.components["comparables_delta"] is None


def test_explanation_cites_strongest_contributor() -> None:
    """The explanation template matches the signal with max |effective_weight · value|."""
    # price=0.9 has the largest contribution under any renormalization; even
    # though peers=0.95 is larger in magnitude, its 0.10 weight (or 0.125
    # after renormalization) makes its contribution smaller than price's.
    signals = [
        _sig("price_zscore", 0.9),
        _sig("supply_velocity", 0.1),
        _sig("event_proximity", None),
        _sig("comparables_delta", 0.95),
    ]
    # Contributions (renormalized; non_null_sum = 0.80):
    #   price: 0.4375 · 0.9 = 0.39375
    #   supply: 0.4375 · 0.1 = 0.04375
    #   peers: 0.125 · 0.95 = 0.11875
    # → price wins.
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.explanation == "Priced below recent baseline"


def test_explanation_negative_direction() -> None:
    """A strong negative signal triggers the negative-direction template."""
    signals = [
        _sig("price_zscore", -0.9),
        _sig("supply_velocity", 0.1),
        _sig("event_proximity", 0.1),
        _sig("comparables_delta", 0.1),
    ]
    score = buy_score.compute_buy_score(signals)
    assert score is not None
    assert score.explanation == "Priced above recent baseline"


def test_empty_signals_list_raises() -> None:
    with pytest.raises(ValueError):
        buy_score.compute_buy_score([])


def test_mixed_item_ids_raises() -> None:
    a = Signal(item_id=1, computed_for=AS_OF, signal_name="price_zscore", value=0.5, metadata={})
    b = Signal(item_id=2, computed_for=AS_OF, signal_name="supply_velocity", value=0.5, metadata={})
    with pytest.raises(ValueError):
        buy_score.compute_buy_score([a, b])


# ----------------------------- rank_top_n -------------------------------------


def _bs(item_id: int, score_value: float) -> BuyScore:
    components: dict[SignalName, float | None] = {
        "price_zscore": 0.0,
        "supply_velocity": 0.0,
        "event_proximity": None,
        "comparables_delta": 0.0,
    }
    return BuyScore(
        item_id=item_id,
        computed_for=AS_OF,
        score=score_value,
        components=components,
        explanation="test",
    )


def test_rank_top_n_orders_by_score_descending() -> None:
    scores = [_bs(1, 0.1), _bs(2, 0.5), _bs(3, 0.3)]
    ranked = buy_score.rank_top_n(scores, 3)
    assert [s.item_id for s in ranked] == [2, 3, 1]


def test_rank_top_n_ties_broken_by_item_id_ascending() -> None:
    scores = [_bs(7, 0.5), _bs(3, 0.5), _bs(5, 0.5)]
    ranked = buy_score.rank_top_n(scores, 3)
    assert [s.item_id for s in ranked] == [3, 5, 7]


def test_rank_top_n_limit_truncates() -> None:
    scores = [_bs(i, float(i) / 100) for i in range(10)]
    ranked = buy_score.rank_top_n(scores, 3)
    assert len(ranked) == 3
    assert ranked[0].score == pytest.approx(0.09, abs=1e-12)


def test_rank_top_n_zero_returns_empty() -> None:
    assert buy_score.rank_top_n([_bs(1, 0.5)], 0) == []


def test_rank_top_n_negative_raises() -> None:
    with pytest.raises(ValueError):
        buy_score.rank_top_n([], -1)
