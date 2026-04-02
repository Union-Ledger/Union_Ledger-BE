from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from union_ledger.core.config import Settings, get_settings
from union_ledger.models.enums import EvidenceType, ExtractionMethod, PaymentMethod
from union_ledger.services.file_storage import SUPPORTED_EVIDENCE_SUFFIXES

IMAGE_SUFFIXES = SUPPORTED_EVIDENCE_SUFFIXES - {".pdf"}
DATE_KEYWORDS = ("거래일", "거래일시", "승인일", "승인일시", "결제일", "일시", "날짜", "date")
MERCHANT_PATTERNS = (
    r"(?:가맹점|상호명|상호|사용처|판매처|merchant|store|받는분)\s*[:：]?\s*(.+)",
)
AMOUNT_KEYWORDS = {
    "총 결제금액": 8,
    "결제금액": 7,
    "승인금액": 7,
    "이체금액": 7,
    "출금액": 6,
    "합계": 6,
    "총액": 6,
    "금액": 4,
    "amount": 6,
    "total": 6,
    "paid": 5,
}
DATE_PATTERNS = (
    re.compile(
        r"(?P<year>\d{4})\s*[./-년]\s*(?P<month>\d{1,2})\s*[./-월]\s*(?P<day>\d{1,2})"
    ),
    re.compile(r"(?P<year>\d{2})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"),
)
AMOUNT_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)")


class ExtractionError(RuntimeError):
    """Raised when evidence extraction fails."""


class ExtractionConfigurationError(ExtractionError):
    """Raised when extraction dependencies are not ready."""


@dataclass(slots=True)
class ParsedEvidenceFields:
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    confidence: float


@dataclass(slots=True)
class ExtractionResult:
    source_file_name: str
    evidence_type: EvidenceType
    method: ExtractionMethod
    raw_text: str
    evidence_date: date | None
    merchant_name: str | None
    amount: Decimal | None
    payment_method: PaymentMethod | None
    payload: dict


def parse_extracted_text(text: str, evidence_type: EvidenceType) -> ParsedEvidenceFields:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    evidence_date = _extract_date(lines)
    amount = _extract_amount(lines)
    merchant_name = _extract_merchant_name(lines)
    payment_method = _extract_payment_method(text, evidence_type)
    found_count = sum(
        value is not None for value in (evidence_date, merchant_name, amount, payment_method)
    )
    return ParsedEvidenceFields(
        evidence_date=evidence_date,
        merchant_name=merchant_name,
        amount=amount,
        payment_method=payment_method,
        confidence=round(found_count / 4, 2),
    )


class EvidenceExtractionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def extract_upload(
        self,
        filename: str,
        content: bytes,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf":
            return self._build_result(
                source_file_name=filename,
                evidence_type=evidence_type,
                method=ExtractionMethod.PDF_TEXT,
                raw_text=self._extract_pdf_text(content),
                engine="pypdf",
            )
        if suffix in IMAGE_SUFFIXES:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                tmp_file.write(content)
                tmp_path = Path(tmp_file.name)
            try:
                return self._build_result(
                    source_file_name=filename,
                    evidence_type=evidence_type,
                    method=ExtractionMethod.OCR,
                    raw_text=self._extract_image_text(tmp_path),
                    engine="tesseract",
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        supported = ", ".join(sorted(SUPPORTED_EVIDENCE_SUFFIXES))
        raise ExtractionError(f"지원하지 않는 파일 형식입니다. 지원 확장자: {supported}")

    def extract_file(self, file_path: Path, evidence_type: EvidenceType) -> ExtractionResult:
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            return self._build_result(
                source_file_name=file_path.name,
                evidence_type=evidence_type,
                method=ExtractionMethod.PDF_TEXT,
                raw_text=self._extract_pdf_text(file_path.read_bytes()),
                engine="pypdf",
            )
        if suffix in IMAGE_SUFFIXES:
            return self._build_result(
                source_file_name=file_path.name,
                evidence_type=evidence_type,
                method=ExtractionMethod.OCR,
                raw_text=self._extract_image_text(file_path),
                engine="tesseract",
            )
        supported = ", ".join(sorted(SUPPORTED_EVIDENCE_SUFFIXES))
        raise ExtractionError(f"지원하지 않는 파일 형식입니다. 지원 확장자: {supported}")

    def _build_result(
        self,
        *,
        source_file_name: str,
        evidence_type: EvidenceType,
        method: ExtractionMethod,
        raw_text: str,
        engine: str,
    ) -> ExtractionResult:
        cleaned_text = raw_text.strip()
        if not cleaned_text:
            raise ExtractionError("텍스트를 추출하지 못했습니다.")

        parsed = parse_extracted_text(cleaned_text, evidence_type)
        payload = {
            "engine": engine,
            "raw_text": cleaned_text,
            "confidence": parsed.confidence,
            "review_required": True,
            "normalized_fields": {
                "evidence_date": (
                    parsed.evidence_date.isoformat() if parsed.evidence_date else None
                ),
                "merchant_name": parsed.merchant_name,
                "amount": str(parsed.amount) if parsed.amount is not None else None,
                "payment_method": (
                    parsed.payment_method.value if parsed.payment_method is not None else None
                ),
            },
        }
        return ExtractionResult(
            source_file_name=source_file_name,
            evidence_type=evidence_type,
            method=method,
            raw_text=cleaned_text,
            evidence_date=parsed.evidence_date,
            merchant_name=parsed.merchant_name,
            amount=parsed.amount,
            payment_method=parsed.payment_method,
            payload=payload,
        )

    def _extract_pdf_text(self, content: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ExtractionConfigurationError(
                "PDF 텍스트 추출을 위해 `pypdf` 패키지가 필요합니다."
            ) from exc

        reader = PdfReader(BytesIO(content))
        return "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()

    def _extract_image_text(self, file_path: Path) -> str:
        tesseract_cmd = self._resolve_tesseract_cmd()
        completed = subprocess.run(
            [tesseract_cmd, str(file_path), "stdout", "-l", self._settings.ocr_languages],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "알 수 없는 OCR 오류"
            raise ExtractionError(f"Tesseract OCR 실행에 실패했습니다: {detail}")
        return completed.stdout.strip()

    def _resolve_tesseract_cmd(self) -> str:
        configured = self._settings.tesseract_cmd.strip()
        if configured and Path(configured).exists():
            return configured

        discovered = shutil.which("tesseract")
        if discovered:
            return discovered

        raise ExtractionConfigurationError(
            "Tesseract 실행 파일을 찾지 못했습니다. TESSERACT_CMD 설정을 확인해주세요."
        )


def _extract_date(lines: list[str]) -> date | None:
    candidates: list[tuple[int, date]] = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        keyword_bonus = 5 if any(keyword in lowered for keyword in DATE_KEYWORDS) else 0
        for pattern in DATE_PATTERNS:
            for match in pattern.finditer(line):
                parsed = _safe_build_date(
                    year=match.group("year"),
                    month=match.group("month"),
                    day=match.group("day"),
                )
                if parsed is None:
                    continue
                candidates.append((keyword_bonus + max(0, 10 - index), parsed))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_amount(lines: list[str]) -> Decimal | None:
    candidates: list[tuple[int, Decimal]] = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        keyword_score = sum(
            score for keyword, score in AMOUNT_KEYWORDS.items() if keyword in lowered
        )
        for raw_number in AMOUNT_PATTERN.findall(line):
            amount = _to_decimal(raw_number)
            if amount is None or amount < Decimal("100"):
                continue
            candidates.append((keyword_score + max(0, 10 - index), amount))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][1]


def _extract_merchant_name(lines: list[str]) -> str | None:
    for line in lines:
        for pattern in MERCHANT_PATTERNS:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                merchant_name = match.group(1).strip(" :-")
                if merchant_name:
                    return merchant_name

    for line in lines:
        if _looks_like_merchant_name(line):
            return line
    return None


def _extract_payment_method(text: str, evidence_type: EvidenceType) -> PaymentMethod | None:
    lowered = text.lower()
    if any(keyword in lowered for keyword in ("계좌이체", "이체", "출금계좌")):
        return PaymentMethod.BANK_TRANSFER
    if any(
        keyword in lowered for keyword in ("네이버페이", "카카오페이", "토스페이", "온라인", "pay")
    ):
        return PaymentMethod.ONLINE_PAYMENT
    if any(keyword in lowered for keyword in ("카드", "승인번호", "카드번호")):
        return PaymentMethod.CARD
    if evidence_type is EvidenceType.BANK_TRANSFER_STATEMENT:
        return PaymentMethod.BANK_TRANSFER
    if evidence_type is EvidenceType.E_RECEIPT:
        return PaymentMethod.ONLINE_PAYMENT
    if evidence_type is EvidenceType.PHYSICAL_RECEIPT:
        return PaymentMethod.CARD
    return None


def _safe_build_date(*, year: str, month: str, day: str) -> date | None:
    try:
        year_value = int(year)
        if year_value < 100:
            year_value += 2000
        return date(year_value, int(month), int(day))
    except ValueError:
        return None


def _to_decimal(raw_number: str) -> Decimal | None:
    try:
        return Decimal(raw_number.replace(",", ""))
    except InvalidOperation:
        return None


def _looks_like_merchant_name(line: str) -> bool:
    lowered = line.lower()
    if any(keyword in lowered for keyword in ("합계", "금액", "총액", "거래일", "승인일", "카드")):
        return False
    if AMOUNT_PATTERN.search(line):
        return False
    if any(pattern.search(line) for pattern in DATE_PATTERNS):
        return False
    return bool(re.search(r"[A-Za-z가-힣]", line)) and len(line) >= 2
