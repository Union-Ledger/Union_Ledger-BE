"""영수증 OCR 라인 → 최종 결제액 추출 파이프라인 (단계별 추적 가능).

설계 배경
---------
결산 시스템은 추출된 금액을 **통장/카드 명세와 대조**한다. 따라서 정답은
실제로 카드에서 출금된 ``카드결제``(승인 금액)이며, 분할결제·할인·포인트
차감 이전 값일 수 있는 ``합계금액``이 아니다. 그래서 본 파서는 라벨이 붙은
금액들을 분류한 뒤 ``결제액 > 합계 > 항목 최댓값`` 우선순위로 최종 금액을
고른다.

이 모듈은 PaddleOCR(``evidence_extraction.OCRLine``)의 라인 단위 출력을
입력으로 받아, 사람이 검수할 수 있도록 **단계마다 직렬화 가능한 산출물**을
남긴다. 외부 의존성 없이 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

# ── 라벨 사전 ────────────────────────────────────────────────────────────────
# 우선순위가 높을수록 "실제 결제액"에 가깝다. 결산 대조 정답은 카드 승인액이므로
# 결제 라벨에 가장 높은 우선순위를 둔다.
PAYMENT_KEYWORDS: tuple[str, ...] = (
    "카드결제",
    "신용승인",
    "승인금액",
    "결제금액",
    "현금결제",
    "체크카드",
    "신용카드",
    "받은금액",
    "받을금액",
)
TOTAL_KEYWORDS: tuple[str, ...] = (
    "합계금액",
    "합계",
    "총액",
    "총계",
    "청구금액",
    "판매합계",
    "주문금액",
)
# 결제액 후보에서 제외해야 하는 라벨 — 부분 금액(세금·공급가·봉사료)이라
# 절대 최종 결제액이 될 수 없다.
SUBTOTAL_KEYWORDS: tuple[str, ...] = (
    "과세물품가액",
    "과세 물품가액",
    "공급가액",
    "면세물품가액",
    "부가세",
    "부가가치세",
    "봉사료",
    "소계",
    "할인",
    "포인트",
)
# 금액처럼 생긴 숫자가 있어도 금액으로 보면 안 되는 메타 라인.
META_KEYWORDS: tuple[str, ...] = (
    "사업자",
    "전화",
    "tel",
    "승인번호",
    "거래번호",
    "카드번호",
    "주문번호",
    "단말기",
    "가맹점번호",
    "사업자번호",
    "판매일자",
    "결제일시",
    "테이블",
)

# 천 단위 콤마가 있는 금액 토큰만 잡는다. 영수증에서 수량(12, 54)은 콤마가
# 없으므로 이 정규식만으로 수량과 금액이 자연스럽게 분리된다.
AMOUNT_PATTERN = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?!\d)")

# 분류 라벨
LABEL_PAYMENT = "payment"
LABEL_TOTAL = "total"
LABEL_SUBTOTAL = "subtotal"
LABEL_META = "meta"
LABEL_ITEM = "item"


@dataclass(slots=True)
class AmountToken:
    """원문 표기와 정규화된 정수 값."""

    raw: str
    value: int

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "value": self.value}


@dataclass(slots=True)
class ClassifiedLine:
    """한 OCR 라인의 분류 결과 + 추출된 금액들."""

    index: int
    text: str
    confidence: float
    label: str
    matched_keyword: str | None
    amounts: list[AmountToken] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "label": self.label,
            "matched_keyword": self.matched_keyword,
            "amounts": [amount.to_dict() for amount in self.amounts],
        }


@dataclass(slots=True)
class ParseTrace:
    """단계별 산출물 묶음. 그대로 JSON으로 직렬화해 검수에 쓴다."""

    stage0_raw_lines: list[dict[str, Any]]
    stage1_amount_tokens: list[dict[str, Any]]
    stage2_classified_lines: list[dict[str, Any]]
    stage3_candidates: dict[str, Any]
    stage4_decision: dict[str, Any]
    final_amount: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_amount(raw: str) -> int:
    """``"234,500"`` → ``234500``."""

    return int(raw.replace(",", ""))


def _contains_keyword(text: str, keywords: Iterable[str]) -> str | None:
    """공백을 무시하고 키워드 포함 여부를 본다 (``과세 물품가액`` 대응)."""

    compact = text.replace(" ", "")
    for keyword in keywords:
        if keyword.replace(" ", "") in compact:
            return keyword
    return None


def _classify_line(index: int, text: str, confidence: float) -> ClassifiedLine:
    """라인 1개를 라벨링하고 금액 토큰을 뽑는다.

    우선순위: 메타 > 소계/세금 > 결제 > 합계 > (금액만 있으면) 항목.
    메타·소계를 먼저 걸러야 부가세(77,676)·공급가액(776,824)이 결제액
    후보로 새어 들어가지 않는다.
    """

    amounts = [
        AmountToken(raw=match, value=_normalize_amount(match))
        for match in AMOUNT_PATTERN.findall(text)
    ]

    meta_kw = _contains_keyword(text, META_KEYWORDS)
    if meta_kw is not None:
        return ClassifiedLine(index, text, confidence, LABEL_META, meta_kw, amounts)

    subtotal_kw = _contains_keyword(text, SUBTOTAL_KEYWORDS)
    if subtotal_kw is not None:
        return ClassifiedLine(index, text, confidence, LABEL_SUBTOTAL, subtotal_kw, amounts)

    payment_kw = _contains_keyword(text, PAYMENT_KEYWORDS)
    if payment_kw is not None:
        return ClassifiedLine(index, text, confidence, LABEL_PAYMENT, payment_kw, amounts)

    total_kw = _contains_keyword(text, TOTAL_KEYWORDS)
    if total_kw is not None:
        return ClassifiedLine(index, text, confidence, LABEL_TOTAL, total_kw, amounts)

    label = LABEL_ITEM if amounts else LABEL_META
    return ClassifiedLine(index, text, confidence, label, None, amounts)


def _amounts_for_label(
    classified: list[ClassifiedLine], label: str
) -> list[dict[str, Any]]:
    """특정 라벨 라인들의 금액 후보를 평탄화한다.

    라벨 라인에 금액이 같이 없으면(OCR이 금액을 다음 줄로 분리한 경우)
    바로 뒤따르는 3개 라인 안에서 첫 금액 토큰을 연결한다.
    """

    candidates: list[dict[str, Any]] = []
    for position, line in enumerate(classified):
        if line.label != label:
            continue
        amounts = line.amounts
        source_index = line.index
        if not amounts:
            for following in classified[position + 1 : position + 4]:
                if following.amounts:
                    amounts = following.amounts
                    source_index = following.index
                    break
        for amount in amounts:
            candidates.append(
                {
                    "value": amount.value,
                    "raw": amount.raw,
                    "line_index": line.index,
                    "amount_source_line": source_index,
                    "matched_keyword": line.matched_keyword,
                }
            )
    return candidates


def parse_payment_amount(lines: Iterable[Any]) -> ParseTrace:
    """OCR 라인들 → 최종 결제액. 단계별 추적 산출물을 함께 반환한다.

    ``lines`` 는 ``OCRLine`` 객체(.text/.confidence)나 ``{"text", "confidence"}``
    딕셔너리의 이터러블을 모두 받는다.
    """

    # ── Stage 0: raw OCR 라인 (PaddleOCR 원본 산출물) ──────────────────────
    normalized_lines: list[ClassifiedLine] = []
    stage0: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if isinstance(line, dict):
            text = line.get("text") or ""
            confidence_raw = line.get("confidence")
        else:
            text = getattr(line, "text", None) or ""
            confidence_raw = getattr(line, "confidence", None)
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        text = text.strip()
        if not text:
            continue
        stage0.append({"index": index, "text": text, "confidence": round(confidence, 4)})
        normalized_lines.append(_classify_line(index, text, confidence))

    # ── Stage 1: 금액 토큰 추출 + 정규화 ──────────────────────────────────
    stage1: list[dict[str, Any]] = []
    for line in normalized_lines:
        for amount in line.amounts:
            stage1.append(
                {
                    "line_index": line.index,
                    "raw": amount.raw,
                    "normalized": amount.value,
                }
            )

    # ── Stage 2: 라인 분류 (메타/세금/결제/합계/항목) ────────────────────
    stage2 = [line.to_dict() for line in normalized_lines]

    # ── Stage 3: 의미 라벨별 금액 후보 ────────────────────────────────────
    payment_candidates = _amounts_for_label(normalized_lines, LABEL_PAYMENT)
    total_candidates = _amounts_for_label(normalized_lines, LABEL_TOTAL)
    item_candidates = _amounts_for_label(normalized_lines, LABEL_ITEM)
    subtotal_candidates = _amounts_for_label(normalized_lines, LABEL_SUBTOTAL)
    stage3 = {
        "payment": payment_candidates,
        "total": total_candidates,
        "item": item_candidates,
        "subtotal_excluded": subtotal_candidates,
    }

    # ── Stage 4: 우선순위 규칙으로 최종 결제액 결정 ──────────────────────
    # 결산 대조 정답 = 실제 카드 승인액. 결제 라벨을 최우선, 없으면 합계,
    # 그래도 없으면 항목 최댓값으로 폴백한다.
    decision: dict[str, Any] = {"rule": "payment > total > item_max", "reasoning": []}
    final_amount: int | None = None

    if payment_candidates:
        chosen = payment_candidates[0]
        final_amount = chosen["value"]
        decision["selected_from"] = LABEL_PAYMENT
        decision["selected"] = chosen
        decision["reasoning"].append(
            f"'{chosen['matched_keyword']}' 라벨에서 결제액 {chosen['value']:,}원을 채택 "
            f"(결산 대조 정답은 실제 카드 승인액)."
        )
        if total_candidates:
            total_value = max(item["value"] for item in total_candidates)
            decision["reasoning"].append(
                f"합계금액 {total_value:,}원은 분할결제·할인 전 값일 수 있어 후순위로 제외."
            )
    elif total_candidates:
        chosen = max(total_candidates, key=lambda item: item["value"])
        final_amount = chosen["value"]
        decision["selected_from"] = LABEL_TOTAL
        decision["selected"] = chosen
        decision["reasoning"].append(
            f"결제 라벨이 없어 합계금액 {chosen['value']:,}원을 채택."
        )
    elif item_candidates:
        chosen = max(item_candidates, key=lambda item: item["value"])
        final_amount = chosen["value"]
        decision["selected_from"] = LABEL_ITEM
        decision["selected"] = chosen
        decision["reasoning"].append(
            f"결제·합계 라벨이 모두 없어 항목 최댓값 {chosen['value']:,}원으로 폴백."
        )
    else:
        decision["selected_from"] = None
        decision["selected"] = None
        decision["reasoning"].append("금액 후보를 찾지 못했습니다.")

    if subtotal_candidates:
        excluded = ", ".join(f"{item['value']:,}원" for item in subtotal_candidates)
        decision["reasoning"].append(f"세금·공급가액({excluded})은 부분 금액이라 후보에서 제외.")

    return ParseTrace(
        stage0_raw_lines=stage0,
        stage1_amount_tokens=stage1,
        stage2_classified_lines=stage2,
        stage3_candidates=stage3,
        stage4_decision=decision,
        final_amount=final_amount,
    )
