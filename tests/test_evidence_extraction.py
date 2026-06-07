from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from union_ledger.api.deps.auth import get_current_user
from union_ledger.main import app
from union_ledger.models.enums import (
    EvidenceStatus,
    EvidenceType,
    ExtractionMethod,
    PaymentMethod,
    RoleType,
)
from union_ledger.schemas.auth_response import AuthUser
from union_ledger.services.evidence_extraction import (
    EvidenceExtractionService,
    ExtractionResult,
    OCRLine,
    build_ocr_attempt,
    merge_ocr_attempt_fields,
    parse_extracted_text,
    select_best_ocr_attempt,
)

client = TestClient(app)


def _ocr_line(
    text: str,
    *,
    left: float = 0,
    top: float = 0,
    right: float = 240,
    bottom: float = 18,
    confidence: float = 0.95,
) -> OCRLine:
    return OCRLine(
        text=text,
        confidence=confidence,
        box=[[left, top], [right, top], [right, bottom], [left, bottom]],
        y=top,
        x=left,
    )


def test_parse_extracted_text_for_bank_transfer_statement() -> None:
    raw_text = """
    거래일시 2026-03-19 14:22
    받는분 학생식당
    이체금액 12,300원
    출금계좌 123-45-67890
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.BANK_TRANSFER_STATEMENT)

    assert parsed.evidence_date == date(2026, 3, 19)
    assert parsed.merchant_name == "학생식당"
    assert parsed.amount == Decimal("12300")
    assert parsed.payment_method == PaymentMethod.BANK_TRANSFER


def test_parse_extracted_text_for_online_receipt() -> None:
    raw_text = """
    네이버페이 주문
    결제일 2026.03.20
    판매처 캠퍼스문구
    결제금액 45,000원
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.E_RECEIPT)

    assert parsed.evidence_date == date(2026, 3, 20)
    assert parsed.merchant_name == "캠퍼스문구"
    assert parsed.amount == Decimal("45000")
    assert parsed.payment_method == PaymentMethod.ONLINE_PAYMENT


def test_parse_extracted_text_for_cash_receipt() -> None:
    raw_text = """
    CU 구의본점
    거래일시 2026.04.04 11:20
    현금결제
    받을금액 2,720원
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 4, 4)
    assert parsed.merchant_name == "CU 구의본점"
    assert parsed.amount == Decimal("2720")
    assert parsed.payment_method == PaymentMethod.CASH


def test_parse_extracted_text_for_physical_receipt() -> None:
    raw_text = """
    사업자번호 123-45-67890
    건국대 학생회관 1847카페
    승인일시 2026/03/21 13:22:11
    합계 5,500원 카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 3, 21)
    assert parsed.merchant_name == "건국대 학생회관 1847카페"
    assert parsed.amount == Decimal("5500")
    assert parsed.payment_method == PaymentMethod.CARD


def test_parse_extracted_text_ignores_business_number_for_amount() -> None:
    raw_text = """
    원플레이 건대점 사업자번호 116-07-98021
    결제금액 18,800원 카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.merchant_name == "원플레이 건대점"
    assert parsed.amount == Decimal("18800")


def test_parse_extracted_text_prefers_total_over_line_item() -> None:
    raw_text = """
    건국대 학생회관 1847카페
    아메리카노 2,000원
    샌드위치 2,500원
    결제금액 4,500원
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.amount == Decimal("4500")
    assert parsed.payment_method == PaymentMethod.CARD


def test_parse_extracted_text_supports_compact_date() -> None:
    raw_text = """
    판매시간 20260404 23:54:53
    합계 28,200원
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 4, 4)
    assert parsed.amount == Decimal("28200")


def test_parse_extracted_text_prefers_total_label_neighbor_value_with_layout_lines() -> None:
    lines = [
        _ocr_line("CU 구의본점", top=0, bottom=18),
        _ocr_line("결제금액", top=180, bottom=198),
        _ocr_line("6,360", top=205, bottom=223),
        _ocr_line("NO:3859 15:19", top=260, bottom=278),
    ]

    parsed = parse_extracted_text(
        "\n".join(line.text for line in lines),
        EvidenceType.PHYSICAL_RECEIPT,
        ocr_lines=lines,
    )

    assert parsed.amount == Decimal("6360")


def test_parse_extracted_text_prefers_richer_top_merchant_line_with_layout_lines() -> None:
    lines = [
        _ocr_line("Again", top=0, bottom=18),
        _ocr_line("CU 구의본점", top=24, bottom=42),
        _ocr_line("사업자등록번호 207-41-02918", top=50, bottom=68),
    ]

    parsed = parse_extracted_text(
        "\n".join(line.text for line in lines),
        EvidenceType.PHYSICAL_RECEIPT,
        ocr_lines=lines,
    )

    assert parsed.merchant_name == "CU 구의본점"


def test_parse_extracted_text_prefers_plausible_date_near_label_with_layout_lines() -> None:
    lines = [
        _ocr_line("거래일시", top=120, bottom=138),
        _ocr_line("2026/04/03 11:04", top=146, bottom=164),
        _ocr_line("2028-04-03 10:29", top=320, bottom=338),
    ]

    parsed = parse_extracted_text(
        "\n".join(line.text for line in lines),
        EvidenceType.PHYSICAL_RECEIPT,
        ocr_lines=lines,
    )

    assert parsed.evidence_date == date(2026, 4, 3)


def test_select_best_ocr_attempt_prefers_more_complete_attempt() -> None:
    weak_attempt = build_ocr_attempt(
        "03/21\n5500",
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="original",
        average_confidence=0.71,
        line_count=2,
    )
    strong_attempt = build_ocr_attempt(
        """
        건국대 학생회관 1847카페
        승인일시 2026-03-21 13:22
        합계 5,500원 카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="adaptive_binary",
        average_confidence=0.83,
        line_count=3,
    )

    best_attempt = select_best_ocr_attempt([weak_attempt, strong_attempt])

    assert best_attempt.preprocessing == "adaptive_binary"
    assert best_attempt.parsed.merchant_name == "건국대 학생회관 1847카페"
    assert best_attempt.parsed.amount == Decimal("5500")


def test_merge_ocr_attempt_fields_uses_best_field_per_attempt() -> None:
    merchant_attempt = build_ocr_attempt(
        """
        건국대 학생회관 1847카페
        합계 4,000원 카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="background_cropped",
        average_confidence=0.84,
        line_count=2,
    )
    date_attempt = build_ocr_attempt(
        """
        승인일시 2026-04-03 10:29:17
        합계 4,000원 카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="document_enhanced",
        average_confidence=0.81,
        line_count=2,
    )

    merged = merge_ocr_attempt_fields(
        [merchant_attempt, date_attempt],
        EvidenceType.PHYSICAL_RECEIPT,
    )

    assert merged.evidence_date == date(2026, 4, 3)
    assert merged.merchant_name == "건국대 학생회관 1847카페"
    assert merged.amount == Decimal("4000")
    assert merged.payment_method == PaymentMethod.CARD


def test_ocr_preview_endpoint_returns_structured_result(monkeypatch) -> None:
    def fake_extract_upload(
        self: EvidenceExtractionService,
        filename: str,
        content: bytes,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        del self, content
        return ExtractionResult(
            source_file_name=filename,
            evidence_type=evidence_type,
            method=ExtractionMethod.OCR,
            raw_text="건국대 학생회관 1847카페\n합계 5,500원\n카드",
            evidence_date=date(2026, 3, 21),
            merchant_name="건국대 학생회관 1847카페",
            amount=Decimal("5500"),
            payment_method=PaymentMethod.CARD,
            payload={
                "engine": "paddleocr",
                "raw_text": "건국대 학생회관 1847카페\n합계 5,500원\n카드",
                "confidence": 0.94,
                "review_required": True,
                "normalized_fields": {
                    "evidence_date": "2026-03-21",
                    "merchant_name": "건국대 학생회관 1847카페",
                    "amount": "5500",
                    "payment_method": "card",
                },
            },
        )

    monkeypatch.setattr(EvidenceExtractionService, "extract_upload", fake_extract_upload)

    # /ocr/preview requires a treasurer/admin (OCR is expensive, so it isn't
    # public). Override the auth dependency so this test exercises the response
    # shape rather than the auth flow, which is covered by the auth tests.
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        id=uuid.uuid4(),
        email="treasurer@konkuk.ac.kr",
        name="재정담당",
        roles=[RoleType.TREASURER],
    )
    try:
        response = client.post(
            "/api/v1/ocr/preview",
            data={"evidence_type": "physical_receipt"},
            files={"file": ("receipt.png", b"fake-image", "image/png")},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == EvidenceStatus.NEEDS_REVIEW.value
    assert payload["merchant_name"] == "건국대 학생회관 1847카페"
    assert payload["amount"] == "5500"
