from decimal import Decimal

from union_ledger.models.enums import EvidenceType, PaymentMethod
from union_ledger.tools.receipt_benchmark import (
    ReceiptEvaluation,
    _build_summary,
    _classify_failure_reasons,
    _evaluate_merchant_name,
    _evaluate_scalar,
    _normalize_match_text,
    _parse_payment_method,
)


def test_normalize_match_text_removes_spacing_and_punctuation() -> None:
    assert _normalize_match_text("건국대 학생회관 1847카페") == "건국대학생회관1847카페"
    assert _normalize_match_text("건국대-학생식당)") == "건국대학생식당"


def test_evaluate_merchant_name_supports_normalized_match() -> None:
    result = _evaluate_merchant_name("건국대 학생식당", "건국대학생식당")

    assert result.exact_match is False
    assert result.normalized_match is True
    assert result.similarity is not None
    assert result.similarity > 0.9


def test_build_summary_counts_hits_correctly() -> None:
    evaluations = [
        ReceiptEvaluation(
            file_name="a.jpg",
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            notes=None,
            split="dev",
            difficulty="easy",
            overall_pass=True,
            date_result=_evaluate_scalar("2026-04-03", "2026-04-03"),
            merchant_result=_evaluate_merchant_name("학생회관 카페", "학생회관 카페"),
            amount_result=_evaluate_scalar(str(Decimal("4000")), str(Decimal("4000"))),
            payment_method_result=_evaluate_scalar("card", "card"),
            failure_reasons=[],
            extraction_payload={},
            raw_text_preview="",
        ),
        ReceiptEvaluation(
            file_name="b.jpg",
            evidence_type=EvidenceType.PHYSICAL_RECEIPT,
            notes=None,
            split="test",
            difficulty="hard",
            overall_pass=False,
            date_result=_evaluate_scalar("2026-04-03", "2026-04-02"),
            merchant_result=_evaluate_merchant_name("학생회관 카페", "학생회관카페"),
            amount_result=_evaluate_scalar(str(Decimal("4000")), str(Decimal("4500"))),
            payment_method_result=_evaluate_scalar("card", "card"),
            failure_reasons=["date_parse", "amount_parse"],
            extraction_payload={},
            raw_text_preview="",
        ),
    ]

    summary = _build_summary(evaluations)

    assert summary["total_samples"] == 2
    assert summary["overall_pass_count"] == 1
    assert summary["overall_pass_rate"] == 0.5
    assert summary["date_accuracy"] == 0.5
    assert summary["merchant_accuracy"] == 1.0
    assert summary["amount_accuracy"] == 0.5
    assert summary["payment_method_accuracy"] == 1.0
    assert summary["split_counts"] == {"dev": 1, "test": 1}
    assert summary["difficulty_counts"] == {"easy": 1, "hard": 1}


def test_parse_payment_method_supports_aliases() -> None:
    assert _parse_payment_method("naver_pay") == PaymentMethod.ONLINE_PAYMENT
    assert _parse_payment_method("cash") == PaymentMethod.CASH


def test_classify_failure_reasons_groups_common_ocr_failures() -> None:
    evaluation = ReceiptEvaluation(
        file_name="sample.jpg",
        evidence_type=EvidenceType.PHYSICAL_RECEIPT,
        notes="많이 구겨진 영수증",
        split="test",
        difficulty="hard",
        overall_pass=False,
        date_result=_evaluate_scalar("2026-04-04", "2016-07-09"),
        merchant_result=_evaluate_merchant_name("CU 구의본점", "최근영수증발행 CU 구의"),
        amount_result=_evaluate_scalar("18800", "98021"),
        payment_method_result=_evaluate_scalar("card", "online_payment"),
        failure_reasons=[],
        extraction_payload={},
        raw_text_preview="사업자번호 116-07-98021\n서울 광진구 자양로\n최근영수증발행",
    )

    reasons = _classify_failure_reasons(evaluation)

    assert "merchant_header_noise" in reasons
    assert "merchant_address_noise" in reasons
    assert "amount_business_number" in reasons
    assert "payment_false_online" in reasons
    assert "date_parse" in reasons
    assert "image_quality_low" in reasons
