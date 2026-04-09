from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from union_ledger.models.enums import EvidenceType
from union_ledger.services.evidence_extraction import (
    EvidenceExtractionService,
    OCRAttempt,
    build_ocr_attempt,
    merge_ocr_attempt_fields,
    select_best_ocr_attempt,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a debug bundle for one receipt sample with OCR attempts."
    )
    parser.add_argument("--sample", required=True, help="Path to the receipt image or PDF sample.")
    parser.add_argument(
        "--evidence-type",
        default=EvidenceType.PHYSICAL_RECEIPT.value,
        choices=[item.value for item in EvidenceType],
    )
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    sample_path = Path(args.sample).resolve()
    evidence_type = EvidenceType(args.evidence_type)
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = (sample_path.parents[1] / "reports" / "debug" / sample_path.stem).resolve()

    render_debug_bundle(sample_path=sample_path, evidence_type=evidence_type, output_dir=output_dir)
    print(f"Debug bundle generated: {output_dir}")
    return 0


def render_debug_bundle(
    *,
    sample_path: Path,
    evidence_type: EvidenceType,
    output_dir: Path,
) -> None:
    service = EvidenceExtractionService()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_copy_name = f"source{sample_path.suffix.lower()}"
    shutil.copy2(sample_path, output_dir / source_copy_name)

    attempts: list[OCRAttempt] = []
    variant_labels: list[str] = []

    with tempfile.TemporaryDirectory() as temp_dir:
        variants = service._prepare_image_variants(sample_path, Path(temp_dir), evidence_type)
        for variant in variants:
            destination = output_dir / f"{variant.label}.png"
            shutil.copy2(variant.path, destination)
            variant_labels.append(variant.label)
            raw_text, average_confidence, line_count = service._run_paddle_ocr(variant.path)
            attempt = build_ocr_attempt(
                raw_text,
                evidence_type,
                preprocessing=variant.label,
                average_confidence=average_confidence,
                line_count=line_count,
            )
            if attempt.raw_text:
                attempts.append(attempt)

    if not attempts:
        raise RuntimeError("OCR 시도 결과가 비어 있습니다.")

    attempts.sort(key=lambda item: (item.score, item.average_confidence), reverse=True)
    best_attempt = select_best_ocr_attempt(attempts)
    merged_fields = merge_ocr_attempt_fields(attempts, evidence_type)

    payload = {
        "summary": _build_summary(
            sample_path=sample_path,
            evidence_type=evidence_type,
            variant_labels=variant_labels,
            attempts=attempts,
            best_attempt=best_attempt,
            merged_fields=merged_fields,
        ),
        "attempts": [_serialize_attempt(attempt) for attempt in attempts],
    }

    (output_dir / "debug.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "debug.html").write_text(
        _render_debug_html(payload, output_dir, source_copy_name),
        encoding="utf-8",
    )


def _build_summary(
    *,
    sample_path: Path,
    evidence_type: EvidenceType,
    variant_labels: list[str],
    attempts: list[OCRAttempt],
    best_attempt: OCRAttempt,
    merged_fields: Any,
) -> dict[str, Any]:
    return {
        "sample": str(sample_path),
        "evidence_type": evidence_type.value,
        "variant_count": len(variant_labels),
        "variant_labels": variant_labels,
        "attempt_count": len(attempts),
        "best_attempt": {
            "preprocessing": best_attempt.preprocessing,
            "score": best_attempt.score,
            "average_confidence": best_attempt.average_confidence,
            "line_count": best_attempt.line_count,
        },
        "merged_fields": {
            "date": (
                merged_fields.evidence_date.isoformat()
                if merged_fields.evidence_date
                else None
            ),
            "merchant": merged_fields.merchant_name,
            "amount": str(merged_fields.amount) if merged_fields.amount is not None else None,
            "payment": (
                merged_fields.payment_method.value
                if merged_fields.payment_method is not None
                else None
            ),
        },
    }


def _serialize_attempt(attempt: OCRAttempt) -> dict[str, Any]:
    return {
        "preprocessing": attempt.preprocessing,
        "score": attempt.score,
        "average_confidence": attempt.average_confidence,
        "line_count": attempt.line_count,
        "fields": {
            "date": (
                attempt.parsed.evidence_date.isoformat()
                if attempt.parsed.evidence_date
                else None
            ),
            "merchant": attempt.parsed.merchant_name,
            "amount": str(attempt.parsed.amount) if attempt.parsed.amount is not None else None,
            "payment": (
                attempt.parsed.payment_method.value
                if attempt.parsed.payment_method
                else None
            ),
        },
        "text_preview": attempt.raw_text[:600],
        "raw_text": attempt.raw_text,
    }


def _render_debug_html(
    debug_payload: dict[str, Any],
    output_dir: Path,
    source_copy_name: str,
) -> str:
    summary = debug_payload["summary"]
    attempts = debug_payload["attempts"]
    variant_cards = [_render_variant_card(output_dir, source_copy_name, "source")]
    variant_cards.extend(
        _render_variant_card(output_dir, f"{label}.png", label)
        for label in summary["variant_labels"]
    )
    attempt_cards = [
        _render_attempt_card(index=index, attempt=attempt)
        for index, attempt in enumerate(attempts, start=1)
    ]
    summary_cards = [
        ("전처리 이미지 수", str(summary["variant_count"])),
        ("총 OCR 시도 수", str(summary["attempt_count"])),
        ("최종 선택 variant", str(summary["best_attempt"]["preprocessing"])),
        ("최종 선택 avg_conf", str(summary["best_attempt"]["average_confidence"])),
        ("최종 날짜", str(summary["merged_fields"]["date"] or "-")),
        ("최종 상호명", str(summary["merged_fields"]["merchant"] or "-")),
        ("최종 금액", str(summary["merged_fields"]["amount"] or "-")),
        ("최종 결제수단", str(summary["merged_fields"]["payment"] or "-")),
    ]
    rendered_summary_cards = "".join(
        (
            '<div class="summary-card">'
            f'<span>{html.escape(label)}</span>'
            f'<strong>{html.escape(value)}</strong>'
            "</div>"
        )
        for label, value in summary_cards
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OCR Debug Bundle</title>
  <style>
    body {{
      margin: 0;
      padding: 28px;
      font-family: "Pretendard", "Noto Sans KR", sans-serif;
      background: #f6f1e8;
      color: #1f1b17;
    }}
    .page {{ max-width: 1400px; margin: 0 auto; }}
    .hero {{
      background: #fffaf1;
      border: 1px solid #e4d4ba;
      border-radius: 24px;
      padding: 24px;
      box-shadow: 0 14px 30px rgba(58, 38, 18, 0.08);
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 34px; }}
    .hero p {{ margin: 0; line-height: 1.6; color: #6d6258; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .summary-card {{
      background: #fff;
      border: 1px solid #eadcc4;
      border-radius: 16px;
      padding: 14px;
    }}
    .summary-card span {{
      display: block;
      font-size: 12px;
      color: #7a6f64;
      margin-bottom: 6px;
    }}
    .summary-card strong {{ font-size: 24px; line-height: 1.3; }}
    h2 {{ margin: 28px 0 12px; font-size: 22px; }}
    .variants {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }}
    .variant-card,
    .attempt-card {{
      background: #fff;
      border: 1px solid #eadcc4;
      border-radius: 18px;
      padding: 14px;
      box-shadow: 0 10px 24px rgba(58, 38, 18, 0.05);
    }}
    .variant-card h3 {{ margin: 0 0 12px; font-size: 16px; }}
    .variant-card img {{
      width: 100%;
      border-radius: 12px;
      background: #faf6ef;
      max-height: 360px;
      object-fit: contain;
    }}
    .attempts {{ display: grid; gap: 14px; }}
    .attempt-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 15px;
      margin-bottom: 12px;
    }}
    .fields-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .field {{
      background: #fffaf1;
      border: 1px solid #f0e4cf;
      border-radius: 12px;
      padding: 10px;
    }}
    .field span {{
      display: block;
      font-size: 11px;
      color: #7a6f64;
      margin-bottom: 6px;
      text-transform: uppercase;
    }}
    .field strong {{ font-size: 14px; line-height: 1.4; }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #fbf7ef;
      border: 1px solid #f0e4cf;
      border-radius: 12px;
      padding: 12px;
      font-family: "D2Coding", "Consolas", monospace;
      font-size: 12px;
      line-height: 1.55;
      max-height: 280px;
      overflow: auto;
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <h1>OCR Debug Bundle</h1>
      <p>
        명세서 v4 기준 전처리 variant별 OCR 시도를 한 화면에 정리한 디버그 페이지입니다.
        어떤 variant가 상호명, 날짜, 금액에 더 유리한지 눈으로 비교할 수 있습니다.
      </p>
      <div class="summary">{rendered_summary_cards}</div>
    </section>

    <h2>전처리 이미지</h2>
    <section class="variants">{''.join(variant_cards)}</section>

    <h2>OCR 시도 결과</h2>
    <section class="attempts">{''.join(attempt_cards)}</section>
  </div>
</body>
</html>"""


def _render_variant_card(output_dir: Path, file_name: str, label: str) -> str:
    relative_path = os.path.relpath(output_dir / file_name, output_dir).replace("\\", "/")
    return (
        '<div class="variant-card">'
        f"<h3>{html.escape(label)}</h3>"
        f'<img src="{html.escape(relative_path)}" alt="{html.escape(label)}" />'
        "</div>"
    )


def _render_attempt_card(*, index: int, attempt: dict[str, Any]) -> str:
    fields = attempt["fields"]
    field_cards = "".join(
        (
            '<div class="field">'
            f"<span>{html.escape(label)}</span>"
            f"<strong>{html.escape(str(value or '-'))}</strong>"
            "</div>"
        )
        for label, value in (
            ("Date", fields["date"]),
            ("Merchant", fields["merchant"]),
            ("Amount", fields["amount"]),
            ("Payment", fields["payment"]),
        )
    )

    return f"""
    <article class="attempt-card">
      <div class="attempt-head">
        <div><strong>#{index} {html.escape(attempt['preprocessing'])}</strong></div>
        <div>score {attempt['score']} · avg_conf {attempt['average_confidence']}</div>
      </div>
      <div class="fields-grid">{field_cards}</div>
      <pre>{html.escape(attempt['text_preview'])}</pre>
    </article>
    """


if __name__ == "__main__":
    raise SystemExit(main())
