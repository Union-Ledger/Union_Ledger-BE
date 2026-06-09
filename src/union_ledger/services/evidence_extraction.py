from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from union_ledger.core.config import Settings, get_settings
from union_ledger.models.enums import EvidenceType, ExtractionMethod, PaymentMethod
from union_ledger.services.file_storage import SUPPORTED_EVIDENCE_SUFFIXES

# --- PaddleOCR engine cache (process-global) --------------------------------
# Constructing PaddleOCR() reloads models into memory (seconds). The FastAPI
# endpoint builds a new EvidenceExtractionService per request, so without a
# cache every upload paid that cost. Reuse one engine per (lang, mkldnn) for
# the whole process. Inference is serialized with a lock: Paddle predictors are
# not guaranteed thread-safe and OCR runs inside asyncio.to_thread workers.
_ENGINE_LOCK = threading.Lock()
_PREDICT_LOCK = threading.Lock()
_ENGINE_CACHE: dict[tuple[Any, ...], Any] = {}

# Priority used when capping OCR passes (Settings.ocr_max_variants). Each
# preprocessing variant is a full OCR pass, so on CPU 6 variants ≈ 6× latency.
# Best general-purpose preprocessing first, so a cap of 1 keeps the most
# reliable single variant.
_VARIANT_PRIORITY = (
    "document_enhanced",
    "background_cropped",
    "original",
    "adaptive_binary",
    "sharpened",
    "perspective_corrected",
)


def _build_paddle_ocr_engine(settings: Settings) -> Any:
    """Construct a PaddleOCR engine, trying kwarg sets newest-API-first."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise ExtractionConfigurationError(
            "PaddleOCR 의존성이 설치되지 않았습니다. `paddleocr`를 설치해 주세요."
        ) from exc

    os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    mkldnn = bool(settings.ocr_enable_mkldnn)
    candidate_kwargs = [
        {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": mkldnn,
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "korean_PP-OCRv5_mobile_rec",
        },
        {
            "lang": settings.paddleocr_lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": mkldnn,
        },
        {
            "lang": settings.paddleocr_lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        },
        {"lang": settings.paddleocr_lang},
        {},
    ]
    last_error: Exception | None = None
    for engine_kwargs in candidate_kwargs:
        try:
            return PaddleOCR(**engine_kwargs)
        except (TypeError, ValueError) as exc:
            last_error = exc
    raise ExtractionConfigurationError(
        f"PaddleOCR 초기화에 실패했습니다: {last_error}"
    ) from last_error


def _get_cached_paddle_ocr_engine(settings: Settings) -> Any:
    key = (settings.paddleocr_lang, bool(settings.ocr_enable_mkldnn))
    engine = _ENGINE_CACHE.get(key)
    if engine is not None:
        return engine
    with _ENGINE_LOCK:
        engine = _ENGINE_CACHE.get(key)
        if engine is None:
            engine = _build_paddle_ocr_engine(settings)
            _ENGINE_CACHE[key] = engine
        return engine


IMAGE_SUFFIXES = SUPPORTED_EVIDENCE_SUFFIXES - {".pdf"}
PIPELINE_STAGES = (
    "perspective_correction",
    "background_removal",
    "document_enhancement",
    "adaptive_binarization",
    "upscaled_enhancement",
    "sharpening",
)

DATE_KEYWORDS = (
    "거래일시",
    "거래일자",
    "거래일",
    "결제일시",
    "결제일자",
    "승인일시",
    "승인일자",
    "판매일시",
    "판매시간",
    "일시",
    "날짜",
    "date",
)
MERCHANT_LABELS = (
    "가맹점명",
    "가맹점",
    "상호명",
    "상호",
    "점포명",
    "점포",
    "판매처",
    "매장명",
    "merchant",
    "store",
)
MERCHANT_NOISE_KEYWORDS = (
    "영수증",
    "조회",
    "발행",
    "고객용",
    "사업자",
    "전화",
    "주소",
    "대표자",
    "부가세",
    "공급가액",
    "면세",
    "과세",
    "합계",
    "총계",
    "총액",
    "포스",
    "pos",
)
ADDRESS_HINTS = (
    "서울",
    "경기",
    "인천",
    "부산",
    "대구",
    "대전",
    "광주",
    "울산",
    "세종",
    "충북",
    "충남",
    "전북",
    "전남",
    "경북",
    "경남",
    "강원",
    "제주",
    "광진구",
    "송파구",
    "강남구",
    "자양로",
    "능동로",
    "로 ",
    "길 ",
    "층",
)
TOTAL_HINTS = (
    "결제금액",
    "승인금액",
    "합계",
    "총계",
    "총액",
    "청구금액",
    "받을금액",
    "판매합계",
    "이체금액",
    "출금금액",
    "입금금액",
    "amount",
    "total",
    "paid",
)
AMOUNT_REJECTION_KEYWORDS = (
    "사업자",
    "전화",
    "tel",
    "승인번호",
    "거래번호",
    "카드번호",
    "주문번호",
    "매출전표",
    "단말기",
    "가맹점번호",
)
ONLINE_PAYMENT_KEYWORDS = (
    "네이버페이",
    "naver pay",
    "naverpay",
    "카카오페이",
    "kakao pay",
    "kakaopay",
    "토스페이",
    "toss pay",
    "tosspay",
)
CASH_KEYWORDS = ("현금", "cash", "거스름돈", "받을금액", "현금영수증")
CARD_KEYWORDS = ("카드", "신용", "체크", "승인번호", "승인", "visa", "master")
BANK_TRANSFER_KEYWORDS = ("이체", "출금", "입금", "계좌", "송금", "받는분", "은행")

DATE_PATTERNS = (
    re.compile(
        r"(?P<year>\d{4})\s*[-./년]\s*(?P<month>\d{1,2})\s*[-./월]\s*(?P<day>\d{1,2})"
    ),
    re.compile(r"(?P<year>\d{2})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"),
    re.compile(
        r"(?P<year>\d{4})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
    ),
    re.compile(r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)"),
)
AMOUNT_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{3,9})(?:\.\d{1,2})?(?!\d)")
BUSINESS_NUMBER_PATTERN = re.compile(r"\d{2,3}\s*-\s*\d{2}\s*-\s*\d{4,5}")
PHONE_PATTERN = re.compile(r"(?:\d{2,4}-\d{3,4}-\d{4})|(?:1[568]\d{2}-\d{4})")


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
class OCRLine:
    text: str
    confidence: float
    box: list[list[float]]
    y: float
    x: float


@dataclass(slots=True)
class OCRAttempt:
    preprocessing: str
    raw_text: str
    parsed: ParsedEvidenceFields
    score: float
    average_confidence: float = 0.0
    line_count: int = 0
    lines: tuple[OCRLine, ...] = ()


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


@dataclass(slots=True)
class _FieldChoice:
    value: Any
    score: float
    attempt: OCRAttempt


@dataclass(slots=True)
class _AmountCandidate:
    value: Decimal
    score: float
    line_index: int
    line: str
    has_total_hint: bool = False


def normalize_extracted_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    cleaned_lines: list[str] = []
    for raw_line in normalized.splitlines():
        compact = re.sub(r"\s+", " ", raw_line).strip(" \t|")
        compact = compact.replace("₩", "원")
        if compact:
            cleaned_lines.append(compact)
    return "\n".join(cleaned_lines)


def parse_extracted_text(
    text: str,
    evidence_type: EvidenceType,
    *,
    ocr_lines: list[OCRLine] | None = None,
) -> ParsedEvidenceFields:
    normalized_text = normalize_extracted_text(text)
    lines = _build_receipt_lines(normalized_text, ocr_lines)

    evidence_date = _extract_date_from_layout(lines)
    merchant_name = _extract_merchant_from_layout(lines, evidence_type)
    payment_method = _extract_payment_method_from_layout(lines, evidence_type)
    amount = _extract_amount_from_layout(lines, evidence_type, payment_method=payment_method)

    confidence = 0.0
    confidence += 0.25 if evidence_date else 0.0
    confidence += 0.3 if merchant_name else 0.0
    confidence += 0.3 if amount is not None else 0.0
    confidence += 0.15 if payment_method else 0.0

    return ParsedEvidenceFields(
        evidence_date=evidence_date,
        merchant_name=merchant_name,
        amount=amount,
        payment_method=payment_method,
        confidence=round(min(confidence, 1.0), 4),
    )


def build_ocr_attempt(
    raw_text: str,
    evidence_type: EvidenceType,
    *,
    preprocessing: str,
    average_confidence: float = 0.0,
    line_count: int = 0,
    lines: list[OCRLine] | None = None,
) -> OCRAttempt:
    normalized_text = normalize_extracted_text(raw_text)
    parsed = parse_extracted_text(normalized_text, evidence_type, ocr_lines=lines)
    score = _score_ocr_attempt(
        normalized_text,
        parsed,
        evidence_type,
        preprocessing=preprocessing,
        average_confidence=average_confidence,
        line_count=line_count,
    )
    return OCRAttempt(
        preprocessing=preprocessing,
        raw_text=normalized_text,
        parsed=parsed,
        score=score,
        average_confidence=average_confidence,
        line_count=line_count,
        lines=tuple(lines or ()),
    )


def select_best_ocr_attempt(attempts: list[OCRAttempt]) -> OCRAttempt:
    if not attempts:
        raise ExtractionError("선택할 OCR 시도가 없습니다.")
    return max(attempts, key=lambda attempt: (attempt.score, attempt.average_confidence))


def merge_ocr_attempt_fields(
    attempts: list[OCRAttempt],
    evidence_type: EvidenceType,
) -> ParsedEvidenceFields:
    del evidence_type
    if not attempts:
        raise ExtractionError("병합할 OCR 시도가 없습니다.")

    date_choice = _select_date_with_consensus(attempts)
    merchant_choice = _select_merchant_with_consensus(attempts)
    amount_choice = _select_amount_with_consensus(attempts)
    payment_choice = _select_field_choice(
        attempts,
        lambda attempt: attempt.parsed.payment_method,
        lambda attempt: _field_score(attempt, "payment"),
    )

    confidence_components = [
        choice.score for choice in (date_choice, merchant_choice, amount_choice, payment_choice)
        if choice is not None
    ]
    confidence = (
        sum(confidence_components) / len(confidence_components)
        if confidence_components
        else 0
    )

    return ParsedEvidenceFields(
        evidence_date=date_choice.value if date_choice else None,
        merchant_name=merchant_choice.value if merchant_choice else None,
        amount=amount_choice.value if amount_choice else None,
        payment_method=payment_choice.value if payment_choice else None,
        confidence=round(min(confidence / 100, 1.0), 4),
    )


# --- CLOVA Receipt OCR (Naver Cloud) ---------------------------------------
# Korean-specialized receipt OCR that returns structured fields directly, so we
# skip the local preprocessing + regex pipeline entirely. Enabled via
# Settings.ocr_provider == "clova". Pure stdlib HTTP — no extra dependency.


def _clova_image_format(file_path: Path) -> str:
    suffix = file_path.suffix.lower().lstrip(".")
    return {"jpeg": "jpg", "tif": "tiff"}.get(suffix, suffix)


def _call_clova_receipt_ocr(
    *,
    invoke_url: str,
    secret_key: str,
    image_bytes: bytes,
    image_name: str,
    image_format: str,
    timeout: int,
) -> dict[str, Any]:
    body = {
        "version": "V2",
        "requestId": str(uuid.uuid4()),
        "timestamp": int(time.time() * 1000),
        "images": [
            {
                "format": image_format,
                "name": image_name,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        ],
    }
    request = urllib.request.Request(
        invoke_url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-OCR-SECRET": secret_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise ExtractionError(f"CLOVA OCR 호출 실패 (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtractionError(f"CLOVA OCR 연결 실패: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"CLOVA OCR 응답 파싱 실패: {exc}") from exc


def _clova_field_text(field: object) -> str | None:
    """Best string from a CLOVA field object ({text, formatted, ...})."""
    if not isinstance(field, dict):
        return None
    formatted = field.get("formatted")
    if isinstance(formatted, dict) and formatted.get("value") not in (None, ""):
        return str(formatted["value"]).strip()
    text = field.get("text")
    return str(text).strip() if text not in (None, "") else None


def _clova_date(field: object) -> date | None:
    if not isinstance(field, dict):
        return None
    formatted = field.get("formatted")
    if isinstance(formatted, dict):
        try:
            return date(
                int(formatted["year"]), int(formatted["month"]), int(formatted["day"])
            )
        except (KeyError, TypeError, ValueError):
            pass
    text = field.get("text")
    if text:
        match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", str(text))
        if match:
            try:
                return date(int(match[1]), int(match[2]), int(match[3]))
            except ValueError:
                return None
    return None


def _clova_amount(field: object) -> Decimal | None:
    candidate = _clova_field_text(field)
    if candidate is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", candidate.replace(",", ""))
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_clova_receipt_response(
    payload: dict[str, Any],
) -> tuple[ParsedEvidenceFields, str]:
    """Map a CLOVA Receipt OCR response to ParsedEvidenceFields + raw-text summary."""
    images = payload.get("images") or []
    if not images:
        raise ExtractionError("CLOVA OCR 응답에 이미지 결과가 없습니다.")
    image = images[0]
    infer = image.get("inferResult")
    if infer and infer != "SUCCESS":
        raise ExtractionError(f"CLOVA OCR 인식 실패: {image.get('message') or infer}")

    result = (image.get("receipt") or {}).get("result") or {}
    store = result.get("storeInfo") or {}
    payment = result.get("paymentInfo") or {}
    total = result.get("totalPrice") or {}

    merchant_name = _clova_field_text(store.get("name"))
    evidence_date = _clova_date(payment.get("date"))
    amount = _clova_amount(total.get("price"))

    scores = [
        float(field["confidenceScore"])
        for field in (store.get("name"), payment.get("date"), total.get("price"))
        if isinstance(field, dict)
        and isinstance(field.get("confidenceScore"), (int, float))
    ]
    confidence = round(sum(scores) / len(scores), 4) if scores else 0.0

    raw_text = "\n".join(
        part
        for part in (
            merchant_name,
            evidence_date.isoformat() if evidence_date else None,
            f"합계 {amount}" if amount is not None else None,
        )
        if part
    )
    parsed = ParsedEvidenceFields(
        evidence_date=evidence_date,
        merchant_name=merchant_name,
        amount=amount,
        payment_method=None,
        confidence=confidence,
    )
    return parsed, raw_text


class EvidenceExtractionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._paddle_ocr_engine: Any | None = None

    def extract_upload(
        self,
        filename: str,
        content: bytes,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EVIDENCE_SUFFIXES:
            raise ExtractionError(f"지원하지 않는 파일 형식입니다: {suffix}")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / filename
            temp_path.write_bytes(content)
            return self.extract_file(temp_path, evidence_type)

    def extract_file(self, file_path: Path, evidence_type: EvidenceType) -> ExtractionResult:
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf_result(file_path, evidence_type)
        if suffix not in IMAGE_SUFFIXES:
            raise ExtractionError(f"지원하지 않는 파일 형식입니다: {suffix}")
        if self._settings.ocr_provider == "clova":
            try:
                return self._extract_image_clova(file_path, evidence_type)
            except (ExtractionError, ExtractionConfigurationError):
                if not self._settings.clova_ocr_fallback_to_local:
                    raise
                # CLOVA unconfigured/unavailable → fall back to local PaddleOCR.
        return self._extract_image_result(file_path, evidence_type)

    def _extract_pdf_result(self, file_path: Path, evidence_type: EvidenceType) -> ExtractionResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ExtractionConfigurationError(
                "PDF 텍스트 추출 의존성이 설치되지 않았습니다. `pypdf`를 설치해 주세요."
            ) from exc

        try:
            reader = PdfReader(str(file_path))
            raw_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise ExtractionError(f"PDF 텍스트 추출에 실패했습니다: {exc}") from exc

        parsed = parse_extracted_text(raw_text, evidence_type)
        payload = {
            "engine": "pypdf",
            "raw_text": normalize_extracted_text(raw_text),
            "confidence": parsed.confidence,
            "review_required": True,
            "normalized_fields": _serialize_parsed_fields(parsed),
            "ocr_pipeline": {
                "version": "v4",
                "engine": "pypdf",
                "stages": ["pdf_text_extraction"],
                "selected_variants": [],
            },
        }
        return ExtractionResult(
            source_file_name=file_path.name,
            evidence_type=evidence_type,
            method=ExtractionMethod.PDF_TEXT,
            raw_text=payload["raw_text"],
            evidence_date=parsed.evidence_date,
            merchant_name=parsed.merchant_name,
            amount=parsed.amount,
            payment_method=parsed.payment_method,
            payload=payload,
        )

    def _extract_image_clova(
        self,
        file_path: Path,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        invoke_url = self._settings.clova_ocr_invoke_url
        secret_key = self._settings.clova_ocr_secret_key
        if not invoke_url or not secret_key:
            raise ExtractionConfigurationError(
                "CLOVA OCR가 설정되지 않았습니다 "
                "(CLOVA_OCR_INVOKE_URL / CLOVA_OCR_SECRET_KEY)."
            )
        response = _call_clova_receipt_ocr(
            invoke_url=invoke_url,
            secret_key=secret_key,
            image_bytes=file_path.read_bytes(),
            image_name=file_path.name,
            image_format=_clova_image_format(file_path),
            timeout=self._settings.clova_ocr_timeout_seconds,
        )
        parsed, raw_text = parse_clova_receipt_response(response)
        payload = {
            "engine": "clova-receipt",
            "raw_text": raw_text,
            "confidence": parsed.confidence,
            "review_required": True,
            "normalized_fields": _serialize_parsed_fields(parsed),
            "ocr_pipeline": {
                "version": "v4",
                "engine": "clova",
                "stages": ["clova_receipt_ocr"],
                "selected_variants": [],
            },
        }
        return ExtractionResult(
            source_file_name=file_path.name,
            evidence_type=evidence_type,
            method=ExtractionMethod.OCR,
            raw_text=raw_text,
            evidence_date=parsed.evidence_date,
            merchant_name=parsed.merchant_name,
            amount=parsed.amount,
            payment_method=parsed.payment_method,
            payload=payload,
        )

    def _extract_image_result(
        self,
        file_path: Path,
        evidence_type: EvidenceType,
    ) -> ExtractionResult:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            variants = self._prepare_image_variants(file_path, temp_root, evidence_type)
            attempts = self._collect_image_ocr_attempts(variants, evidence_type)

        best_attempt = select_best_ocr_attempt(attempts)
        merged_fields = merge_ocr_attempt_fields(attempts, evidence_type)
        field_fusion = self._build_field_fusion_payload(attempts)

        payload = {
            "engine": "paddleocr",
            "raw_text": best_attempt.raw_text,
            "confidence": merged_fields.confidence,
            "review_required": True,
            "normalized_fields": _serialize_parsed_fields(merged_fields),
            "selected_attempt": self._serialize_ocr_attempt(best_attempt),
            "field_fusion": field_fusion,
            "attempts": [self._serialize_ocr_attempt(attempt) for attempt in attempts],
            "ocr_pipeline": {
                "version": "v4",
                "engine": self._settings.ocr_engine,
                "stages": list(PIPELINE_STAGES),
                "selected_variants": [attempt.preprocessing for attempt in attempts],
            },
        }

        return ExtractionResult(
            source_file_name=file_path.name,
            evidence_type=evidence_type,
            method=ExtractionMethod.OCR,
            raw_text=best_attempt.raw_text,
            evidence_date=merged_fields.evidence_date,
            merchant_name=merged_fields.merchant_name,
            amount=merged_fields.amount,
            payment_method=merged_fields.payment_method,
            payload=payload,
        )

    def _limit_variants(
        self, variants: list[PreparedImageVariant]
    ) -> list[PreparedImageVariant]:
        """Cap OCR passes for speed (Settings.ocr_max_variants; 0 = no cap)."""
        cap = self._settings.ocr_max_variants
        if cap <= 0 or len(variants) <= cap:
            return variants
        rank = {label: i for i, label in enumerate(_VARIANT_PRIORITY)}
        ordered = sorted(
            variants, key=lambda v: rank.get(v.label, len(_VARIANT_PRIORITY))
        )
        return ordered[:cap]

    def _collect_image_ocr_attempts(
        self,
        variants: list[PreparedImageVariant],
        evidence_type: EvidenceType,
    ) -> list[OCRAttempt]:
        if self._settings.ocr_mode == "modal":
            return self._collect_modal_ocr_attempts_parallel(variants, evidence_type)

        attempts: list[OCRAttempt] = []
        for variant in self._limit_variants(variants):
            raw_text, average_confidence, line_count, lines = self._run_paddle_ocr(variant.path)
            attempt = build_ocr_attempt(
                raw_text,
                evidence_type,
                preprocessing=variant.label,
                average_confidence=average_confidence,
                line_count=line_count,
                lines=lines,
            )
            if attempt.raw_text:
                attempts.append(attempt)

        if not attempts:
            raise ExtractionError("이미지에서 OCR 텍스트를 추출하지 못했습니다.")
        return sorted(attempts, key=lambda attempt: attempt.score, reverse=True)

    def _collect_modal_ocr_attempts_parallel(
        self,
        variants: list[PreparedImageVariant],
        evidence_type: EvidenceType,
    ) -> list[OCRAttempt]:
        try:
            import modal
        except ImportError as exc:
            raise ExtractionConfigurationError(
                "Modal 의존성이 설치되지 않았습니다."
            ) from exc

        worker_cls = modal.Cls.from_name("union-ledger-ocr", "OCRWorker")
        worker = worker_cls()

        results = [worker.run_ocr.remote(v.path.read_bytes()) for v in variants]

        attempts: list[OCRAttempt] = []
        for variant, result in zip(variants, results):
            lines = self._parse_modal_lines(result)
            filtered = [l for l in lines if l.confidence >= self._settings.ocr_min_line_confidence]
            raw_text = "\n".join(l.text for l in filtered)
            avg_conf = (
                sum(l.confidence for l in filtered) / len(filtered) if filtered else 0.0
            )
            attempt = build_ocr_attempt(
                normalize_extracted_text(raw_text),
                evidence_type,
                preprocessing=variant.label,
                average_confidence=round(avg_conf, 4),
                line_count=len(filtered),
                lines=filtered,
            )
            if attempt.raw_text:
                attempts.append(attempt)

        if not attempts:
            raise ExtractionError("이미지에서 OCR 텍스트를 추출하지 못했습니다.")
        return sorted(attempts, key=lambda a: a.score, reverse=True)

    def _parse_modal_lines(self, result: dict[str, Any]) -> list[OCRLine]:
        lines: list[OCRLine] = []
        for ld in result.get("lines", []):
            text = normalize_extracted_text(str(ld.get("text", "")))
            if not text:
                continue
            conf = float(ld.get("confidence", 0.0))
            box = ld.get("box", [])
            y = min(p[1] for p in box) if box else 0.0
            x = min(p[0] for p in box) if box else 0.0
            lines.append(OCRLine(text=text, confidence=conf, box=box, y=y, x=x))
        lines.sort(key=lambda l: (l.y, l.x))
        return lines

    def _prepare_image_variants(
        self,
        file_path: Path,
        temp_root: Path,
        evidence_type: EvidenceType,
    ) -> list[PreparedImageVariant]:
        cv2, np, threshold_sauvola = self._load_ocr_dependencies()
        image = self._load_cv_image(file_path, cv2)
        variants: list[PreparedImageVariant] = []

        def add_variant(label: str, variant_image: Any) -> Any:
            target_path = temp_root / f"{label}.png"
            cv2.imwrite(str(target_path), variant_image)
            variants.append(PreparedImageVariant(label=label, path=target_path))
            return variant_image

        base = add_variant("original", image)
        if evidence_type == EvidenceType.PHYSICAL_RECEIPT:
            perspective = add_variant(
                "perspective_corrected",
                self._perspective_correct(base, cv2),
            )
            background_removed = add_variant(
                "background_cropped",
                self._remove_background(perspective, cv2),
            )
        else:
            background_removed = base

        enhanced = add_variant(
            "document_enhanced",
            self._enhance_document(background_removed, cv2, threshold_sauvola),
        )
        add_variant("adaptive_binary", self._adaptive_binarize(enhanced, cv2))

        sharpened = self._sharpen_for_ocr(background_removed, cv2)
        add_variant("sharpened", sharpened)

        return variants

    def _upscale_for_ocr(self, image: Any, cv2: Any) -> Any:
        height, width = image.shape[:2]
        if max(height, width) >= 2400:
            return None
        scale = min(1.5, 3200 / max(height, width))
        if scale <= 1.05:
            return None
        return cv2.resize(
            image,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_CUBIC,
        )

    def _sharpen_for_ocr(self, image: Any, cv2: Any) -> Any:
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(4, 4))
        contrasted = clahe.apply(gray)
        sharpened = cv2.addWeighted(
            contrasted, 2.0,
            cv2.GaussianBlur(contrasted, (0, 0), 3), -1.0,
            0,
        )
        sharpened = np.clip(sharpened, 0, 255).astype("uint8")
        return cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR)

    def _load_ocr_dependencies(self) -> tuple[Any, Any, Any]:
        try:
            import cv2
            import numpy as np
            from skimage.filters import threshold_sauvola
        except ImportError as exc:
            raise ExtractionConfigurationError(
                "OCR 전처리 의존성이 설치되지 않았습니다. "
                "`opencv-python-headless`, `numpy`, `scikit-image`를 설치해 주세요."
            ) from exc
        return cv2, np, threshold_sauvola

    def _get_paddle_ocr_engine(self) -> Any:
        # Reuse the process-global engine so models load once, not per request.
        if self._paddle_ocr_engine is None:
            self._paddle_ocr_engine = _get_cached_paddle_ocr_engine(self._settings)
        return self._paddle_ocr_engine

    def _run_paddle_ocr(self, file_path: Path) -> tuple[str, float, int, list[OCRLine]]:
        if self._settings.ocr_mode == "modal":
            return self._run_modal_ocr(file_path)
        return self._run_local_paddle_ocr(file_path)

    def _run_modal_ocr(self, file_path: Path) -> tuple[str, float, int, list[OCRLine]]:
        try:
            import modal
        except ImportError as exc:
            raise ExtractionConfigurationError(
                "Modal 의존성이 설치되지 않았습니다. `modal`을 설치해 주세요."
            ) from exc

        image_bytes = file_path.read_bytes()
        try:
            worker_cls = modal.Cls.from_name("union-ledger-ocr", "OCRWorker")
            result = worker_cls().run_ocr.remote(image_bytes)
        except Exception as exc:
            raise ExtractionError(f"Modal OCR Worker 호출에 실패했습니다: {exc}") from exc

        lines: list[OCRLine] = []
        for line_data in result.get("lines", []):
            text = normalize_extracted_text(str(line_data.get("text", "")))
            if not text:
                continue
            confidence = float(line_data.get("confidence", 0.0))
            box = line_data.get("box", [])
            y = min(point[1] for point in box) if box else 0.0
            x = min(point[0] for point in box) if box else 0.0
            lines.append(OCRLine(text=text, confidence=confidence, box=box, y=y, x=x))

        lines.sort(key=lambda line: (line.y, line.x))
        filtered_lines = [
            line for line in lines if line.confidence >= self._settings.ocr_min_line_confidence
        ]
        raw_text = "\n".join(line.text for line in filtered_lines)
        average_confidence = (
            sum(line.confidence for line in filtered_lines) / len(filtered_lines)
            if filtered_lines
            else 0.0
        )
        return (
            normalize_extracted_text(raw_text),
            round(average_confidence, 4),
            len(filtered_lines),
            filtered_lines,
        )

    def _run_local_paddle_ocr(self, file_path: Path) -> tuple[str, float, int, list[OCRLine]]:
        engine = self._get_paddle_ocr_engine()
        try:
            # Serialize inference: the engine is shared across to_thread workers
            # and Paddle predictors are not guaranteed thread-safe.
            with _PREDICT_LOCK:
                if hasattr(engine, "predict"):
                    raw_result = list(engine.predict(str(file_path)))
                else:
                    raw_result = engine.ocr(str(file_path))
        except Exception as exc:
            raise ExtractionError(f"PaddleOCR 실행에 실패했습니다: {exc}") from exc

        lines = self._parse_paddle_output(raw_result)
        filtered_lines = [
            line for line in lines if line.confidence >= self._settings.ocr_min_line_confidence
        ]
        raw_text = "\n".join(line.text for line in filtered_lines)
        average_confidence = (
            sum(line.confidence for line in filtered_lines) / len(filtered_lines)
            if filtered_lines
            else 0.0
        )
        return (
            normalize_extracted_text(raw_text),
            round(average_confidence, 4),
            len(filtered_lines),
            filtered_lines,
        )

    def _parse_paddle_output(self, raw_result: Any) -> list[OCRLine]:
        lines: list[OCRLine] = []
        for item in self._iter_paddle_items(raw_result):
            if isinstance(item, dict) and "rec_texts" in item:
                texts = item.get("rec_texts") or []
                scores = item.get("rec_scores") or []
                boxes = item.get("rec_polys") or item.get("dt_polys") or []
                for index, text in enumerate(texts):
                    confidence = float(scores[index]) if index < len(scores) else 0.0
                    box = self._normalize_box(boxes[index]) if index < len(boxes) else []
                    y = min(point[1] for point in box) if box else 0.0
                    x = min(point[0] for point in box) if box else 0.0
                    lines.append(
                        OCRLine(
                            text=normalize_extracted_text(str(text)),
                            confidence=confidence,
                            box=box,
                            y=y,
                            x=x,
                        )
                    )
                continue
            box, text, confidence = self._unpack_paddle_item(item)
            if not text:
                continue
            y = min(point[1] for point in box) if box else 0.0
            x = min(point[0] for point in box) if box else 0.0
            lines.append(
                OCRLine(
                    text=normalize_extracted_text(text),
                    confidence=confidence,
                    box=box,
                    y=y,
                    x=x,
                )
            )
        return sorted(lines, key=lambda line: (line.y, line.x))

    def _iter_paddle_items(self, raw_result: Any) -> list[Any]:
        if raw_result is None:
            return []
        if isinstance(raw_result, list):
            items: list[Any] = []
            for page in raw_result:
                if isinstance(page, list):
                    items.extend(page)
                elif page:
                    items.append(page)
            return items
        return [raw_result]

    def _unpack_paddle_item(self, item: Any) -> tuple[list[list[float]], str, float]:
        if isinstance(item, dict):
            box = item.get("bbox") or item.get("box") or []
            text = str(item.get("text") or "").strip()
            confidence = float(item.get("score") or item.get("confidence") or 0.0)
            return self._normalize_box(box), text, confidence

        if isinstance(item, (list, tuple)) and len(item) >= 2:
            box = self._normalize_box(item[0])
            content = item[1]
            if isinstance(content, (list, tuple)) and len(content) >= 2:
                return box, str(content[0]).strip(), float(content[1])
            if isinstance(content, str):
                confidence = float(item[2]) if len(item) > 2 else 0.0
                return box, content.strip(), confidence

        return [], str(item).strip(), 0.0

    def _normalize_box(self, raw_box: Any) -> list[list[float]]:
        if not isinstance(raw_box, (list, tuple)):
            return []
        box: list[list[float]] = []
        for point in raw_box:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                box.append([float(point[0]), float(point[1])])
        return box

    def _load_cv_image(self, file_path: Path, cv2: Any) -> Any:
        import numpy as np

        image_bytes = file_path.read_bytes()
        matrix = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if matrix is None:
            raise ExtractionError("이미지 파일을 열 수 없습니다.")
        return self._resize_cv_image(matrix, cv2)

    def _resize_cv_image(self, image: Any, cv2: Any, max_dimension: int = 2600) -> Any:
        height, width = image.shape[:2]
        longest_edge = max(height, width)
        if longest_edge <= max_dimension:
            return image
        scale = max_dimension / float(longest_edge)
        return cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    def _perspective_correct(self, image: Any, cv2: Any) -> Any:
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edged = cv2.Canny(blurred, 50, 150)
        contours, _ = cv2.findContours(edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        document_contour = None
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
            perimeter = cv2.arcLength(contour, True)
            approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if len(approximation) == 4:
                document_contour = approximation.reshape(4, 2)
                break

        if document_contour is None:
            return self._deskew(image, cv2)

        ordered = _order_points(document_contour)
        top_left, top_right, bottom_right, bottom_left = ordered
        width_top = _distance(top_left, top_right)
        width_bottom = _distance(bottom_left, bottom_right)
        height_left = _distance(top_left, bottom_left)
        height_right = _distance(top_right, bottom_right)
        target_width = int(max(width_top, width_bottom))
        target_height = int(max(height_left, height_right))
        if target_width < 50 or target_height < 50:
            return self._deskew(image, cv2)

        destination = [
            [0.0, 0.0],
            [float(target_width - 1), 0.0],
            [float(target_width - 1), float(target_height - 1)],
            [0.0, float(target_height - 1)],
        ]
        matrix = cv2.getPerspectiveTransform(
            np.float32(ordered),
            np.float32(destination),
        )
        warped = cv2.warpPerspective(image, matrix, (target_width, target_height))
        return self._deskew(warped, cv2)

    def _deskew(self, image: Any, cv2: Any) -> Any:
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        lines = cv2.HoughLinesP(binary, 1, np.pi / 180, 100, minLineLength=80, maxLineGap=10)
        if lines is None:
            return image

        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = x2 - x1
            dy = y2 - y1
            if abs(dx) < 5:
                continue
            angle = np.degrees(np.arctan2(dy, dx))
            if abs(angle) < 15:
                angles.append(angle)

        if not angles:
            return image

        median_angle = float(np.median(angles))
        if abs(median_angle) < 0.3:
            return image

        height, width = image.shape[:2]
        center = (width / 2, height / 2)
        rotation_matrix = cv2.getRotationMatrix2D(center, median_angle, 1.0)
        return cv2.warpAffine(
            image, rotation_matrix, (width, height),
            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
        )

    def _remove_background(self, image: Any, cv2: Any) -> Any:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresholded = cv2.threshold(
            blurred,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        closed = cv2.morphologyEx(thresholded, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return image

        contour = max(contours, key=cv2.contourArea)
        x, y, width, height = cv2.boundingRect(contour)
        if width < image.shape[1] * 0.35 or height < image.shape[0] * 0.35:
            return image

        padding = 12
        x0 = max(x - padding, 0)
        y0 = max(y - padding, 0)
        x1 = min(x + width + padding, image.shape[1])
        y1 = min(y + height + padding, image.shape[0])
        return image[y0:y1, x0:x1]

    def _enhance_document(self, image: Any, cv2: Any, threshold_sauvola: Any) -> Any:
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        clahe = cv2.createCLAHE(clipLimit=3.2, tileGridSize=(6, 6))
        contrasted = clahe.apply(gray)

        denoised = cv2.bilateralFilter(contrasted, 9, 40, 40)

        sharpened = cv2.addWeighted(
            denoised, 1.5,
            cv2.GaussianBlur(denoised, (0, 0), 3), -0.5,
            0,
        )

        sauvola_threshold = threshold_sauvola(sharpened, window_size=25, k=0.15)
        mask = sharpened > sauvola_threshold
        enhanced = sharpened.copy()
        enhanced[mask] = 255
        enhanced[~mask] = np.clip(enhanced[~mask] * 0.7, 0, 255).astype("uint8")
        return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)

    def _adaptive_binarize(self, image: Any, cv2: Any) -> Any:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        sharpened = cv2.addWeighted(
            gray, 1.4,
            cv2.GaussianBlur(gray, (0, 0), 2), -0.4,
            0,
        )
        adaptive = cv2.adaptiveThreshold(
            sharpened,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            25,
            11,
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        cleaned = cv2.morphologyEx(adaptive, cv2.MORPH_OPEN, kernel, iterations=1)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)
        return cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)

    def _build_field_fusion_payload(self, attempts: list[OCRAttempt]) -> dict[str, Any]:
        payload: dict[str, Any] = {"normalized_fields": {}, "sources": {}}
        for field_name in ("date", "merchant", "amount", "payment"):
            choice = _select_field_choice(
                attempts,
                lambda attempt, name=field_name: _field_value(attempt, name),
                lambda attempt, name=field_name: _field_score(attempt, name),
            )
            if choice is None:
                payload["normalized_fields"][field_name] = None
                continue
            payload["normalized_fields"][field_name] = _serialize_field_value(
                field_name,
                choice.value,
            )
            payload["sources"][field_name] = {
                "preprocessing": choice.attempt.preprocessing,
                "score": round(choice.score, 4),
                "average_confidence": round(choice.attempt.average_confidence, 4),
            }
        return payload

    def _serialize_ocr_attempt(self, attempt: OCRAttempt) -> dict[str, Any]:
        return {
            "preprocessing": attempt.preprocessing,
            "score": round(attempt.score, 4),
            "average_confidence": round(attempt.average_confidence, 4),
            "line_count": attempt.line_count,
            "normalized_fields": _serialize_parsed_fields(attempt.parsed),
        }


def _select_field_choice(
    attempts: list[OCRAttempt],
    value_getter: Any,
    score_getter: Any,
) -> _FieldChoice | None:
    best_choice: _FieldChoice | None = None
    for attempt in attempts:
        value = value_getter(attempt)
        if value in (None, ""):
            continue
        score = score_getter(attempt)
        if best_choice is None or score > best_choice.score:
            best_choice = _FieldChoice(value=value, score=score, attempt=attempt)
    return best_choice


def _select_amount_with_consensus(attempts: list[OCRAttempt]) -> _FieldChoice | None:
    from collections import Counter as _Counter

    amount_votes: _Counter[str] = _Counter()
    amount_attempts: dict[str, list[OCRAttempt]] = {}
    for attempt in attempts:
        if attempt.parsed.amount is not None:
            key = str(attempt.parsed.amount)
            amount_votes[key] += 1
            amount_attempts.setdefault(key, []).append(attempt)

    if not amount_votes:
        return None

    most_common_value, most_common_count = amount_votes.most_common(1)[0]
    if most_common_count >= 2:
        best_attempt = max(
            amount_attempts[most_common_value],
            key=lambda a: _field_score(a, "amount"),
        )
        return _FieldChoice(
            value=best_attempt.parsed.amount,
            score=_field_score(best_attempt, "amount"),
            attempt=best_attempt,
        )

    return _select_field_choice(
        attempts,
        lambda attempt: attempt.parsed.amount,
        lambda attempt: _field_score(attempt, "amount"),
    )


def _select_date_with_consensus(attempts: list[OCRAttempt]) -> _FieldChoice | None:
    from collections import Counter as _Counter

    date_votes: _Counter[str] = _Counter()
    date_attempts: dict[str, list[OCRAttempt]] = {}
    for attempt in attempts:
        if attempt.parsed.evidence_date is not None:
            key = attempt.parsed.evidence_date.isoformat()
            date_votes[key] += 1
            date_attempts.setdefault(key, []).append(attempt)

    if not date_votes:
        return None

    most_common_value, most_common_count = date_votes.most_common(1)[0]
    if most_common_count >= 2:
        best_attempt = max(
            date_attempts[most_common_value],
            key=lambda a: _field_score(a, "date"),
        )
        return _FieldChoice(
            value=best_attempt.parsed.evidence_date,
            score=_field_score(best_attempt, "date"),
            attempt=best_attempt,
        )

    return _select_field_choice(
        attempts,
        lambda attempt: attempt.parsed.evidence_date,
        lambda attempt: _field_score(attempt, "date"),
    )


def _select_merchant_with_consensus(attempts: list[OCRAttempt]) -> _FieldChoice | None:
    import re as _re
    from collections import Counter as _Counter

    merchant_votes: _Counter[str] = _Counter()
    merchant_attempts: dict[str, list[OCRAttempt]] = {}
    for attempt in attempts:
        if attempt.parsed.merchant_name:
            key = _re.sub(r"[\s·\-_.,()（）]", "", attempt.parsed.merchant_name).lower()
            merchant_votes[key] += 1
            merchant_attempts.setdefault(key, []).append(attempt)

    if not merchant_votes:
        return None

    most_common_value, most_common_count = merchant_votes.most_common(1)[0]
    if most_common_count >= 2:
        best_attempt = max(
            merchant_attempts[most_common_value],
            key=lambda a: _field_score(a, "merchant"),
        )
        return _FieldChoice(
            value=best_attempt.parsed.merchant_name,
            score=_field_score(best_attempt, "merchant"),
            attempt=best_attempt,
        )

    return _select_field_choice(
        attempts,
        lambda attempt: attempt.parsed.merchant_name,
        lambda attempt: _field_score(attempt, "merchant"),
    )


def _field_value(attempt: OCRAttempt, field_name: str) -> Any:
    match field_name:
        case "date":
            return attempt.parsed.evidence_date
        case "merchant":
            return attempt.parsed.merchant_name
        case "amount":
            return attempt.parsed.amount
        case "payment":
            return attempt.parsed.payment_method
        case _:
            return None


def _field_score(attempt: OCRAttempt, field_name: str) -> float:
    base = attempt.score + (attempt.average_confidence * 10)
    if field_name == "merchant":
        bonus = 0
        if attempt.preprocessing in {"perspective_corrected", "background_cropped"}:
            bonus = 6
        elif attempt.preprocessing == "upscaled_enhanced":
            bonus = 5
        elif attempt.preprocessing == "sharpened":
            bonus = 4
        return base + bonus
    if field_name == "amount":
        bonus = 0
        if attempt.preprocessing == "adaptive_binary":
            bonus = 2
        elif attempt.preprocessing in {"upscaled_enhanced", "sharpened"}:
            bonus = 1
        return base + bonus
    if field_name == "date":
        bonus = 0
        if attempt.preprocessing == "document_enhanced":
            bonus = 3
        elif attempt.preprocessing == "upscaled_enhanced":
            bonus = 3
        return base + bonus
    return base


def _score_ocr_attempt(
    normalized_text: str,
    parsed: ParsedEvidenceFields,
    evidence_type: EvidenceType,
    *,
    preprocessing: str,
    average_confidence: float,
    line_count: int,
) -> float:
    score = 0.0
    score += 18 if parsed.evidence_date else 0
    score += 30 if parsed.merchant_name else 0
    score += 32 if parsed.amount is not None else 0
    score += 12 if parsed.payment_method else 0
    score += average_confidence * 15
    score += min(line_count, 12)
    score += min(len(normalized_text), 600) / 60
    if evidence_type == EvidenceType.PHYSICAL_RECEIPT and preprocessing == "adaptive_binary":
        score += 4
    if evidence_type == EvidenceType.PHYSICAL_RECEIPT and preprocessing == "background_cropped":
        score += 3
    if preprocessing == "upscaled_enhanced":
        score += 3
    if preprocessing == "sharpened":
        score += 2
    return round(score, 4)


def _build_receipt_lines(
    normalized_text: str,
    ocr_lines: list[OCRLine] | None,
) -> list[OCRLine]:
    if ocr_lines:
        prepared: list[OCRLine] = []
        for index, line in enumerate(ocr_lines):
            text = normalize_extracted_text(line.text)
            if not text:
                continue
            if line.box:
                box = [[float(point[0]), float(point[1])] for point in line.box]
                x = min(point[0] for point in box)
                y = min(point[1] for point in box)
            else:
                y = float(index * 20)
                box = _make_box(0.0, y, max(float(len(text) * 10), 40.0), y + 12.0)
                x = 0.0
            prepared.append(
                OCRLine(
                    text=text,
                    confidence=line.confidence,
                    box=box,
                    y=y,
                    x=x,
                )
            )
        return _merge_receipt_lines(prepared)
    return _build_synthetic_lines(normalized_text)


def _build_synthetic_lines(normalized_text: str) -> list[OCRLine]:
    lines: list[OCRLine] = []
    for index, line in enumerate(part for part in normalized_text.splitlines() if part):
        y = float(index * 20)
        lines.append(
            OCRLine(
                text=line,
                confidence=1.0,
                box=_make_box(0.0, y, max(float(len(line) * 10), 40.0), y + 12.0),
                y=y,
                x=0.0,
            )
        )
    return lines


def _merge_receipt_lines(lines: list[OCRLine]) -> list[OCRLine]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (_line_top(line), _line_left(line)))
    heights = [_line_height(line) for line in ordered if _line_height(line) > 0]
    row_threshold = max(12.0, (median(heights) if heights else 18.0) * 0.7)

    merged: list[OCRLine] = []
    current_group: list[OCRLine] = [ordered[0]]
    for line in ordered[1:]:
        current_center = sum(_line_center_y(item) for item in current_group) / len(current_group)
        if abs(_line_center_y(line) - current_center) <= row_threshold:
            current_group.append(line)
            continue
        merged.append(_merge_line_group(current_group))
        current_group = [line]
    merged.append(_merge_line_group(current_group))
    return merged


def _merge_line_group(group: list[OCRLine]) -> OCRLine:
    ordered = sorted(group, key=_line_left)
    text = normalize_extracted_text(" ".join(line.text for line in ordered if line.text))
    left = min(_line_left(line) for line in ordered)
    top = min(_line_top(line) for line in ordered)
    right = max(_line_right(line) for line in ordered)
    bottom = max(_line_bottom(line) for line in ordered)
    average_confidence = sum(line.confidence for line in ordered) / len(ordered)
    return OCRLine(
        text=text,
        confidence=average_confidence,
        box=_make_box(left, top, right, bottom),
        y=top,
        x=left,
    )


def _extract_date_from_layout(lines: list[OCRLine]) -> date | None:
    if not lines:
        return None

    today = date.today()
    page_height = _page_height(lines)
    best_match: tuple[float, date] | None = None

    for index, line in enumerate(lines):
        contexts = [line.text]
        if index + 1 < len(lines) and _vertical_gap(line, lines[index + 1]) <= 28:
            contexts.append(f"{line.text} {lines[index + 1].text}")

        for context_text in contexts:
            lowered = context_text.lower()
            keyword_bonus = 22 if any(keyword in lowered for keyword in DATE_KEYWORDS) else 0
            for pattern in DATE_PATTERNS:
                for match in pattern.finditer(context_text):
                    year = int(match.group("year"))
                    if year < 100:
                        year += 2000
                    month = int(match.group("month"))
                    day = int(match.group("day"))
                    try:
                        parsed_date = date(year, month, day)
                    except ValueError:
                        continue

                    score = 50 + keyword_bonus - (index * 0.8)
                    if _normalized_y(line, page_height) <= 0.35:
                        score += 6
                    score -= abs(parsed_date.year - today.year) * 4
                    if parsed_date.year > today.year + 1 or parsed_date.year < today.year - 4:
                        score -= 12
                    if best_match is None or score > best_match[0]:
                        best_match = (score, parsed_date)

    return best_match[1] if best_match else None


def _extract_payment_method_from_layout(
    lines: list[OCRLine],
    evidence_type: EvidenceType,
) -> PaymentMethod | None:
    text = "\n".join(line.text for line in lines).lower()
    if any(keyword in text for keyword in ONLINE_PAYMENT_KEYWORDS):
        return PaymentMethod.ONLINE_PAYMENT
    if evidence_type == EvidenceType.BANK_TRANSFER_STATEMENT or any(
        keyword in text for keyword in BANK_TRANSFER_KEYWORDS
    ):
        return PaymentMethod.BANK_TRANSFER
    if any(keyword in text for keyword in CASH_KEYWORDS):
        return PaymentMethod.CASH
    if any(keyword in text for keyword in CARD_KEYWORDS):
        return PaymentMethod.CARD
    if evidence_type == EvidenceType.E_RECEIPT:
        return PaymentMethod.ONLINE_PAYMENT
    return None


def _extract_amount_from_layout(
    lines: list[OCRLine],
    evidence_type: EvidenceType,
    *,
    payment_method: PaymentMethod | None = None,
) -> Decimal | None:
    del evidence_type
    if not lines:
        return None

    payment_method = payment_method or _extract_payment_method_from_layout(
        lines,
        EvidenceType.PHYSICAL_RECEIPT,
    )
    page_height = _page_height(lines)
    derived_cash_total = _derive_cash_total_from_layout(lines)
    candidates: list[_AmountCandidate] = []
    line_item_hints = (
        "수량",
        "단가",
        "qty",
        "item",
        "ea",
        "공급가액",
        "부가세",
        "과세",
        "면세",
        "봉사료",
        "할인",
        "메뉴",
    )

    for index, line in enumerate(lines):
        line_text = line.text
        line_lower = line_text.lower()
        previous_lower = lines[index - 1].text.lower() if index > 0 else ""
        next_lower = lines[index + 1].text.lower() if index + 1 < len(lines) else ""
        context_lower = " ".join(part for part in (previous_lower, line_lower, next_lower) if part)
        current_values = list(_iter_amount_values(line_text))
        line_has_total_hint = any(hint in line_lower for hint in TOTAL_HINTS)
        adjacent_total_hint = any(hint in previous_lower for hint in TOTAL_HINTS) or any(
            hint in next_lower for hint in TOTAL_HINTS
        )
        context_has_total_hint = line_has_total_hint or adjacent_total_hint

        if current_values and not _should_reject_amount_context(line_text, context_has_total_hint):
            for number_text, value in current_values:
                digits = re.sub(r"\D", "", number_text)
                if len(digits) >= 9:
                    continue

                score = 12.0
                if line_has_total_hint:
                    score += 62
                elif adjacent_total_hint:
                    score += 38
                if any(word in context_lower for word in ("결제", "승인", "합계", "total", "paid")):
                    score += 10
                if any(word in context_lower for word in ("원", "krw", "won")):
                    score += 6
                if value >= Decimal("1000"):
                    score += 8
                if value >= Decimal("10000"):
                    score += 4
                if _normalized_y(line, page_height) >= 0.45:
                    score += 10
                if index >= max(len(lines) - 5, 0):
                    score += 4
                if len(current_values) == 1 and line_has_total_hint:
                    score += 10
                if any(word in line_lower for word in line_item_hints) and not line_has_total_hint:
                    score -= 30
                if len(current_values) >= 3 and not line_has_total_hint:
                    score -= 22
                if len(digits) >= 7 and not context_has_total_hint:
                    score -= 32
                if (
                    any(keyword in context_lower for keyword in AMOUNT_REJECTION_KEYWORDS)
                    and not context_has_total_hint
                ):
                    score -= 55
                if payment_method == PaymentMethod.CASH and any(
                    word in line_lower for word in ("받을금액", "받은금액", "을금액", "은금액", "거스름돈", "스름돈", "거스름")
                ):
                    score -= 18
                if _normalized_y(line, page_height) <= 0.22 and not context_has_total_hint:
                    score -= 12

                candidates.append(
                    _AmountCandidate(
                        value=value,
                        score=score,
                        line_index=index,
                        line=line_text,
                        has_total_hint=context_has_total_hint,
                    )
                )

        if not line_has_total_hint or current_values:
            continue

        for offset in (1, 2):
            if index + offset >= len(lines):
                break
            nearby_line = lines[index + offset]
            if _vertical_gap(line, nearby_line) > 28:
                break
            if _should_reject_amount_context(nearby_line.text, True):
                continue

            for number_text, value in _iter_amount_values(nearby_line.text):
                digits = re.sub(r"\D", "", number_text)
                if len(digits) >= 9:
                    continue
                score = 74 - (offset * 6)
                if _normalized_y(nearby_line, page_height) >= 0.45:
                    score += 10
                if value >= Decimal("1000"):
                    score += 8
                candidates.append(
                    _AmountCandidate(
                        value=value,
                        score=score,
                        line_index=index + offset,
                        line=f"{line.text} -> {nearby_line.text}",
                        has_total_hint=True,
                    )
                )

        for offset in (1, 2):
            backward_index = index - offset
            if backward_index < 0:
                break
            nearby_line = lines[backward_index]
            if _vertical_gap(nearby_line, line) > 28:
                break
            if _should_reject_amount_context(nearby_line.text, True):
                continue

            for number_text, value in _iter_amount_values(nearby_line.text):
                digits = re.sub(r"\D", "", number_text)
                if len(digits) >= 9:
                    continue
                score = 70 - (offset * 6)
                if _normalized_y(nearby_line, page_height) >= 0.45:
                    score += 10
                if value >= Decimal("1000"):
                    score += 8
                candidates.append(
                    _AmountCandidate(
                        value=value,
                        score=score,
                        line_index=backward_index,
                        line=f"{nearby_line.text} <- {line.text}",
                        has_total_hint=True,
                    )
                )

    if derived_cash_total is not None:
        candidates.append(
            _AmountCandidate(
                value=derived_cash_total,
                score=88,
                line_index=len(lines),
                line="cash_difference",
                has_total_hint=True,
            )
        )

    if not candidates:
        return None

    value_counts = Counter(candidate.value for candidate in candidates)
    return max(
        candidates,
        key=lambda candidate: (
            candidate.has_total_hint,
            round(candidate.score + min((value_counts[candidate.value] - 1) * 12, 24), 4),
            value_counts[candidate.value],
            candidate.value,
        ),
    ).value


def _extract_merchant_from_layout(lines: list[OCRLine], evidence_type: EvidenceType) -> str | None:
    if not lines:
        return None

    if evidence_type == EvidenceType.BANK_TRANSFER_STATEMENT:
        merchant = _extract_labelled_value_from_layout(lines, ("받는분", "받는분명", "받는 사람"))
        if merchant:
            return merchant
    if evidence_type == EvidenceType.E_RECEIPT:
        merchant = _extract_labelled_value_from_layout(
            lines,
            ("판매처", "가맹점명", "가맹점", "merchant", "store"),
        )
        if merchant:
            return merchant

    labelled = _extract_labelled_value_from_layout(lines, MERCHANT_LABELS)
    if labelled:
        return labelled

    page_height = _page_height(lines)
    page_width = _page_width(lines)
    structural_index = _find_first_structural_line_index(lines)
    search_limit = min(
        len(lines),
        max(6, (structural_index + 2) if structural_index is not None else 8),
    )
    candidates: list[tuple[float, int, int, str]] = []

    for index, line in enumerate(lines[:search_limit]):
        raw_line = line.text
        candidate = _clean_merchant_candidate(raw_line)
        if not candidate:
            continue

        lowered = candidate.lower()
        score = 38.0 - (index * 2.5)
        if _normalized_y(line, page_height) <= 0.18:
            score += 14
        elif _normalized_y(line, page_height) <= 0.32:
            score += 8
        if structural_index is not None and index < structural_index:
            score += 12
        if structural_index is not None and index + 1 == structural_index:
            score += 8
        if _contains_korean(candidate):
            score += 14
        score += _alpha_ratio(candidate) * 10
        if len(candidate) >= 5:
            score += 6
        width_ratio = (_line_width(line) / page_width) if page_width else 0.0
        if 0.2 <= width_ratio <= 0.92:
            score += 5
        if len(candidate) <= 3:
            score -= 24
        if _looks_like_address(candidate):
            score -= 40
        if BUSINESS_NUMBER_PATTERN.search(candidate) or PHONE_PATTERN.search(candidate):
            score -= 45
        if any(
            keyword in raw_line.lower()
            for keyword in (
                DATE_KEYWORDS
                + TOTAL_HINTS
                + CARD_KEYWORDS
                + CASH_KEYWORDS
                + ONLINE_PAYMENT_KEYWORDS
            )
        ):
            score -= 34
        if any(keyword in lowered for keyword in MERCHANT_NOISE_KEYWORDS):
            score -= 28
        if _is_ascii_only(candidate):
            score -= 18
        if index + 1 < search_limit:
            next_candidate = _clean_merchant_candidate(lines[index + 1].text)
            if (
                next_candidate
                and _contains_korean(next_candidate)
                and not _contains_korean(candidate)
                and len(candidate) <= 10
            ):
                score -= 18
        if index + 1 < len(lines):
            next_text = lines[index + 1].text
            if BUSINESS_NUMBER_PATTERN.search(next_text) or PHONE_PATTERN.search(next_text):
                score += 14

        if score > 0:
            candidates.append((round(score, 4), len(candidate), -index, candidate))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def _extract_labelled_value_from_layout(
    lines: list[OCRLine],
    labels: tuple[str, ...],
) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]?\s*(.*)$", flags=re.IGNORECASE)
    for index, line in enumerate(lines[:10]):
        match = pattern.match(line.text)
        if match is None:
            continue
        candidate = _clean_merchant_candidate(match.group(1))
        if candidate:
            return candidate
        if index + 1 < len(lines):
            fallback = _clean_merchant_candidate(lines[index + 1].text)
            if fallback:
                return fallback
    return None


def _derive_cash_total_from_layout(lines: list[OCRLine]) -> Decimal | None:
    received: Decimal | None = None
    change: Decimal | None = None
    total: Decimal | None = None
    for line in lines:
        lowered = line.text.lower()
        values = [value for _, value in _iter_amount_values(line.text)]
        if not values:
            continue
        if any(kw in lowered for kw in ("받을금액", "받은금액", "을금액", "은금액", "받을 금액", "받은 금액")):
            received = max(values)
        elif any(kw in lowered for kw in ("거스름돈", "스름돈", "거스름", "거슬러")):
            change = max(values)
        elif any(hint in lowered for hint in TOTAL_HINTS):
            total = max(values)
    if total is not None:
        return total
    if received is not None and change is not None:
        difference = received - change
        if difference > 0:
            return difference
    return None


def _iter_amount_values(text: str) -> list[tuple[str, Decimal]]:
    values: list[tuple[str, Decimal]] = []
    for match in AMOUNT_PATTERN.finditer(text):
        number_text = match.group(1)
        value = _parse_decimal_value(number_text)
        if value is None or value <= 0:
            continue
        values.append((number_text, value))
    return values


def _should_reject_amount_context(text: str, has_total_hint: bool) -> bool:
    if has_total_hint:
        return False
    lowered = text.lower()
    if BUSINESS_NUMBER_PATTERN.search(text) or PHONE_PATTERN.search(text):
        return True
    return any(keyword in lowered for keyword in AMOUNT_REJECTION_KEYWORDS)


def _find_first_structural_line_index(lines: list[OCRLine]) -> int | None:
    for index, line in enumerate(lines[:10]):
        text = line.text
        lowered = text.lower()
        if BUSINESS_NUMBER_PATTERN.search(text) or PHONE_PATTERN.search(text):
            return index
        if any(
            keyword in lowered
            for keyword in (
                DATE_KEYWORDS
                + TOTAL_HINTS
                + CARD_KEYWORDS
                + CASH_KEYWORDS
                + ONLINE_PAYMENT_KEYWORDS
            )
        ):
            return index
        if index > 0 and _looks_like_address(text):
            return index
    return None


def _make_box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _line_left(line: OCRLine) -> float:
    if line.box:
        return min(point[0] for point in line.box)
    return line.x


def _line_right(line: OCRLine) -> float:
    if line.box:
        return max(point[0] for point in line.box)
    return line.x


def _line_top(line: OCRLine) -> float:
    if line.box:
        return min(point[1] for point in line.box)
    return line.y


def _line_bottom(line: OCRLine) -> float:
    if line.box:
        return max(point[1] for point in line.box)
    return line.y


def _line_width(line: OCRLine) -> float:
    return max(_line_right(line) - _line_left(line), 1.0)


def _line_height(line: OCRLine) -> float:
    return max(_line_bottom(line) - _line_top(line), 1.0)


def _line_center_y(line: OCRLine) -> float:
    return _line_top(line) + (_line_height(line) / 2.0)


def _page_height(lines: list[OCRLine]) -> float:
    if not lines:
        return 1.0
    return max(_line_bottom(line) for line in lines)


def _page_width(lines: list[OCRLine]) -> float:
    if not lines:
        return 1.0
    return max(_line_right(line) for line in lines)


def _normalized_y(line: OCRLine, page_height: float) -> float:
    if page_height <= 0:
        return 0.0
    return _line_top(line) / page_height


def _vertical_gap(previous: OCRLine, current: OCRLine) -> float:
    return max(_line_top(current) - _line_bottom(previous), 0.0)


def _contains_korean(value: str) -> bool:
    return any("가" <= character <= "힣" for character in value)


def _is_ascii_only(value: str) -> bool:
    has_alpha = any(character.isalpha() for character in value)
    return has_alpha and all(ord(character) < 128 for character in value if character.isalpha())


def _clean_merchant_candidate(value: str) -> str | None:
    candidate = normalize_extracted_text(value).replace(":", " ").replace("|", " ")
    for label in MERCHANT_LABELS:
        candidate = re.sub(
            rf"^\s*{re.escape(label)}\s*[:：-]?\s*",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    candidate = re.sub(
        r"\b(?:사업자번호|사업자등록번호|대표자|전화번호|주소)\b.*$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = BUSINESS_NUMBER_PATTERN.sub("", candidate)
    candidate = PHONE_PATTERN.sub("", candidate)
    candidate = re.sub(r"\b(?:tel|fax)\b.*$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" -:/")
    if not candidate:
        return None
    if any(keyword in candidate.lower() for keyword in MERCHANT_NOISE_KEYWORDS):
        keep_words = (
            "카페", "식당", "편의점", "주유", "영업소", "마트", "스토어", "store",
            "볼링", "당구", "노래", "pc방", "피씨방", "플레이", "play",
            "마라탕", "치킨", "피자", "버거", "빵", "베이커리", "bakery",
            "약국", "병원", "의원", "센터", "학원", "서점",
            "CU", "GS", "세븐", "이마트", "롯데", "현대",
            "프라자", "plaza", "타워", "tower",
        )
        if not any(word in candidate for word in keep_words):
            return None
    return candidate or None


def _looks_like_address(value: str) -> bool:
    lowered = value.lower()
    hint_count = sum(hint.lower() in lowered for hint in ADDRESS_HINTS)
    return hint_count >= 2 or bool(re.search(r"\d{1,4}-\d{1,4}", lowered))


def _alpha_ratio(value: str) -> float:
    alpha_count = sum(character.isalpha() for character in value)
    if not value:
        return 0.0
    return alpha_count / len(value)


def _parse_decimal_value(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _serialize_parsed_fields(fields: ParsedEvidenceFields) -> dict[str, Any]:
    return {
        "evidence_date": fields.evidence_date.isoformat() if fields.evidence_date else None,
        "merchant_name": fields.merchant_name,
        "amount": str(fields.amount) if fields.amount is not None else None,
        "payment_method": fields.payment_method.value if fields.payment_method else None,
        "confidence": round(fields.confidence, 4),
    }


def _serialize_field_value(field_name: str, value: Any) -> Any:
    if field_name == "date" and isinstance(value, date):
        return value.isoformat()
    if field_name == "amount" and isinstance(value, Decimal):
        return str(value)
    if field_name == "payment" and isinstance(value, PaymentMethod):
        return value.value
    return value


def _distance(point_a: Any, point_b: Any) -> float:
    dx = float(point_a[0]) - float(point_b[0])
    dy = float(point_a[1]) - float(point_b[1])
    return (dx * dx + dy * dy) ** 0.5


def _order_points(points: Any) -> list[list[float]]:
    points_list = [[float(point[0]), float(point[1])] for point in points]
    sums = [point[0] + point[1] for point in points_list]
    diffs = [point[0] - point[1] for point in points_list]
    top_left = points_list[sums.index(min(sums))]
    bottom_right = points_list[sums.index(max(sums))]
    top_right = points_list[diffs.index(min(diffs))]
    bottom_left = points_list[diffs.index(max(diffs))]
    return [top_left, top_right, bottom_right, bottom_left]
