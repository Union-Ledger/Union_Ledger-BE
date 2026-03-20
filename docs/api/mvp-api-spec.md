# Union Ledger MVP API Spec

## Base

- Base URL: `/api/v1`
- Auth: 추후 JWT 또는 세션 기반 인증 추가 예정
- File upload: `multipart/form-data`
- Response envelope: MVP에서는 단순 JSON 응답을 우선 적용하고, 운영 단계에서 공통 envelope 여부를 재검토

## Endpoint Groups

### Auth / Identity

- `POST /auth/signup`: 대학 이메일로 회원가입
- `POST /auth/verify-email`: 이메일 인증 완료
- `GET /me`: 현재 사용자/소속/권한 조회

### Organizations / Memberships

- `GET /organizations`: 사용자 접근 가능한 조직 목록
- `POST /organizations`: 학생회 조직 생성
- `GET /organizations/{organizationId}/members`: 조직 구성원 조회
- `POST /organizations/{organizationId}/invitations`: 재정담당자/감사위원 초대 코드 발급
- `POST /invitations/accept`: 초대 코드 수락

### Settlement Templates

- `POST /organizations/{organizationId}/templates`: 빈 결산안 엑셀 업로드
- `GET /organizations/{organizationId}/templates`: 템플릿 목록
- `PATCH /templates/{templateId}/mapping`: 셀 매핑 저장

### Settlements

- `POST /organizations/{organizationId}/settlements`: 학기별 결산안 생성
- `GET /organizations/{organizationId}/settlements`: 결산안 목록
- `GET /settlements/{settlementId}`: 결산안 상세
- `PATCH /settlements/{settlementId}`: 제목/기간/메타정보 수정
- `POST /settlements/{settlementId}/submit`: 감사 제출
- `POST /settlements/{settlementId}/resubmit`: 반려 후 재제출
- `POST /settlements/{settlementId}/artifacts:generate`: 엑셀/PDF 생성
- `GET /settlements/{settlementId}/artifacts`: 생성 산출물 목록

### Evidence / OCR

- `POST /settlements/{settlementId}/evidences`: 증빙 업로드
- `GET /settlements/{settlementId}/evidences`: 증빙 목록
- `GET /evidences/{evidenceId}`: 증빙 상세
- `POST /evidences/{evidenceId}/extract`: OCR 또는 PDF 텍스트 추출 실행
- `PATCH /evidences/{evidenceId}`: 추출값 수정 및 예산 항목 선택
- `POST /evidences/{evidenceId}/ledger-entry`: 장부 등록 확정

### Bank Statements / Reconciliation

- `POST /settlements/{settlementId}/bank-statements`: 거래내역 엑셀 업로드
- `GET /settlements/{settlementId}/bank-transactions`: 파싱된 거래내역 조회
- `POST /settlements/{settlementId}/reconciliation:run`: 자동 대조 실행
- `GET /settlements/{settlementId}/reconciliation`: 매칭 결과 목록 조회
- `PATCH /reconciliation/{matchId}`: 수동 조정 및 확인 처리

### Audit

- `GET /audit/settlements`: 감사 대기/진행/완료 목록
- `GET /audit/settlements/{settlementId}`: 비교 검토 화면 데이터
- `POST /audit/settlements/{settlementId}/comments`: 항목별 코멘트 생성
- `PATCH /audit/comments/{commentId}`: 코멘트 수정
- `POST /audit/settlements/{settlementId}/approve`: 전체 승인
- `POST /audit/settlements/{settlementId}/reject`: 전체 반려

### Public Viewer

- `GET /public/settlements`: 승인 완료된 결산안 목록
- `GET /public/settlements/{settlementId}`: 공개 결산안 상세
- `GET /public/settlements/{settlementId}/items`: 항목별 웹 조회
- `GET /public/evidences/{evidenceId}`: 공개 가능한 증빙 열람
- `GET /public/settlements/{settlementId}/downloads/{artifactId}`: 산출물 다운로드

### Dashboard / Notifications

- `GET /dashboard/treasurer`: 재정담당자 대시보드
- `GET /dashboard/auditor`: 감사위원 대시보드
- `GET /notifications`: 인앱 알림 목록
- `POST /notifications/{notificationId}/read`: 읽음 처리

## State Transitions

- `settlement.status`
  - `draft -> ready_for_review -> submitted -> under_audit -> approved`
  - `under_audit -> rejected -> resubmitted -> under_audit`

- `evidence.status`
  - `uploaded -> extracting -> needs_review -> confirmed`
  - `extracting -> failed`

- `artifact.status`
  - `queued -> processing -> completed`
  - `processing -> failed`

## Outstanding Design Questions

- 결산안 승인 직후 자동 공개인지, 수동 공개 승인 단계가 하나 더 필요한지
- 동일 날짜/금액 다건 발생 시 자동 대조 tie-breaker 정책
- 대조 불일치 허용 범위와 수동 override 이력 정책
- 공개용 증빙에서 마스킹해야 할 개인정보 범위

