from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a visual HTML report from a receipt OCR benchmark JSON file."
    )
    parser.add_argument("--report-json", default="samples/receipts/reports/latest.json")
    parser.add_argument("--output", default="samples/receipts/reports/latest.html")
    args = parser.parse_args()

    report_path = Path(args.report_json).resolve()
    output_path = Path(args.output).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report_html(report, output_path=output_path), encoding="utf-8")
    print(f"HTML report generated: {output_path}")
    return 0


def render_report_html(report: dict[str, Any], *, output_path: Path) -> str:
    summary = report["summary"]
    samples_root = Path(report["samples_root"])
    cards = "\n".join(
        _render_evaluation_card(evaluation, samples_root=samples_root, output_path=output_path)
        for evaluation in report["evaluations"]
    )
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_cards = "\n".join(
        _render_summary_card(label, value)
        for label, value in (
            ("샘플 수", str(summary["total_samples"])),
            ("전체 통과율", f"{summary['overall_pass_rate']:.0%}"),
            ("날짜 정확도", f"{summary['date_accuracy']:.0%}"),
            ("상호명 정확도", f"{summary['merchant_accuracy']:.0%}"),
            ("금액 정확도", f"{summary['amount_accuracy']:.0%}"),
            ("결제수단 정확도", f"{summary['payment_method_accuracy']:.0%}"),
        )
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Receipt OCR Report</title>
  <style>
    :root {{
      --bg: #f5efe5;
      --panel: #fffdf8;
      --panel-strong: #fff4dc;
      --line: #e7d8bb;
      --text: #241d16;
      --muted: #75675a;
      --ok: #1f7a48;
      --fail: #aa3434;
      --shadow: 0 18px 40px rgba(69, 47, 26, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 28px;
      font-family: "Pretendard", "Noto Sans KR", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(255, 212, 118, 0.24), transparent 25%),
        linear-gradient(180deg, #fbf8f1 0%, var(--bg) 100%);
      color: var(--text);
    }}
    .page {{ max-width: 1440px; margin: 0 auto; }}
    .hero {{
      background: linear-gradient(135deg, #fffdf8, #fff2d6);
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 28px 32px;
      box-shadow: var(--shadow);
      margin-bottom: 24px;
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 34px; }}
    .hero p {{ margin: 0; color: var(--muted); line-height: 1.6; }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px;
      margin-top: 20px;
    }}
    .summary-card {{
      background: rgba(255, 255, 255, 0.82);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
    }}
    .summary-card .label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .summary-card .value {{ font-size: 28px; font-weight: 800; }}
    .section-title {{ margin: 24px 0 14px; font-size: 22px; font-weight: 800; }}
    .cards {{ display: grid; gap: 18px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 22px;
      background: var(--panel-strong);
      border-bottom: 1px solid var(--line);
    }}
    .card-title {{ margin: 0; font-size: 22px; font-weight: 800; }}
    .card-subtitle {{ margin-top: 4px; color: var(--muted); font-size: 14px; }}
    .status {{
      padding: 8px 14px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
      height: fit-content;
    }}
    .status.pass {{ background: rgba(31, 122, 72, 0.12); color: var(--ok); }}
    .status.fail {{ background: rgba(170, 52, 52, 0.12); color: var(--fail); }}
    .card-body {{
      display: grid;
      grid-template-columns: minmax(280px, 380px) 1fr;
      gap: 18px;
      padding: 20px 22px 24px;
    }}
    .image-panel {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px;
    }}
    .image-panel img {{
      display: block;
      width: 100%;
      max-height: 520px;
      object-fit: contain;
      border-radius: 12px;
      background: #faf7f0;
    }}
    .chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .chip {{
      border-radius: 999px;
      padding: 7px 11px;
      border: 1px solid var(--line);
      font-size: 12px;
      background: #fffaf0;
      color: var(--muted);
    }}
    .panel-grid {{ display: grid; gap: 14px; }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: #fffefb;
    }}
    .panel h3 {{ margin: 0 0 12px; font-size: 16px; }}
    .field-table {{
      width: 100%;
      border-collapse: collapse;
    }}
    .field-table th,
    .field-table td {{
      text-align: left;
      padding: 10px 8px;
      border-top: 1px solid #f0e5cf;
      vertical-align: top;
      line-height: 1.5;
      font-size: 14px;
    }}
    .field-table tr:first-child th,
    .field-table tr:first-child td {{ border-top: none; }}
    .field-table th {{ width: 128px; color: var(--muted); font-weight: 600; }}
    .prediction-ok {{ color: var(--ok); font-weight: 700; }}
    .prediction-fail {{ color: var(--fail); font-weight: 700; }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #fbf7ef;
      border-radius: 14px;
      border: 1px solid #f0e5cf;
      padding: 14px;
      font-family: "D2Coding", "Consolas", monospace;
      font-size: 13px;
      line-height: 1.55;
      max-height: 280px;
      overflow: auto;
    }}
    .footer-note {{ margin-top: 18px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 980px) {{
      body {{ padding: 16px; }}
      .hero h1 {{ font-size: 28px; }}
      .card-body {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <h1>Receipt OCR Report</h1>
      <p>
        PaddleOCR 기반 OCR 결과를 샘플별로 시각화한 리포트입니다.
        각 카드에서 원본 이미지, 최종 추출값, 선택된 전처리 variant,
        필드 병합 결과를 함께 확인할 수 있습니다.
      </p>
      <div class="summary-grid">{summary_cards}</div>
      <div class="footer-note">Generated at {html.escape(generated_at)}</div>
    </section>

    <h2 class="section-title">Sample-by-Sample Results</h2>
    <section class="cards">{cards}</section>
  </div>
</body>
</html>
"""


def _render_summary_card(label: str, value: str) -> str:
    return (
        '<div class="summary-card">'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        "</div>"
    )


def _render_evaluation_card(
    evaluation: dict[str, Any],
    *,
    samples_root: Path,
    output_path: Path,
) -> str:
    sample_path = _resolve_sample_path(samples_root, evaluation["file_name"])
    image_relative_path = os.path.relpath(sample_path, output_path.parent).replace("\\", "/")
    extraction_payload = evaluation["extraction_payload"]
    selected_attempt = extraction_payload.get("selected_attempt", {})
    pipeline = extraction_payload.get("ocr_pipeline", {})
    field_fusion = extraction_payload.get("field_fusion", {})
    engine_label = html.escape(str(extraction_payload.get("engine", "-")))
    variant_label = html.escape(str(selected_attempt.get("preprocessing", "-")))
    confidence_label = html.escape(str(selected_attempt.get("average_confidence", "-")))
    line_count_label = html.escape(str(selected_attempt.get("line_count", "-")))
    image_src = html.escape(image_relative_path)
    image_alt = html.escape(evaluation["file_name"])
    stages_label = html.escape(", ".join(pipeline.get("stages", [])) or "-")
    selected_label = html.escape(str(selected_attempt.get("preprocessing", "-")))
    field_fusion_json = html.escape(json.dumps(field_fusion, ensure_ascii=False, indent=2))
    selected_fields_json = html.escape(
        json.dumps(selected_attempt.get("normalized_fields", {}), ensure_ascii=False, indent=2)
    )
    raw_text = html.escape(evaluation.get("raw_text_preview") or "-")

    return f"""
    <article class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title">{html.escape(evaluation['file_name'])}</h3>
          <div class="card-subtitle">
            유형: {html.escape(evaluation['evidence_type'])}
            · split: {html.escape(str(evaluation.get('split') or '-'))}
            · 난이도: {html.escape(str(evaluation.get('difficulty') or '-'))}
          </div>
        </div>
        <div class="status {'pass' if evaluation['overall_pass'] else 'fail'}">
          {'PASS' if evaluation['overall_pass'] else 'FAIL'}
        </div>
      </div>

      <div class="card-body">
        <div class="image-panel">
          <img src="{image_src}" alt="{image_alt}" />
          <div class="chip-row">
            <span class="chip">engine: {engine_label}</span>
            <span class="chip">variant: {variant_label}</span>
            <span class="chip">avg_conf: {confidence_label}</span>
            <span class="chip">lines: {line_count_label}</span>
          </div>
        </div>

        <div class="panel-grid">
          <section class="panel">
            <h3>최종 추출 결과</h3>
            {_render_result_table(evaluation)}
          </section>

          <section class="panel">
            <h3>파이프라인 요약</h3>
            <table class="field-table">
              <tr><th>stage</th><td>{stages_label}</td></tr>
              <tr><th>selected</th><td>{selected_label}</td></tr>
              <tr><th>field fusion</th><td><pre>{field_fusion_json}</pre></td></tr>
            </table>
          </section>

          <section class="panel">
            <h3>선택된 시도 결과</h3>
            <pre>{selected_fields_json}</pre>
          </section>

          <section class="panel">
            <h3>OCR 원문 텍스트</h3>
            <pre>{raw_text}</pre>
          </section>
        </div>
      </div>
    </article>
    """


def _render_result_table(evaluation: dict[str, Any]) -> str:
    rows = [
        ("날짜", evaluation["date_result"]),
        ("상호명", evaluation["merchant_result"]),
        ("금액", evaluation["amount_result"]),
        ("결제수단", evaluation["payment_method_result"]),
    ]
    rendered_rows: list[str] = []
    for label, result in rows:
        matched = result["normalized_match"] if label == "상호명" else result["exact_match"]
        rendered_rows.append(
            "<tr>"
            f"<th>{html.escape(label)}</th>"
            f"<td>{html.escape(str(result['expected'] or '-'))}</td>"
            f"<td class=\"{'prediction-ok' if matched else 'prediction-fail'}\">"
            f"{html.escape(str(result['predicted'] or '-'))}</td>"
            "</tr>"
        )

    return (
        "<table class=\"field-table\">"
        "<tr><th>항목</th><td>정답</td><td>예측</td></tr>"
        + "".join(rendered_rows)
        + "</table>"
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
