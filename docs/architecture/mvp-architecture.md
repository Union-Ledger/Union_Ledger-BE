# Union Ledger MVP Architecture

## Goal

기능 명세서 기준 MVP는 다음 흐름을 끊김 없이 지원하는 것이 핵심입니다.

1. 재정담당자가 조직별 결산안 템플릿을 등록한다.
2. 증빙을 업로드하고 OCR 또는 PDF 텍스트 추출을 수행한다.
3. 은행 거래내역을 업로드하고 날짜+금액 기준으로 자동 대조한다.
4. 대조 결과를 수정한 뒤 결산안 산출물을 생성하고 감사 제출한다.
5. 감사위원이 항목별 코멘트와 전체 승인/반려를 수행한다.
6. 승인된 결산안만 일반 학우에게 공개한다.

## Technical Direction

- API: FastAPI
- Persistence: PostgreSQL + SQLAlchemy 2.0
- Migration: Alembic
- File handling: 로컬 파일 경로 기준으로 시작하고, 이후 S3 호환 스토리지로 전환 가능하게 설계
- OCR pipeline: 현재는 API/도메인 경계만 정의하고, 실제 OCR 엔진과 비동기 워커는 후속 구현 대상으로 남김

## Initial Domain Model

- `User`: 대학 이메일 계정
- `Organization`: 단과대/학과 학생회 단위 조직
- `OrganizationMembership`: 조직별 역할 연결
- `Invitation`: 재정담당자/감사위원 초대 및 권한 이전 링크
- `SettlementTemplate`: 결산안 엑셀 원본과 셀 매핑
- `Settlement`: 학기별 결산안 본체
- `Evidence`: 증빙 원본과 추출 데이터
- `BankStatementUpload`: 거래내역 업로드 이력
- `BankTransaction`: 거래내역 개별 레코드
- `ReconciliationResult`: 증빙-거래내역 매칭 결과
- `AuditComment`: 항목별 감사 의견
- `SettlementArtifact`: 생성된 결산안 엑셀/증빙 PDF
- `Notification`: 인앱 알림

## Assumptions Locked In This Bootstrap

- 조직 계층은 초기에는 단일 `organizations` 테이블에 `college_name`, `department_name`, `name`으로 표현합니다.
- 역할은 `student`, `treasurer`, `auditor`, `admin` 네 가지로 시작합니다.
- 증빙은 1건이 1개 지출 건으로 이어진다는 명세서 기준 가정을 우선 적용했습니다.
- 거래내역 대조는 `날짜 + 금액` 기준의 1:1 자동 매칭을 기본값으로 둡니다.
- 감사 승인 시 일반 학우 공개 대상이 됩니다.

## Open Issues For Next Iteration

- 이메일 인증을 자체 OTP로 할지, 메일 링크 인증으로 할지 확정 필요
- OCR/텍스트 추출을 동기 처리할지, 잡 큐 기반 비동기 처리로 갈지 확정 필요
- 공개 증빙에 대한 마스킹 정책 필요
- 반려 후 재제출의 수정 이력 단위를 엔티티별 diff로 남길지 확정 필요
- 증빙 확정 후 별도 `line_items` 테이블을 둘지, `evidences`를 장부 엔트리로 직접 활용할지 재검토 필요

