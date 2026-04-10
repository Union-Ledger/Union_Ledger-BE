"""Benchmark OCR pipeline against ground truth CSV."""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from union_ledger.core.config import Settings
from union_ledger.models.enums import EvidenceType
from union_ledger.services.evidence_extraction import EvidenceExtractionService


GROUND_TRUTH_PATH = Path("samples/receipts/ground_truth_local.csv")
SAMPLES_ROOT = Path("samples/receipts/physical")
REPORT_PATH = Path("samples/receipts/reports/benchmark_v6_fixes.json")


def _normalize_merchant(name: str | None) -> str:
    if not name:
        return ""
    import re
    return re.sub(r"[\s·\-_.,()（）]", "", name).lower()


def _merchant_similarity(expected: str, predicted: str) -> float:
    a = _normalize_merchant(expected)
    b = _normalize_merchant(predicted)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    common = sum(1 for c in a if c in b)
    return common / max(len(a), len(b))


def _merchant_match(expected: str | None, predicted: str | None) -> bool:
    if not expected or not predicted:
        return expected == predicted
    return _merchant_similarity(expected, predicted) >= 0.6


def main() -> None:
    ocr_mode = "modal"
    print(f"OCR mode: {ocr_mode}")

    settings = Settings(ocr_mode=ocr_mode)
    service = EvidenceExtractionService(settings)

    rows = []
    with open(GROUND_TRUTH_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Ground truth: {len(rows)} samples")
    evaluations = []
    total_pass = 0
    field_correct = {"date": 0, "merchant": 0, "amount": 0, "payment": 0}

    for i, row in enumerate(rows):
        file_name = row["file_name"]
        image_path = SAMPLES_ROOT / file_name
        if not image_path.exists():
            print(f"  [{i+1}/{len(rows)}] SKIP {file_name} — not found")
            continue

        evidence_type = EvidenceType(row["evidence_type"])
        expected_date = row.get("expected_date", "")
        expected_merchant = row.get("expected_merchant_name", "")
        expected_amount = row.get("expected_amount", "")
        expected_payment = row.get("expected_payment_method", "")

        t0 = time.time()
        try:
            result = service.extract_file(image_path, evidence_type)
        except Exception as e:
            print(f"  [{i+1}/{len(rows)}] ERROR {file_name}: {e}")
            evaluations.append({"file_name": file_name, "error": str(e)})
            continue
        elapsed = time.time() - t0

        pred_date = result.evidence_date.isoformat() if result.evidence_date else ""
        pred_merchant = result.merchant_name or ""
        pred_amount = str(result.amount) if result.amount is not None else ""
        pred_payment = result.payment_method.value if result.payment_method else ""

        date_ok = pred_date == expected_date
        merchant_ok = _merchant_match(expected_merchant, pred_merchant)
        try:
            amount_ok = Decimal(pred_amount) == Decimal(expected_amount) if expected_amount and pred_amount else pred_amount == expected_amount
        except InvalidOperation:
            amount_ok = False
        payment_ok = pred_payment == expected_payment

        all_ok = date_ok and merchant_ok and amount_ok and payment_ok
        if all_ok:
            total_pass += 1
        if date_ok:
            field_correct["date"] += 1
        if merchant_ok:
            field_correct["merchant"] += 1
        if amount_ok:
            field_correct["amount"] += 1
        if payment_ok:
            field_correct["payment"] += 1

        status = "PASS" if all_ok else "FAIL"
        misses = []
        if not date_ok:
            misses.append(f"date({expected_date}→{pred_date})")
        if not merchant_ok:
            misses.append(f"merchant({expected_merchant}→{pred_merchant})")
        if not amount_ok:
            misses.append(f"amount({expected_amount}→{pred_amount})")
        if not payment_ok:
            misses.append(f"payment({expected_payment}→{pred_payment})")

        miss_str = " | ".join(misses) if misses else ""
        print(f"  [{i+1}/{len(rows)}] {status} {file_name} ({elapsed:.1f}s) {miss_str}")

        evaluations.append({
            "file_name": file_name,
            "evidence_type": row["evidence_type"],
            "notes": row.get("notes", ""),
            "overall_pass": all_ok,
            "elapsed_seconds": round(elapsed, 2),
            "date_result": {"expected": expected_date, "predicted": pred_date, "match": date_ok},
            "merchant_result": {"expected": expected_merchant, "predicted": pred_merchant, "match": merchant_ok, "similarity": round(_merchant_similarity(expected_merchant, pred_merchant), 3)},
            "amount_result": {"expected": expected_amount, "predicted": pred_amount, "match": amount_ok},
            "payment_method_result": {"expected": expected_payment, "predicted": pred_payment, "match": payment_ok},
        })

    n = len(rows)
    print(f"\n=== Results: {total_pass}/{n} pass ({total_pass/n*100:.1f}%) ===")
    print(f"  Date:     {field_correct['date']}/{n} ({field_correct['date']/n*100:.1f}%)")
    print(f"  Merchant: {field_correct['merchant']}/{n} ({field_correct['merchant']/n*100:.1f}%)")
    print(f"  Amount:   {field_correct['amount']}/{n} ({field_correct['amount']/n*100:.1f}%)")
    print(f"  Payment:  {field_correct['payment']}/{n} ({field_correct['payment']/n*100:.1f}%)")

    report = {
        "ground_truth_path": str(GROUND_TRUTH_PATH),
        "samples_root": str(SAMPLES_ROOT),
        "ocr_mode": ocr_mode,
        "summary": {
            "total_samples": n,
            "overall_pass_count": total_pass,
            "overall_pass_rate": total_pass / n if n else 0,
            "date_accuracy": field_correct["date"] / n if n else 0,
            "merchant_accuracy": field_correct["merchant"] / n if n else 0,
            "amount_accuracy": field_correct["amount"] / n if n else 0,
            "payment_method_accuracy": field_correct["payment"] / n if n else 0,
        },
        "evaluations": evaluations,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport saved to {REPORT_PATH}")


if __name__ == "__main__":
    main()
