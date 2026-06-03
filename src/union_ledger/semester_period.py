"""Fixed calendar ranges for regular (1·2) semesters on the 결산안 UI."""

from __future__ import annotations

from datetime import date

# Matches FE subtitle rules: 1학기 Mar–Jun, 2학기 Sep–Feb (crosses year).
_SEMESTER_1 = "1"
_SEMESTER_2 = "2"


def resolve_semester_period(
    academic_year: int,
    semester: str,
) -> tuple[date, date] | None:
    """Return inclusive period bounds for semester ``1`` or ``2``.

    ``academic_year`` is the Korean 학년도 label year (e.g. 2024학년도 2학기
    → start 2024-09-01, end 2025-02-28). Summer/winter have no fixed rule yet.
    """
    if semester == _SEMESTER_1:
        return (date(academic_year, 3, 2), date(academic_year, 6, 22))
    if semester == _SEMESTER_2:
        return (date(academic_year, 9, 1), date(academic_year + 1, 2, 28))
    return None
