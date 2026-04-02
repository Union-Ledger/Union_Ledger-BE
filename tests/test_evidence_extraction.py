from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from union_ledger.main import app
from union_ledger.models.enums import EvidenceStatus, EvidenceType, ExtractionMethod, PaymentMethod
from union_ledger.services.evidence_extraction import (
    EvidenceExtractionService,
    ExtractionResult,
    parse_extracted_text,
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
