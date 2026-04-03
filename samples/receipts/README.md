# Receipt Samples

실제 영수증 OCR 튜닝용 로컬 샘플 폴더입니다.

## 무엇을 준비하면 좋은가

- 우선 `실물 영수증` 이미지 10장 정도면 시작하기 충분합니다.
- 가능하면 아래 케이스를 섞어주세요.
  - 선명한 영수증 3장
  - 살짝 기울어진 영수증 2장
  - 조명이 어둡거나 그림자가 있는 영수증 2장
  - 길쭉한 영수증 2장
  - 글자가 작거나 흐린 영수증 1장

## 권장 폴더 구조

- `physical/`: 실물 영수증 이미지
- `bank_transfer/`: 거래명세서 PDF 또는 이미지
- `e_receipt/`: 전자영수증 스크린샷 또는 PDF

현재 우선순위는 `physical/`입니다.

## 파일명 규칙

예시:

- `physical/receipt_001.jpg`
- `physical/receipt_002.png`

## 정답 라벨 파일

`ground_truth.template.csv`를 복사해서 `ground_truth.local.csv`로 만든 뒤 채워주세요.

각 행에는 최소한 아래 값을 넣으면 됩니다.

- `file_name`
- `evidence_type`
- `expected_date`
- `expected_merchant_name`
- `expected_amount`
- `expected_payment_method`

## 벤치마크 실행

한글 영수증 OCR을 제대로 돌리려면 먼저 한국어 Tesseract 언어팩을 로컬에 준비하는 것이 좋습니다.

```powershell
.venv\Scripts\python.exe -m union_ledger.tools.setup_korean_ocr
```

아래 명령으로 샘플 전체를 한 번에 평가할 수 있습니다.

```powershell
.venv\Scripts\python.exe -m union_ledger.tools.receipt_benchmark
```

JSON 리포트까지 저장하려면:

```powershell
.venv\Scripts\python.exe -m union_ledger.tools.receipt_benchmark --output samples\receipts\reports\latest.json
```

## 개인정보 주의

- 카드번호, 승인번호, 전화번호, 주소 등은 가려도 됩니다.
- 상호명, 날짜, 금액 정도만 남아 있으면 OCR 튜닝에는 충분합니다.
- 이 폴더의 실제 샘플 파일은 `.gitignore`로 커밋되지 않게 설정되어 있습니다.
