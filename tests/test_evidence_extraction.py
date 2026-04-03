from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from union_ledger.core.config import Settings
from union_ledger.main import app
from union_ledger.models.enums import EvidenceStatus, EvidenceType, ExtractionMethod, PaymentMethod
from union_ledger.services.evidence_extraction import (
    EvidenceExtractionService,
    ExtractionConfigurationError,
    ExtractionResult,
    build_ocr_attempt,
    merge_ocr_attempt_fields,
    parse_extracted_text,
    select_best_ocr_attempt,
)

client = TestClient(app)


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


def test_parse_extracted_text_for_physical_receipt() -> None:
    raw_text = """
    사업자번호 123-45-67890
    학생회관 매점
    승인일시 2026/03/21 13:22:11
    합계 5,500원
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 3, 21)
    assert parsed.merchant_name == "학생회관 매점"
    assert parsed.amount == Decimal("5500")
    assert parsed.payment_method == PaymentMethod.CARD


def test_parse_extracted_text_ignores_store_name_digits_when_extracting_amount() -> None:
    raw_text = """
    영수증 건국대 학생회관 1847카페
    아메리카노 2,000 1 2,000
    금액 4,000
    승인번호 28269942 승인금액 4,000
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.merchant_name == "건국대 학생회관 1847카페"
    assert parsed.amount == Decimal("4000")
    assert parsed.payment_method == PaymentMethod.CARD


def test_parse_extracted_text_ignores_phone_like_long_numbers() -> None:
    raw_text = """
    [영수증] 건국대 학생회관 1847카페
    3148628653 / 참인수 / 1577-9063
    금액 4,000
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.merchant_name == "건국대 학생회관 1847카페"
    assert parsed.amount == Decimal("4000")


def test_parse_extracted_text_handles_split_toll_receipt_fields() -> None:
    raw_text = """
    하이패스는
    빠르고 편리합니다
    한국도로공사
    구리남양영업소 (0 6 2 )
    2026
    2월28일18
    0#: 1
    0008! (카드)
    비씨카드 (주)
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 2, 28)
    assert parsed.merchant_name == "한국도로공사 구리남양영업소"
    assert parsed.amount == Decimal("1000")
    assert parsed.payment_method == PaymentMethod.CARD


def test_parse_extracted_text_cleans_labeled_merchant_candidate() -> None:
    raw_text = """
    거래번호: 0010010060
    거래일시: 26/03/13 16:22:59
    가맹점명: (주)잠원시더불뮤주유101:02 3438 1112
    카드
    """

    parsed = parse_extracted_text(raw_text, EvidenceType.PHYSICAL_RECEIPT)

    assert parsed.evidence_date == date(2026, 3, 13)
    assert parsed.merchant_name == "(주)창원씨더블유주유"
    assert parsed.payment_method == PaymentMethod.CARD


def test_select_best_ocr_attempt_prefers_receipt_like_candidate() -> None:
    weak_attempt = build_ocr_attempt(
        "03/21\n5500",
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="original",
        psm=11,
    )
    strong_attempt = build_ocr_attempt(
        """
        학생회관 매점
        승인일시 2026-03-21 13:22
        합계 5,500원
        카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="threshold",
        psm=6,
    )

    best_attempt = select_best_ocr_attempt([weak_attempt, strong_attempt])

    assert best_attempt.preprocessing == "threshold"
    assert best_attempt.parsed.merchant_name == "학생회관 매점"
    assert best_attempt.parsed.amount == Decimal("5500")


def test_merge_ocr_attempt_fields_uses_best_field_per_attempt() -> None:
    merchant_attempt = build_ocr_attempt(
        """
        [영수증] 건국대 학생외관 1847카페
        합계 4,000원
        카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="original",
        psm=4,
    )
    date_attempt = build_ocr_attempt(
        """
        승인일시 2026-04-03 10:29:17
        합계 4,000원
        카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="threshold",
        psm=6,
    )

    merged = merge_ocr_attempt_fields(
        [merchant_attempt, date_attempt],
        EvidenceType.PHYSICAL_RECEIPT,
    )

    assert merged.evidence_date == date(2026, 4, 3)
    assert merged.merchant_name == "건국대 학생회관 1847카페"
    assert merged.amount == Decimal("4000")
    assert merged.payment_method == PaymentMethod.CARD


def test_merge_ocr_attempt_fields_combines_brand_and_labeled_merchant() -> None:
    brand_attempt = build_ocr_attempt(
        """
        HD현대오일뱅크 Ah) At ant?
        거래일시 2026-03-13 16:22:59
        카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="threshold",
        psm=6,
    )
    merchant_attempt = build_ocr_attempt(
        """
        가맹점멸: (주)잠원시더블뮤주유101:02 3438 1112
        거래일시 2026-03-13 16:22:59
        합계 6,000원
        카드
        """,
        EvidenceType.PHYSICAL_RECEIPT,
        preprocessing="grayscale_autocontrast",
        psm=6,
    )

    merged = merge_ocr_attempt_fields(
        [brand_attempt, merchant_attempt],
        EvidenceType.PHYSICAL_RECEIPT,
    )

    assert merged.merchant_name == "HD현대오일뱅크 (주)창원씨더블유주유"
    assert merged.amount == Decimal("6000")
    assert merged.evidence_date == date(2026, 3, 13)


def test_ocr_preview_endpoint_returns_structured_result(monkeypatch) -> None:
    def fake_extract_upload(
        self: EvidenceExtractionService,
        filename: str,
        content: bytes,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        return ExtractionResult(
            source_file_name=filename,
            evidence_type=evidence_type,
            method=ExtractionMethod.OCR,
            raw_text="원본 텍스트",
            evidence_date=date(2026, 3, 21),
            merchant_name="학생회관 매점",
            amount=Decimal("5500"),
            payment_method=PaymentMethod.CARD,
            payload={
                "engine": "tesseract",
                "raw_text": "원본 텍스트",
                "confidence": 1.0,
                "review_required": True,
                "normalized_fields": {
                    "evidence_date": "2026-03-21",
                    "merchant_name": "학생회관 매점",
                    "amount": "5500",
                    "payment_method": "card",
                },
            },
        )

    monkeypatch.setattr(EvidenceExtractionService, "extract_upload", fake_extract_upload)

    response = client.post(
        "/api/v1/ocr/preview",
        data={"evidence_type": "physical_receipt"},
        files={"file": ("receipt.png", b"fake-image", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == EvidenceStatus.NEEDS_REVIEW.value
    assert payload["merchant_name"] == "학생회관 매점"
    assert payload["amount"] == "5500"


def test_service_raises_when_requested_language_data_is_missing(tmp_path: Path) -> None:
    tessdata_dir = tmp_path / "tessdata"
    tessdata_dir.mkdir()
    (tessdata_dir / "eng.traineddata").write_bytes(b"eng")

    service = EvidenceExtractionService(
        Settings(
            tesseract_cmd=r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            tessdata_dir=tessdata_dir,
            ocr_languages="kor+eng",
        )
    )

    with pytest.raises(ExtractionConfigurationError):
        service._ensure_requested_languages_available(tessdata_dir)
