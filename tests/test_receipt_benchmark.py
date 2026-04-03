from decimal import Decimal

from union_ledger.tools.receipt_benchmark import (
    _build_summary,
    _evaluate_merchant_name,
    _evaluate_scalar,
    _normalize_match_text,
)


def test_normalize_match_text_removes_spacing_and_punctuation() -> None:
    assert _normalize_match_text("건국대 학생회관 1847카페") == "건국대학생회관1847카페"
    assert _normalize_match_text("건국대점(학생식당)") == "건국대점학생식당"


def test_evaluate_merchant_name_supports_normalized_match() -> None:
    result = _evaluate_merchant_name("건국대점(학생식당)", "건국대점 학생식당")

    assert result.exact_match is False
    assert result.normalized_match is True
    assert result.similarity is not None
    assert result.similarity > 0.9


def test_build_summary_counts_hits_correctly() -> None:
    evaluations = [
        type(
            "Evaluation",
            (),
            {
                "overall_pass": True,
                "date_result": _evaluate_scalar("2026-04-03", "2026-04-03"),
                "merchant_result": _evaluate_merchant_name("학생회관 매점", "학생회관 매점"),
                "amount_result": _evaluate_scalar(str(Decimal("4000")), str(Decimal("4000"))),
                "payment_method_result": _evaluate_scalar("card", "card"),
            },
        )(),
        type(
            "Evaluation",
            (),
            {
                "overall_pass": False,
                "date_result": _evaluate_scalar("2026-04-03", "2026-04-02"),
                "merchant_result": _evaluate_merchant_name("학생회관 매점", "학생회관매점"),
                "amount_result": _evaluate_scalar(str(Decimal("4000")), str(Decimal("4500"))),
                "payment_method_result": _evaluate_scalar("card", "card"),
            },
        )(),
    ]

    summary = _build_summary(evaluations)

    assert summary["total_samples"] == 2
    assert summary["overall_pass_count"] == 1
    assert summary["overall_pass_rate"] == 0.5
    assert summary["date_accuracy"] == 0.5
    assert summary["merchant_accuracy"] == 1.0
    assert summary["amount_accuracy"] == 0.5
    assert summary["payment_method_accuracy"] == 1.0
