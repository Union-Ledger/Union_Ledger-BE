"""영수증 파싱 중간 과정 추적 도구 (발표/검수용).

OCR 라인 → 단계별 파싱 산출물을 콘솔과 JSON 파일로 남긴다. 입력은 둘 중 하나:

  1) 실제 이미지: PaddleOCR을 돌려 raw 라인을 얻은 뒤 파싱
       python -m union_ledger.tools.receipt_parse_trace --image samples/receipts/physical/goose.jpg

  2) OCR 라인 JSON 픽스처: 이미 추출해 둔 라인으로 파싱만 재현(의존성 불필요)
       python -m union_ledger.tools.receipt_parse_trace --lines samples/receipts/goose_lines.json

산출물:
  <out>/stage0_raw_ocr_lines.json     PaddleOCR 원본 라인 (텍스트+신뢰도)
  <out>/stage1_amount_tokens.json     금액 토큰 추출/정규화
  <out>/stage2_classified_lines.json  라인 분류 (메타/세금/결제/합계/항목)
  <out>/stage3_candidates.json        의미 라벨별 금액 후보
  <out>/stage4_decision.json          최종 결제액 결정 + 근거
  <out>/trace.json                    전체 묶음
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# src 경로 보정 — 패키지 외부에서 직접 실행해도 import 되도록.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from union_ledger.services.receipt_payment_parser import (  # noqa: E402
    ParseTrace,
    parse_payment_amount,
)


def _load_lines_from_fixture(path: Path) -> list[dict[str, Any]]:
    """OCR 라인 JSON 픽스처를 읽는다. ``[{"text", "confidence"}, ...]`` 형태."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "lines" in data:
        data = data["lines"]
    if not isinstance(data, list):
        raise ValueError("픽스처는 라인 리스트이거나 {'lines': [...]} 형태여야 합니다.")
    return data


def _load_lines_from_image(image_path: Path) -> list[dict[str, Any]]:
    """실제 이미지에 PaddleOCR을 돌려 라인 단위 산출물을 얻는다.

    프로덕션과 동일한 ``EvidenceExtractionService`` 의 로컬 PaddleOCR 경로를
    그대로 사용하므로, 여기서 나오는 raw 라인은 실제 시스템 산출물과 같다.
    """

    from union_ledger.core.config import get_settings
    from union_ledger.services.evidence_extraction import EvidenceExtractionService

    service = EvidenceExtractionService(get_settings())
    # 로컬 PaddleOCR 라인 추출 (text, confidence, box 포함).
    _, _, _, ocr_lines = service._run_local_paddle_ocr(image_path)  # noqa: SLF001
    return [{"text": line.text, "confidence": line.confidence} for line in ocr_lines]


def _write_artifacts(trace: ParseTrace, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "stage0_raw_ocr_lines.json": trace.stage0_raw_lines,
        "stage1_amount_tokens.json": trace.stage1_amount_tokens,
        "stage2_classified_lines.json": trace.stage2_classified_lines,
        "stage3_candidates.json": trace.stage3_candidates,
        "stage4_decision.json": trace.stage4_decision,
        "trace.json": trace.to_dict(),
    }
    for name, payload in artifacts.items():
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _print_trace(trace: ParseTrace) -> None:
    label_names = {
        "payment": "결제",
        "total": "합계",
        "subtotal": "세금/공급가",
        "item": "항목",
        "meta": "메타",
    }

    print("\n[Stage 0] PaddleOCR raw 라인")
    for line in trace.stage0_raw_lines:
        print(f"  #{line['index']:>2}  conf={line['confidence']:.2f}  {line['text']}")

    print("\n[Stage 1] 금액 토큰 추출 → 정규화")
    for token in trace.stage1_amount_tokens:
        print(f"  line#{token['line_index']:>2}  '{token['raw']}'  →  {token['normalized']:,}")

    print("\n[Stage 2] 라인 분류")
    for line in trace.stage2_classified_lines:
        if line["label"] == "meta" and not line["amounts"]:
            continue
        tag = label_names.get(line["label"], line["label"])
        keyword = f" ({line['matched_keyword']})" if line["matched_keyword"] else ""
        amounts = ", ".join(f"{amount['value']:,}" for amount in line["amounts"]) or "-"
        print(f"  #{line['index']:>2}  [{tag}{keyword}]  금액: {amounts}  | {line['text']}")

    print("\n[Stage 3] 의미 라벨별 금액 후보")
    for category, items in trace.stage3_candidates.items():
        name = label_names.get(category.replace("_excluded", ""), category)
        values = ", ".join(f"{item['value']:,}" for item in items) or "-"
        print(f"  {name:>10}: {values}")

    print("\n[Stage 4] 최종 결제액 결정")
    print(f"  규칙: {trace.stage4_decision['rule']}")
    for reason in trace.stage4_decision["reasoning"]:
        print(f"   - {reason}")

    print("\n" + "=" * 48)
    if trace.final_amount is not None:
        print(f"  최종 추출 결제액 = {trace.final_amount:,} 원")
    else:
        print("  최종 추출 결제액 = (없음)")
    print("=" * 48)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="영수증 파싱 중간 과정 추적")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="영수증 이미지 (PaddleOCR 실행)")
    source.add_argument("--lines", type=Path, help="OCR 라인 JSON 픽스처")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("samples/receipts/reports/parse_trace"),
        help="단계별 산출물 저장 폴더",
    )
    args = parser.parse_args(argv)

    if args.image is not None:
        lines = _load_lines_from_image(args.image)
    else:
        lines = _load_lines_from_fixture(args.lines)

    trace = parse_payment_amount(lines)
    _print_trace(trace)
    _write_artifacts(trace, args.out)
    print(f"\n산출물 저장: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
