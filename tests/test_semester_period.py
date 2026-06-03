from datetime import date

from union_ledger.semester_period import resolve_semester_period


def test_semester_1_period() -> None:
    assert resolve_semester_period(2024, "1") == (
        date(2024, 3, 2),
        date(2024, 6, 22),
    )


def test_semester_2_period_crosses_calendar_year() -> None:
    assert resolve_semester_period(2024, "2") == (
        date(2024, 9, 1),
        date(2025, 2, 28),
    )


def test_summer_winter_have_no_fixed_period() -> None:
    assert resolve_semester_period(2024, "summer") is None
    assert resolve_semester_period(2024, "winter") is None
