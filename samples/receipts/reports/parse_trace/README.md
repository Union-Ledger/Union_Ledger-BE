# 영수증 OCR 결제액 추출 — 중간 산출물

구스치킨 영수증(합계 854,500원 / 실제 카드결제 234,500원)을 OCR 라인부터
최종 결제액까지 파싱한 **단계별 중간 산출물**입니다. 각 단계가 독립 JSON
파일로 저장되어 있어 파싱 과정 전체를 추적할 수 있습니다.

## 재현 방법

```bash
# (A) 실제 이미지 → PaddleOCR 실행 후 파싱  ← 프로덕션과 동일 OCR 경로
python -m union_ledger.tools.receipt_parse_trace --image <영수증.jpg>

# (B) 추출해 둔 OCR 라인으로 파싱만 재현 (의존성 불필요)
python -m union_ledger.tools.receipt_parse_trace --lines samples/receipts/goose_lines.json
```

- 파서: `src/union_ledger/services/receipt_payment_parser.py`
- 추적 도구: `src/union_ledger/tools/receipt_parse_trace.py`

## 단계별 산출물

| 파일 | 단계 | 내용 |
|------|------|------|
| `stage0_raw_ocr_lines.json` | 0. OCR 인식 | PaddleOCR 원본 라인 27줄 (텍스트 + 신뢰도). 매장명·주소·날짜·항목·금액이 섞인 날것 상태 |
| `stage1_amount_tokens.json` | 1. 금액 토큰화 | 천 단위 콤마 정규식으로 금액 13개를 잡아 정수로 정규화. 수량(12·54·18)은 콤마가 없어 자연 분리 |
| `stage2_classified_lines.json` | 2. 라인 분류 | 각 라인을 `결제 / 합계 / 세금·공급가 / 항목 / 메타` 5종으로 라벨링 |
| `stage3_candidates.json` | 3. 후보 그룹화 | 의미 라벨별 금액 후보. 결제=234,500 · 합계=854,500 · 세금(776,824·77,676)은 분리·제외 |
| `stage4_decision.json` | 4. 결정 | `결제 > 합계 > 항목 최댓값` 우선순위 규칙 적용 → 234,500 채택, 근거 기록 |
| `trace.json` | 전체 | 0~4단계를 한 파일로 묶은 전체 추적본 |
| `pipeline_diagram.svg` | — | 파이프라인 다이어그램 (슬라이드용) |

## 핵심 설계: 왜 합계 854,500이 아니라 234,500인가

결산 시스템은 추출 금액을 **통장·카드 명세와 대조**한다. 따라서 정답은
실제로 카드에서 출금된 `카드결제 234,500원`이다. `합계금액 854,500원`은
분할결제·할인·포인트 차감 이전 값일 수 있어 후순위로 둔다.

이 규칙은 단일 영수증에 맞춘 것이 아니다 — `카드결제` 라벨이 없는 영수증
(`samples/receipts/cafe_lines.json`)에서는 자동으로 합계 금액으로 폴백한다.

```
영수증 A (구스치킨): 결제 라벨 있음 → 234,500 (카드결제)
영수증 B (카페):     결제 라벨 없음 → 14,500  (합계금액으로 폴백)
```
