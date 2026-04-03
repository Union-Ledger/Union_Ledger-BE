from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any

from union_ledger.core.config import Settings, get_settings
from union_ledger.models.enums import EvidenceType, ExtractionMethod, PaymentMethod
from union_ledger.services.file_storage import SUPPORTED_EVIDENCE_SUFFIXES

IMAGE_SUFFIXES = SUPPORTED_EVIDENCE_SUFFIXES - {".pdf"}
DATE_KEYWORDS = ("거래일", "거래일시", "승인일", "승인일시", "결제일", "일시", "날짜", "date")
RECEIPT_HINT_KEYWORDS = (
    "승인",
    "승인번호",
    "카드",
    "매출",
    "합계",
    "총액",
    "가맹점",
    "금액",
)
TRANSFER_HINT_KEYWORDS = ("거래일", "이체", "출금계좌", "입금", "받는분")
ONLINE_HINT_KEYWORDS = ("주문", "결제", "페이", "pay", "온라인")
MERCHANT_PATTERNS = (
    r"(?:가맹점멸|가맹절명|가맹점명|가맹점|상호명|상호|사용처|판매처|merchant|store|받는분)\s*[:：]?\s*(.+)",
)
MERCHANT_STOPWORDS = (
    "사업자",
    "대표",
    "전화",
    "tel",
    "승인",
    "거래",
    "결제",
    "금액",
    "합계",
    "총액",
    "카드",
    "vat",
    "부가세",
    "공급가액",
    "계좌",
    "주문",
    "번호",
    "테이블",
    "take out",
    "kiosk",
    "포스",
    "pos",
    "편리합니다",
    "helpdesk",
)
MERCHANT_BOOST_KEYWORDS = (
    "카페",
    "식당",
    "주유",
    "영업소",
    "매점",
    "도로공사",
    "학생회관",
    "오일뱅크",
    "휴게소",
    "점",
)
AMOUNT_KEYWORDS = {
    "총 결제금액": 10,
    "총결제금액": 10,
    "결제금액": 9,
    "승인금액": 9,
    "이체금액": 9,
    "총액": 8,
    "합계": 8,
    "청구금액": 7,
    "출금액": 7,
    "금액": 5,
    "amount": 6,
    "total": 6,
    "paid": 5,
}
DATE_PATTERNS = (
    re.compile(
        r"(?P<year>\d{4})\s*[-./년]\s*(?P<month>\d{1,2})\s*[-./월]\s*(?P<day>\d{1,2})"
    ),
    re.compile(r"(?P<year>\d{2})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"),
    re.compile(
        r"(?P<year>\d{4})\s*(?:년)?\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
    ),
)
AMOUNT_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)")
CURRENCY_HINTS = ("원", "krw", "￦", "won")
AMOUNT_CONTEXT_REJECTION_KEYWORDS = ("번호", "전화", "tel", "사업자", "pos", "포스", "주문")
MERCHANT_FIXUP_PATTERNS = (
    (re.compile(r"학생외[관과]"), "학생회관"),
    (re.compile(r"학[새붕]식[당비]"), "학생식당"),
    (re.compile(r"항국도로공사|하국도로공사"), "한국도로공사"),
    (re.compile(r"[ᄀㄱ]리남양영업소"), "구리남양영업소"),
    (re.compile(r"참원|잠원"), "창원"),
    (re.compile(r"시더불뮤|시더블뮤|더불뮤|더블뮤"), "씨더블유"),
    (re.compile(r"요월빙크|오일빙크|오잃뱅크"), "오일뱅크"),
)


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
class OCRAttempt:
    preprocessing: str
    psm: int
    raw_text: str
    parsed: ParsedEvidenceFields
    score: float


@dataclass(slots=True)
class PreparedImageVariant:
    label: str
    path: Path


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
    payload: dict[str, Any]


def normalize_extracted_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    cleaned_lines: list[str] = []
    for line in normalized.splitlines():
        compact = re.sub(r"\s+", " ", line).strip(" \t|")
        compact = compact.replace("₩", "원")
        if compact:
            cleaned_lines.append(compact)
    return "\n".join(cleaned_lines)


def parse_extracted_text(text: str, evidence_type: EvidenceType) -> ParsedEvidenceFields:
    normalized_text = normalize_extracted_text(text)
    lines = [line.strip() for line in normalized_text.splitlines() if line.strip()]
    evidence_date = _extract_date(lines)
    amount = _extract_amount(lines)
    merchant_name = _extract_merchant_name(lines)
    payment_method = _extract_payment_method(normalized_text, evidence_type)
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


def build_ocr_attempt(
    raw_text: str,
    evidence_type: EvidenceType,
    *,
    preprocessing: str,
    psm: int,
) -> OCRAttempt:
    normalized_text = normalize_extracted_text(raw_text)
    parsed = parse_extracted_text(normalized_text, evidence_type)
    score = _score_ocr_attempt(normalized_text, parsed, evidence_type, preprocessing, psm)
    return OCRAttempt(
        preprocessing=preprocessing,
        psm=psm,
        raw_text=normalized_text,
        parsed=parsed,
        score=score,
    )


def select_best_ocr_attempt(attempts: list[OCRAttempt]) -> OCRAttempt:
    populated_attempts = [attempt for attempt in attempts if attempt.raw_text]
    if not populated_attempts:
        raise ExtractionError("텍스트를 추출하지 못했습니다.")
    return max(populated_attempts, key=lambda attempt: (attempt.score, len(attempt.raw_text)))


def merge_ocr_attempt_fields(
    attempts: list[OCRAttempt],
    evidence_type: EvidenceType,
) -> ParsedEvidenceFields:
    evidence_date = _select_best_date(attempts)
    merchant_name = _select_best_merchant_name(attempts)
    amount = _select_best_amount(attempts)
    payment_method = _select_best_payment_method(attempts, evidence_type)
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
        self._resolved_tessdata_dir: Path | None = None
        self._validated_languages: tuple[str, ...] | None = None

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
                return self._extract_image_result(tmp_path, filename, evidence_type)
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
            return self._extract_image_result(file_path, file_path.name, evidence_type)
        supported = ", ".join(sorted(SUPPORTED_EVIDENCE_SUFFIXES))
        raise ExtractionError(f"지원하지 않는 파일 형식입니다. 지원 확장자: {supported}")

    def _extract_image_result(
        self,
        file_path: Path,
        source_file_name: str,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        attempts = self._collect_image_ocr_attempts(file_path, evidence_type)
        best_attempt = select_best_ocr_attempt(attempts)
        merged_fields = merge_ocr_attempt_fields(attempts, evidence_type)
        payload = {
            "selected_attempt": self._serialize_ocr_attempt(best_attempt),
            "attempts": [self._serialize_ocr_attempt(attempt) for attempt in attempts],
            "field_fusion": {
                "enabled": True,
                "normalized_fields": _serialize_parsed_fields(merged_fields),
            },
        }
        return self._build_result(
            source_file_name=source_file_name,
            evidence_type=evidence_type,
            method=ExtractionMethod.OCR,
            raw_text=best_attempt.raw_text,
            engine="tesseract",
            extra_payload=payload,
            parsed_override=merged_fields,
        )

    def _collect_image_ocr_attempts(
        self,
        file_path: Path,
        evidence_type: EvidenceType,
    ) -> list[OCRAttempt]:
        attempts: list[OCRAttempt] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            variants = self._prepare_image_variants(file_path, Path(temp_dir), evidence_type)
            for variant in variants:
                for psm in self._get_psm_modes(evidence_type):
                    raw_text = self._run_tesseract_ocr(variant.path, psm)
                    attempt = build_ocr_attempt(
                        raw_text,
                        evidence_type,
                        preprocessing=variant.label,
                        psm=psm,
                    )
                    if attempt.raw_text:
                        attempts.append(attempt)
        return attempts

    def _prepare_image_variants(
        self,
        file_path: Path,
        temp_dir: Path,
        evidence_type: EvidenceType,
    ) -> list[PreparedImageVariant]:
        try:
            from PIL import Image, ImageFilter, ImageOps
        except ImportError:
            return [PreparedImageVariant(label="original", path=file_path)]

        with Image.open(file_path) as source_image:
            base_image = ImageOps.exif_transpose(source_image).convert("RGB")
            base_image = self._resize_image_for_ocr(base_image, Image)
            variants = [self._save_image_variant(temp_dir, base_image, "original")]
            grayscale = ImageOps.autocontrast(ImageOps.grayscale(base_image))
            sharpened = grayscale.filter(ImageFilter.SHARPEN)

            variants.append(
                self._save_image_variant(
                    temp_dir,
                    grayscale,
                    "grayscale_autocontrast",
                )
            )
            variants.append(self._save_image_variant(temp_dir, sharpened, "sharpened"))

            if evidence_type is EvidenceType.PHYSICAL_RECEIPT:
                threshold = grayscale.point(lambda pixel: 255 if pixel > 170 else 0)
                variants.append(self._save_image_variant(temp_dir, threshold, "threshold"))

                width, height = threshold.size
                if max(width, height) < 2200:
                    resampling = getattr(Image, "Resampling", Image)
                    upscaled = threshold.resize(
                        (width * 2, height * 2),
                        resampling.LANCZOS,
                    )
                    variants.append(
                        self._save_image_variant(temp_dir, upscaled, "threshold_upscaled")
                    )

        return variants

    def _resize_image_for_ocr(self, image: Any, image_module: Any) -> Any:
        max_dimension = max(image.size)
        target_max_dimension = 2200
        if max_dimension <= target_max_dimension:
            return image

        scale = target_max_dimension / max_dimension
        resized_size = (
            max(1, int(image.size[0] * scale)),
            max(1, int(image.size[1] * scale)),
        )
        resampling = getattr(image_module, "Resampling", image_module)
        return image.resize(resized_size, resampling.LANCZOS)

    def _save_image_variant(
        self,
        temp_dir: Path,
        image: Any,
        label: str,
    ) -> PreparedImageVariant:
        variant_path = temp_dir / f"{label}.png"
        image.save(variant_path, format="PNG")
        return PreparedImageVariant(label=label, path=variant_path)

    def _get_psm_modes(self, evidence_type: EvidenceType) -> tuple[int, ...]:
        if evidence_type is EvidenceType.PHYSICAL_RECEIPT:
            return (4, 6, 11)
        return (6, 11)

    def _build_result(
        self,
        *,
        source_file_name: str,
        evidence_type: EvidenceType,
        method: ExtractionMethod,
        raw_text: str,
        engine: str,
        extra_payload: dict[str, Any] | None = None,
        parsed_override: ParsedEvidenceFields | None = None,
    ) -> ExtractionResult:
        cleaned_text = normalize_extracted_text(raw_text)
        if not cleaned_text:
            raise ExtractionError("텍스트를 추출하지 못했습니다.")

        parsed = parsed_override or parse_extracted_text(cleaned_text, evidence_type)
        payload: dict[str, Any] = {
            "engine": engine,
            "raw_text": cleaned_text,
            "confidence": parsed.confidence,
            "review_required": True,
            "normalized_fields": _serialize_parsed_fields(parsed),
        }
        if extra_payload:
            payload.update(extra_payload)
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

    def _run_tesseract_ocr(self, file_path: Path, psm: int) -> str:
        tesseract_cmd = self._resolve_tesseract_cmd()
        tessdata_dir = self._resolve_tessdata_dir(tesseract_cmd)
        self._ensure_requested_languages_available(tessdata_dir)

        command = [
            tesseract_cmd,
            str(file_path),
            "stdout",
            "-l",
            self._settings.ocr_languages,
        ]
        if tessdata_dir is not None:
            command.extend(["--tessdata-dir", str(tessdata_dir)])

        command.extend(
            [
                "--oem",
                "1",
                "--psm",
                str(psm),
                "-c",
                "preserve_interword_spaces=1",
            ]
        )
        completed = subprocess.run(
            command,
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

    def _resolve_tessdata_dir(self, tesseract_cmd: str) -> Path | None:
        if self._resolved_tessdata_dir is not None:
            return self._resolved_tessdata_dir

        configured = self._settings.tessdata_dir
        if configured.exists():
            self._resolved_tessdata_dir = configured.resolve()
            return self._resolved_tessdata_dir

        system_tessdata_dir = Path(tesseract_cmd).resolve().parent / "tessdata"
        if system_tessdata_dir.exists():
            self._resolved_tessdata_dir = system_tessdata_dir
            return self._resolved_tessdata_dir

        self._resolved_tessdata_dir = None
        return None

    def _ensure_requested_languages_available(self, tessdata_dir: Path | None) -> None:
        requested_languages = tuple(
            language.strip()
            for language in self._settings.ocr_languages.split("+")
            if language.strip()
        )
        if not requested_languages:
            raise ExtractionConfigurationError("OCR 언어 설정이 비어 있습니다.")
        if self._validated_languages == requested_languages:
            return

        if tessdata_dir is None:
            raise ExtractionConfigurationError("Tesseract tessdata 디렉터리를 찾지 못했습니다.")

        missing_languages = [
            language
            for language in requested_languages
            if not (tessdata_dir / f"{language}.traineddata").exists()
        ]
        if missing_languages:
            available_languages = sorted(
                traineddata_file.stem for traineddata_file in tessdata_dir.glob("*.traineddata")
            )
            raise ExtractionConfigurationError(
                "요청한 OCR 언어팩이 없습니다. "
                f"requested={requested_languages}, missing={missing_languages}, "
                f"available={available_languages}, tessdata_dir={tessdata_dir}"
            )

        self._validated_languages = requested_languages

    def _serialize_ocr_attempt(self, attempt: OCRAttempt) -> dict[str, Any]:
        return {
            "preprocessing": attempt.preprocessing,
            "psm": attempt.psm,
            "score": attempt.score,
            "confidence": attempt.parsed.confidence,
            "normalized_fields": _serialize_parsed_fields(attempt.parsed),
            "text_preview": attempt.raw_text[:240],
        }


def _extract_date(lines: list[str]) -> date | None:
    candidates: list[tuple[int, date]] = []
    for index, window_size, segment in _iter_line_windows(lines, max_window_size=3):
        lowered = segment.lower()
        keyword_bonus = 5 if any(keyword in lowered for keyword in DATE_KEYWORDS) else 0
        for pattern in DATE_PATTERNS:
            for match in pattern.finditer(segment):
                parsed = _safe_build_date(
                    year=match.group("year"),
                    month=match.group("month"),
                    day=match.group("day"),
                )
                if parsed is None:
                    continue
                window_bonus = 2 if window_size > 1 else 0
                candidates.append((keyword_bonus + window_bonus + max(0, 10 - index), parsed))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_amount(lines: list[str]) -> Decimal | None:
    candidates: list[tuple[int, Decimal]] = []
    for index, window_size, segment in _iter_line_windows(lines, max_window_size=2):
        lowered = segment.lower()
        if any(keyword in lowered for keyword in ("사업자", "전화", "tel", "카드번호")):
            continue

        has_amount_keywords = any(keyword in lowered for keyword in AMOUNT_KEYWORDS)
        keyword_score = sum(
            score for keyword, score in AMOUNT_KEYWORDS.items() if keyword in lowered
        )
        currency_bonus = 5 if any(currency in lowered for currency in CURRENCY_HINTS) else 0
        if any(
            keyword in lowered for keyword in ("가맹점", "상호", "merchant", "store", "판매처")
        ) and not (has_amount_keywords or currency_bonus):
            continue
        has_date = any(pattern.search(segment) for pattern in DATE_PATTERNS)
        if has_date and not (has_amount_keywords or currency_bonus):
            continue
        loose_numbers = _find_loose_amount_numbers(
            segment,
            allow_short_groups=bool(has_amount_keywords or currency_bonus),
        )
        standard_numbers = [match.group(1) for match in AMOUNT_PATTERN.finditer(segment)]
        number_strings = loose_numbers or standard_numbers
        number_strings = _dedupe_preserving_order(number_strings)
        if len(number_strings) > 1 and not (has_amount_keywords or currency_bonus):
            continue

        for raw_number in number_strings:
            amount = _to_decimal(raw_number)
            if amount is None or amount < Decimal("100"):
                continue
            digit_count = len(raw_number.replace(",", ""))
            if digit_count >= 6 and not (has_amount_keywords or currency_bonus):
                continue
            if Decimal("2000") <= amount <= Decimal("2100") and not (
                has_amount_keywords or currency_bonus
            ):
                continue
            candidate_score = keyword_score + currency_bonus
            if amount % Decimal("1000") == 0:
                candidate_score += 3
            elif amount % Decimal("100") == 0:
                candidate_score += 1

            if any(keyword in lowered for keyword in AMOUNT_CONTEXT_REJECTION_KEYWORDS):
                continue

            window_bonus = 2 if window_size > 1 else 0
            candidates.append(
                (
                    candidate_score + window_bonus + max(0, 10 - index),
                    amount,
                )
            )

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][1]


def _extract_merchant_name(lines: list[str]) -> str | None:
    candidates: list[tuple[int, str]] = []
    candidates.extend(_extract_merchant_name_from_labels(lines))
    for index, window_size, segment in _iter_line_windows(lines, max_window_size=2, limit=12):
        candidate = _sanitize_merchant_candidate(segment)
        if candidate is None:
            continue
        candidates.append((_score_merchant_candidate(candidate, index, window_size), candidate))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_merchant_name_from_labels(lines: list[str]) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        for pattern in MERCHANT_PATTERNS:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                candidate = _sanitize_merchant_candidate(match.group(1))
                if candidate is None:
                    candidate = _sanitize_labeled_merchant_candidate(match.group(1))
                if candidate is not None:
                    score = _score_merchant_candidate(candidate, index, window_size=1) + 24
                    candidates.append((score, candidate))
    return candidates


def _sanitize_merchant_candidate(line: str) -> str | None:
    candidate = line.strip(" :-")
    candidate = re.sub(r"^[^A-Za-z가-힣(]+", "", candidate)
    candidate = re.sub(r"^(영수증|매출전표|영수증번호)\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"^\[[^\]]+\]\s*[_-]*\s*", "", candidate)
    candidate = re.sub(r"^#+\s*", "", candidate)
    candidate = re.sub(r"^[\]\[(){}<>|]+\s*", "", candidate)
    candidate = re.sub(r"\s*\(\s*[0-9OIlSB ]{1,8}\s*\)\s*$", "", candidate)
    candidate = re.sub(r"([가-힣)])\d{1,3}:\d{2}(?::\d{2})?(?:\s+\d{2,4})*$", r"\1", candidate)
    candidate = re.sub(r"\s+\d{1,3}:\d{2}(?::\d{2})?(?:\s+\d{2,4})*$", "", candidate)
    candidate = re.sub(r"\s+\d{2,4}(?:[-:]\d{2,4}){1,}.*$", "", candidate)
    candidate = re.sub(r"\s+\d{3,}(?:\s+\d{2,}){1,}.*$", "", candidate)
    candidate = _apply_merchant_fixups(candidate)
    candidate = candidate.strip(" :-")
    lowered = candidate.lower()
    if len(candidate) < 2:
        return None
    if candidate.endswith("는") and len(candidate) <= 6:
        return None
    if any(keyword in lowered for keyword in MERCHANT_STOPWORDS):
        return None
    if any(currency in lowered for currency in CURRENCY_HINTS):
        return None
    if any(pattern.search(candidate) for pattern in DATE_PATTERNS):
        return None
    if re.search(r"\d{2,}-\d{2,}", candidate):
        return None
    if not re.search(r"[A-Za-z가-힣]", candidate):
        return None

    has_merchant_hint = any(keyword in candidate for keyword in MERCHANT_BOOST_KEYWORDS)
    for number_match in AMOUNT_PATTERN.finditer(candidate):
        next_character = candidate[number_match.end() : number_match.end() + 1]
        previous_character = candidate[number_match.start() - 1 : number_match.start()]
        attached_to_word = bool(
            re.match(r"[A-Za-z가-힣]", next_character)
            or re.match(r"[A-Za-z가-힣]", previous_character)
        )
        if attached_to_word and has_merchant_hint:
            continue
        return None

    return candidate


def _score_merchant_candidate(candidate: str, index: int, window_size: int) -> int:
    score = max(0, 20 - index)
    if window_size > 1:
        score += 4
    if any(token in candidate for token in MERCHANT_BOOST_KEYWORDS):
        score += 6
    if any(token in candidate for token in ("공사", "영업소", "학생회관", "학생식당")):
        score += 5
    score += min(len(re.findall(r"[A-Za-z가-힣]", candidate)), 16) // 2
    if any(character in candidate for character in "<>[]{}|"):
        score -= 8
    if "하이패스는" in candidate or "빠르고 편리합니다" in candidate:
        score -= 20
    return score


def _sanitize_labeled_merchant_candidate(value: str) -> str | None:
    candidate = value.strip(" :-")
    candidate = re.sub(r"([가-힣)])\d{1,3}:\d{2}(?::\d{2})?(?:\s+\d{2,4})*$", r"\1", candidate)
    candidate = re.sub(r"\s+\d{1,3}:\d{2}(?::\d{2})?(?:\s+\d{2,4})*$", "", candidate)
    candidate = re.sub(r"\s+\d{2,4}(?:[-:]\d{2,4}){1,}.*$", "", candidate)
    candidate = re.sub(r"\s+\d{3,}(?:\s+\d{2,}){1,}.*$", "", candidate)
    candidate = _apply_merchant_fixups(candidate).strip(" :-")
    if candidate.startswith("주)"):
        candidate = f"(주){candidate[2:]}"
    if len(candidate) < 2:
        return None
    if not re.search(r"[A-Za-z가-힣]", candidate):
        return None
    return candidate


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


def _score_ocr_attempt(
    raw_text: str,
    parsed: ParsedEvidenceFields,
    evidence_type: EvidenceType,
    preprocessing: str,
    psm: int,
) -> float:
    score = parsed.confidence * 100
    lowered = raw_text.lower()
    line_count = len([line for line in raw_text.splitlines() if line.strip()])
    score += min(line_count, 12)
    score += min(len(raw_text) / 24, 12)

    if parsed.amount is not None:
        score += 18
    if parsed.evidence_date is not None:
        score += 12
    if parsed.merchant_name is not None:
        score += 16
    if parsed.payment_method is not None:
        score += 8
    if any(currency in lowered for currency in CURRENCY_HINTS):
        score += 6

    if evidence_type is EvidenceType.PHYSICAL_RECEIPT:
        if any(keyword in lowered for keyword in RECEIPT_HINT_KEYWORDS):
            score += 12
        if preprocessing != "original":
            score += 2
        if psm in {4, 6}:
            score += 2
    elif evidence_type is EvidenceType.BANK_TRANSFER_STATEMENT:
        if any(keyword in lowered for keyword in TRANSFER_HINT_KEYWORDS):
            score += 10
    elif evidence_type is EvidenceType.E_RECEIPT:
        if any(keyword in lowered for keyword in ONLINE_HINT_KEYWORDS):
            score += 10

    return round(score, 2)


def _serialize_parsed_fields(parsed: ParsedEvidenceFields) -> dict[str, str | None]:
    return {
        "evidence_date": parsed.evidence_date.isoformat() if parsed.evidence_date else None,
        "merchant_name": parsed.merchant_name,
        "amount": str(parsed.amount) if parsed.amount is not None else None,
        "payment_method": parsed.payment_method.value if parsed.payment_method else None,
    }


def _safe_build_date(*, year: str, month: str, day: str) -> date | None:
    try:
        year_value = int(year)
        if year_value < 100:
            year_value += 2000
        current_year = date.today().year
        if year_value < 2000 or year_value > current_year + 1:
            return None
        return date(year_value, int(month), int(day))
    except ValueError:
        return None


def _to_decimal(raw_number: str) -> Decimal | None:
    try:
        return Decimal(raw_number.replace(",", ""))
    except InvalidOperation:
        return None


def _iter_line_windows(
    lines: list[str],
    *,
    max_window_size: int,
    limit: int | None = None,
) -> list[tuple[int, int, str]]:
    capped_lines = lines[:limit] if limit is not None else lines
    windows: list[tuple[int, int, str]] = []
    for index in range(len(capped_lines)):
        for window_size in range(1, max_window_size + 1):
            if index + window_size > len(capped_lines):
                break
            segment = " ".join(
                line.strip()
                for line in capped_lines[index : index + window_size]
                if line.strip()
            )
            if segment:
                windows.append((index, window_size, segment))
    return windows


def _find_loose_amount_numbers(
    text: str,
    *,
    allow_short_groups: bool,
) -> list[str]:
    patterns = [
        re.compile(r"(?<!\d)(\d{1,2})\s*[.,]\s*(\d{3})(?:\s*[원%Oo!Bb])?(?!\d)"),
        re.compile(r"(?<!\d)(\d{1,2})\s+(\d{3})[8%!]?(?!\d)"),
    ]
    if allow_short_groups:
        patterns.append(
            re.compile(r"(?<!\d)(\d)\s*[.,]\s*(\d{2})(?:\s*[원%Oo!Bb])?(?!\d)")
        )

    numbers: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            numbers.append(f"{match.group(1)}{match.group(2)}")
    return numbers


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _apply_merchant_fixups(candidate: str) -> str:
    fixed = candidate
    for pattern, replacement in MERCHANT_FIXUP_PATTERNS:
        fixed = pattern.sub(replacement, fixed)
    if "현대" in fixed and "오일뱅크" in fixed and not fixed.lstrip().startswith("HD현대"):
        fixed = re.sub(r"^\d*\s*현대", "HD현대", fixed, count=1)
    fixed = re.sub(r"\s+", " ", fixed).strip()
    return fixed


def _select_best_date(attempts: list[OCRAttempt]) -> date | None:
    weighted_candidates: dict[date, float] = {}
    current_year = date.today().year
    for attempt in attempts:
        candidate = attempt.parsed.evidence_date
        if candidate is None:
            continue
        weight = attempt.score
        if candidate.year == current_year:
            weight += 6
        if candidate.year == current_year + 1:
            weight += 2
        weighted_candidates[candidate] = weighted_candidates.get(candidate, 0.0) + weight

    if not weighted_candidates:
        return None
    return max(weighted_candidates.items(), key=lambda item: item[1])[0]


def _select_best_amount(attempts: list[OCRAttempt]) -> Decimal | None:
    weighted_candidates: dict[Decimal, float] = {}
    for attempt in attempts:
        candidate = attempt.parsed.amount
        if candidate is None:
            continue
        weight = attempt.score
        if candidate % Decimal("1000") == 0:
            weight += 12
        elif candidate % Decimal("100") == 0:
            weight += 4
        if Decimal("2000") <= candidate <= Decimal("2100"):
            weight -= 40
        evidence_date = attempt.parsed.evidence_date
        if evidence_date is not None and candidate == Decimal(str(evidence_date.year)):
            weight -= 80
        weighted_candidates[candidate] = weighted_candidates.get(candidate, 0.0) + weight

    if not weighted_candidates:
        return None

    adjusted_candidates = dict(weighted_candidates)
    for candidate, _weight in weighted_candidates.items():
        if candidate % Decimal("1000") != 0:
            continue
        for other_candidate, other_weight in weighted_candidates.items():
            if other_candidate >= candidate:
                continue
            gap_ratio = (candidate - other_candidate) / candidate
            if Decimal("0.07") <= gap_ratio <= Decimal("0.12"):
                adjusted_candidates[candidate] += other_weight * 0.7

    return max(adjusted_candidates.items(), key=lambda item: item[1])[0]


def _select_best_merchant_name(attempts: list[OCRAttempt]) -> str | None:
    clusters: list[dict[str, Any]] = []
    for attempt in attempts:
        candidate = attempt.parsed.merchant_name
        if not candidate:
            continue
        quality = _score_merchant_candidate(candidate, index=0, window_size=1)
        weight = attempt.score + quality
        for cluster in clusters:
            similarity = SequenceMatcher(
                None,
                _normalize_candidate_key(candidate),
                cluster["key"],
            ).ratio()
            if similarity >= 0.82:
                cluster["weight"] += weight
                if quality > cluster["quality"]:
                    cluster["value"] = candidate
                    cluster["quality"] = quality
                    cluster["key"] = _normalize_candidate_key(candidate)
                break
        else:
            clusters.append(
                {
                    "value": candidate,
                    "quality": quality,
                    "weight": weight,
                    "key": _normalize_candidate_key(candidate),
                }
            )

    if not clusters:
        return None
    best_value = max(clusters, key=lambda item: (item["weight"], item["quality"]))["value"]
    best_value = _apply_merchant_fixups(best_value)

    combined_value = _combine_brand_and_merchant(best_value, clusters)
    if combined_value is not None:
        return combined_value
    return best_value


def _select_best_payment_method(
    attempts: list[OCRAttempt],
    evidence_type: EvidenceType,
) -> PaymentMethod | None:
    weighted_candidates: dict[PaymentMethod, float] = {}
    for attempt in attempts:
        candidate = attempt.parsed.payment_method
        if candidate is None:
            continue
        weighted_candidates[candidate] = weighted_candidates.get(candidate, 0.0) + attempt.score

    if weighted_candidates:
        return max(weighted_candidates.items(), key=lambda item: item[1])[0]
    return _extract_payment_method("", evidence_type)


def _normalize_candidate_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(character for character in normalized if character.isalnum())


def _combine_brand_and_merchant(
    merchant_value: str,
    clusters: list[dict[str, Any]],
) -> str | None:
    if "주유" not in merchant_value or "오일뱅크" in merchant_value:
        return None

    for cluster in clusters:
        brand_prefix = _extract_brand_prefix(cluster["value"])
        if brand_prefix is None:
            continue
        return f"{brand_prefix} {merchant_value}"
    return None


def _extract_brand_prefix(candidate: str) -> str | None:
    fixed_candidate = _apply_merchant_fixups(candidate)
    brand_match = re.search(r"(?:HD\s*)?현대[^\s]{0,12}?오일뱅크", fixed_candidate)
    if brand_match is None:
        return None
    brand_prefix = brand_match.group(0)
    if brand_prefix.startswith("현대"):
        brand_prefix = f"HD{brand_prefix}"
    return brand_prefix
