"""Smoke test: verify :mod:`dota_deals.scoring.buy_score` imports."""

from __future__ import annotations

import math

from dota_deals.scoring import buy_score


def test_module_imports() -> None:
    assert callable(buy_score.compute_buy_score)
    assert callable(buy_score.rank_top_n)


def test_weights_sum_to_one() -> None:
    assert math.isclose(sum(buy_score.WEIGHTS.values()), 1.0)
