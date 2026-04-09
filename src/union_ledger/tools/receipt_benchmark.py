from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from union_ledger.models.enums import EvidenceType, PaymentMethod
from union_ledger.services.evidence_extraction import EvidenceExtractionService


@dataclass(slots=True)
class GroundTruthRow:
    file_name: str
    evidence_type: EvidenceType
    expected_date: date | None
    expected_merchant_name: str | None
    expected_amount: Decimal | None
    expected_payment_method: PaymentMethod | None
    notes: str | None
    split: str | None
    difficulty: str | None


@dataclass(slots=True)
class FieldEvaluation:
    expected: str | None
    predicted: str | None
    exact_match: bool
    normalized_match: bool
    similarity: float | None


@dataclass(slots=True)
class ReceiptEvaluation:
    file_name: str
    evidence_type: EvidenceType
    notes: str | None
    split: str | None
    difficulty: str | None
    overall_pass: bool
    date_result: FieldEvaluation
    merchant_result: FieldEvaluation
    amount_result: FieldEvaluation
    payment_method_result: FieldEvaluation
    failure_reasons: list[str]
    extraction_payload: dict[str, Any]
    raw_text_preview: str


def load_ground_truth(csv_path: Path) -> list[GroundTruthRow]:
    rows: list[GroundTruthRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for index, row in enumerate(reader, start=2):
            file_name = (row.get("file_name") or "").strip()
            evidence_type_value = (row.get("evidence_type") or "").strip()
            if not file_name or not evidence_type_value:
                continue

            try:
                evidence_type = EvidenceType(evidence_type_value)
            except ValueError as exc:
                raise ValueError(
                    f"{csv_path}:{index} evidence_type 값이 올바르지 않습니다."
                ) from exc

            rows.append(
                GroundTruthRow(
                    file_name=file_name,
                    evidence_type=evidence_type,
                    expected_date=_parse_date(row.get("expected_date")),
                    expected_merchant_name=_clean_optional_text(row.get("expected_merchant_name")),
                    expected_amount=_parse_decimal(row.get("expected_amount")),
                    expected_payment_method=_parse_payment_method(
                        row.get("expected_payment_method")
                    ),
                    notes=_clean_optional_text(row.get("notes")),
                    split=_clean_optional_text(row.get("split")),
                    difficulty=_clean_optional_text(row.get("difficulty")),
                )
            )
    return rows


def evaluate_receipts(
    *,
    samples_root: Path,
    ground_truth_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    extractor = EvidenceExtractionService()
    evaluations: list[ReceiptEvaluation] = []

    for row in load_ground_truth(ground_truth_path):
        sample_path = _resolve_sample_path(samples_root, row.file_name)
        result = extractor.extract_file(sample_path, row.evidence_type)
        evaluation = ReceiptEvaluation(
            file_name=row.file_name,
            evidence_type=row.evidence_type,
            notes=row.notes,
            split=row.split,
            difficulty=row.difficulty,
            overall_pass=False,
            date_result=_evaluate_scalar(
                expected=row.expected_date.isoformat() if row.expected_date else None,
                predicted=result.evidence_date.isoformat() if result.evidence_date else None,
            ),
            merchant_result=_evaluate_merchant_name(
                expected=row.expected_merchant_name,
                predicted=result.merchant_name,
            ),
            amount_result=_evaluate_scalar(
                expected=str(row.expected_amount) if row.expected_amount is not None else None,
                predicted=str(result.amount) if result.amount is not None else None,
            ),
            payment_method_result=_evaluate_scalar(
                expected=row.expected_payment_method.value if row.expected_payment_method else None,
                predicted=result.payment_method.value if result.payment_method else None,
            ),
            failure_reasons=[],
            extraction_payload=result.payload,
            raw_text_preview=result.raw_text[:600],
        )
        evaluation.overall_pass = all(
            (
                evaluation.date_result.exact_match,
                evaluation.merchant_result.normalized_match,
                evaluation.amount_result.exact_match,
                evaluation.payment_method_result.exact_match,
            )
        )
        evaluation.failure_reasons = _classify_failure_reasons(evaluation)
        evaluations.append(evaluation)

    report = {
        "ground_truth_path": str(ground_truth_path),
        "samples_root": str(samples_root),
        "summary": _build_summary(evaluations),
        "evaluations": [_serialize_evaluation(item) for item in evaluations],
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark OCR results against labeled receipts.")
    parser.add_argument("--samples-root", default="samples/receipts")
    parser.add_argument("--ground-truth", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    samples_root = Path(args.samples_root).resolve()
    ground_truth_path = _resolve_ground_truth_path(samples_root, args.ground_truth)
    output_path = Path(args.output).resolve() if args.output else None
    report = evaluate_receipts(
        samples_root=samples_root,
        ground_truth_path=ground_truth_path,
        output_path=output_path,
    )
    _print_report(report)
    return 0


def _resolve_ground_truth_path(samples_root: Path, explicit_path: str | None) -> Path:
    if explicit_path:
        return Path(explicit_path).resolve()

    for candidate_name in ("ground_truth.local.csv", "ground_truth.template.csv"):
        candidate = samples_root / candidate_name
        if candidate.exists():
            return candidate
    raise FileNotFoundError("ground truth CSV 파일을 찾지 못했습니다.")


def _resolve_sample_path(samples_root: Path, file_name: str) -> Path:
    direct_path = samples_root / file_name
    if direct_path.exists():
        return direct_path

    matches = list(samples_root.rglob(file_name))
    if not matches:
        raise FileNotFoundError(f"샘플 파일을 찾지 못했습니다: {file_name}")
    if len(matches) > 1:
        raise FileExistsError(f"동일한 파일명이 여러 개 있습니다: {file_name}")
    return matches[0]


def _evaluate_scalar(expected: str | None, predicted: str | None) -> FieldEvaluation:
    exact_match = expected == predicted
    return FieldEvaluation(
        expected=expected,
        predicted=predicted,
        exact_match=exact_match,
        normalized_match=exact_match,
        similarity=1.0 if exact_match and expected is not None else None,
    )


def _evaluate_merchant_name(expected: str | None, predicted: str | None) -> FieldEvaluation:
    normalized_expected = _normalize_match_text(expected)
    normalized_predicted = _normalize_match_text(predicted)
    exact_match = expected == predicted
    normalized_match = bool(
        normalized_expected
        and normalized_predicted
        and (
            normalized_expected == normalized_predicted
            or normalized_expected in normalized_predicted
            or normalized_predicted in normalized_expected
        )
    )
    similarity = None
    if normalized_expected and normalized_predicted:
        similarity = round(
            SequenceMatcher(None, normalized_expected, normalized_predicted).ratio(),
            4,
        )
    return FieldEvaluation(
        expected=expected,
        predicted=predicted,
        exact_match=exact_match,
        normalized_match=normalized_match,
        similarity=similarity,
    )


def _build_summary(evaluations: list[ReceiptEvaluation]) -> dict[str, Any]:
    total = len(evaluations)
    summary = {
        "total_samples": total,
        "overall_pass_count": sum(item.overall_pass for item in evaluations),
        "overall_pass_rate": _rate(sum(item.overall_pass for item in evaluations), total),
        "date_accuracy": _rate(sum(item.date_result.exact_match for item in evaluations), total),
        "merchant_accuracy": _rate(
            sum(item.merchant_result.normalized_match for item in evaluations),
            total,
        ),
        "amount_accuracy": _rate(
            sum(item.amount_result.exact_match for item in evaluations),
            total,
        ),
        "payment_method_accuracy": _rate(
            sum(item.payment_method_result.exact_match for item in evaluations),
            total,
        ),
        "failure_reason_counts": _count_failure_reasons(evaluations),
        "split_counts": _count_attr(evaluations, "split"),
        "difficulty_counts": _count_attr(evaluations, "difficulty"),
    }
    return summary


def _count_failure_reasons(evaluations: list[ReceiptEvaluation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        for reason in evaluation.failure_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _count_attr(evaluations: list[ReceiptEvaluation], attr_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        value = getattr(evaluation, attr_name) or "unspecified"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _classify_failure_reasons(evaluation: ReceiptEvaluation) -> list[str]:
    if evaluation.overall_pass:
        return []

    reasons: list[str] = []
    merchant_prediction = evaluation.merchant_result.predicted or ""
    raw_text = evaluation.raw_text_preview or ""
    notes = evaluation.notes or ""

    if not evaluation.merchant_result.normalized_match:
        if _contains_any(merchant_prediction, ("영수증", "조회", "발행")):
            reasons.append("merchant_header_noise")
        if _contains_any(
            merchant_prediction + "\n" + raw_text,
            ("서울", "경기", "광진구", "자양로"),
        ):
            reasons.append("merchant_address_noise")
        if not any(reason.startswith("merchant_") for reason in reasons):
            reasons.append("merchant_parse")

    if not evaluation.amount_result.exact_match:
        predicted_amount = _safe_decimal(evaluation.amount_result.predicted)
        expected_amount = _safe_decimal(evaluation.amount_result.expected)
        if _amount_matches_business_number(raw_text, evaluation.amount_result.predicted):
            reasons.append("amount_business_number")
        elif (
            predicted_amount is not None
            and expected_amount is not None
            and predicted_amount < expected_amount
            and expected_amount - predicted_amount >= Decimal("1000")
        ):
            reasons.append("amount_line_item_selected")
        else:
            reasons.append("amount_parse")

    if not evaluation.payment_method_result.exact_match:
        if evaluation.payment_method_result.predicted == PaymentMethod.ONLINE_PAYMENT.value:
            reasons.append("payment_false_online")
        else:
            reasons.append("payment_parse")

    if not evaluation.date_result.exact_match:
        reasons.append("date_parse")

    mismatch_count = sum(
        (
            not evaluation.date_result.exact_match,
            not evaluation.merchant_result.normalized_match,
            not evaluation.amount_result.exact_match,
            not evaluation.payment_method_result.exact_match,
        )
    )
    if mismatch_count >= 2 and _contains_any(notes, ("구겨", "어두", "흐림", "그림자")):
        reasons.append("image_quality_low")

    return reasons


def _serialize_evaluation(evaluation: ReceiptEvaluation) -> dict[str, Any]:
    payload = asdict(evaluation)
    payload["evidence_type"] = evaluation.evidence_type.value
    return payload


def _print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    _console_print("Receipt OCR Benchmark")
    _console_print(f"- samples: {summary['total_samples']}")
    _console_print(
        "- accuracy:"
        f" overall={summary['overall_pass_rate']:.2%},"
        f" date={summary['date_accuracy']:.2%},"
        f" merchant={summary['merchant_accuracy']:.2%},"
        f" amount={summary['amount_accuracy']:.2%},"
        f" payment={summary['payment_method_accuracy']:.2%}"
    )
    if summary["failure_reason_counts"]:
        _console_print(
            "- failure reasons: "
            + ", ".join(
                f"{reason}={count}" for reason, count in summary["failure_reason_counts"].items()
            )
        )
    _console_print()

    for evaluation in report["evaluations"]:
        status_label = "PASS" if evaluation["overall_pass"] else "FAIL"
        _console_print(f"[{status_label}] {evaluation['file_name']}")
        _console_print(
            "  date: "
            f"{evaluation['date_result']['predicted']} / "
            f"{evaluation['date_result']['expected']}"
        )
        _console_print(
            "  merchant: "
            f"{evaluation['merchant_result']['predicted']} / "
            f"{evaluation['merchant_result']['expected']}"
        )
        _console_print(
            "  amount: "
            f"{evaluation['amount_result']['predicted']} / "
            f"{evaluation['amount_result']['expected']}"
        )
        _console_print(
            "  payment: "
            f"{evaluation['payment_method_result']['predicted']} / "
            f"{evaluation['payment_method_result']['expected']}"
        )
        selected_attempt = evaluation["extraction_payload"].get("selected_attempt")
        if selected_attempt:
            _console_print(
                "  selected: "
                f"variant={selected_attempt['preprocessing']}, "
                f"avg_conf={selected_attempt['average_confidence']}, "
                f"score={selected_attempt['score']}"
            )
        if evaluation["failure_reasons"]:
            _console_print(f"  reasons: {', '.join(evaluation['failure_reasons'])}")
        _console_print()


def _rate(hit_count: int, total: int) -> float:
    if total == 0:
        return 0.0
    return hit_count / total


def _parse_date(value: str | None) -> date | None:
    cleaned_value = _clean_optional_text(value)
    if cleaned_value is None:
        return None
    return date.fromisoformat(cleaned_value)


def _parse_decimal(value: str | None) -> Decimal | None:
    cleaned_value = _clean_optional_text(value)
    if cleaned_value is None:
        return None
    try:
        return Decimal(cleaned_value)
    except InvalidOperation as exc:
        raise ValueError(f"금액 값이 올바르지 않습니다: {value}") from exc


def _parse_payment_method(value: str | None) -> PaymentMethod | None:
    cleaned_value = _clean_optional_text(value)
    if cleaned_value is None:
        return None

    aliases = {
        "naver_pay": PaymentMethod.ONLINE_PAYMENT,
        "naverpay": PaymentMethod.ONLINE_PAYMENT,
        "kakao_pay": PaymentMethod.ONLINE_PAYMENT,
        "kakaopay": PaymentMethod.ONLINE_PAYMENT,
        "toss_pay": PaymentMethod.ONLINE_PAYMENT,
        "tosspay": PaymentMethod.ONLINE_PAYMENT,
    }
    alias_match = aliases.get(cleaned_value.lower())
    if alias_match is not None:
        return alias_match

    try:
        return PaymentMethod(cleaned_value)
    except ValueError as exc:
        raise ValueError(f"payment method 값이 올바르지 않습니다: {value}") from exc


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned_value = value.strip()
    return cleaned_value or None


def _normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(character for character in normalized if character.isalnum())


def _contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _safe_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _amount_matches_business_number(text: str, value: str | None) -> bool:
    if not value:
        return False
    normalized_value = re.sub(r"\D", "", value)
    if not normalized_value:
        return False
    for match in re.finditer(r"\d{2,3}\s*-\s*\d{2}\s*-\s*\d{4,5}", text):
        business_number = re.sub(r"\D", "", match.group(0))
        if normalized_value in business_number:
            return True
    return False


def _console_print(message: str = "") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_message)


if __name__ == "__main__":
    raise SystemExit(main())
