"""Tests for :mod:`dota_deals.models.events`."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import BaseModel, ValidationError

from dota_deals.models import events


def test_module_imports() -> None:
    assert issubclass(events.EventRecord, BaseModel)


def test_valid_event_with_end_date_after_start() -> None:
    record = events.EventRecord(
        event_id=None,
        kind="ti",
        name="TI 2026",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 15),
    )
    assert record.start_date == date(2026, 8, 1)
    assert record.end_date == date(2026, 8, 15)


def test_valid_event_with_null_end_date() -> None:
    """Open-ended events (no end_date) are valid."""
    record = events.EventRecord(
        event_id=None,
        kind="major_patch",
        name="Patch 7.40",
        start_date=date(2026, 6, 1),
        end_date=None,
    )
    assert record.end_date is None


def test_end_date_before_start_date_rejected() -> None:
    """Validator: end_date < start_date is a ValidationError."""
    with pytest.raises(ValidationError):
        events.EventRecord(
            event_id=None,
            kind="ti",
            name="TI 2026",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 1),
        )


def test_end_date_equal_to_start_date_accepted() -> None:
    """One-day events: end_date == start_date is allowed."""
    record = events.EventRecord(
        event_id=None,
        kind="treasure_release",
        name="Mini-treasure",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 1),
    )
    assert record.end_date == record.start_date
