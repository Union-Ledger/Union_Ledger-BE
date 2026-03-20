# Union Ledger Backend

Union Ledger의 백엔드/API 저장소입니다. 첨부된 기능 명세서(2026-03-20)를 기준으로 MVP 착수를 위한 기본 세팅과 API 설계를 먼저 반영했습니다.

## Chosen Stack

- FastAPI
- SQLAlchemy 2.0 + Alembic
- PostgreSQL
- Pytest + Ruff
- OpenAPI 기반 API 설계 문서

## Quick Start

1. `.env.example`을 복사해 `.env`를 만듭니다.
2. 가상환경을 만든 뒤 의존성을 설치합니다.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

3. 로컬 DB를 띄웁니다.

```powershell
docker compose up -d postgres
```

4. 마이그레이션을 적용하고 서버를 실행합니다.

```powershell
alembic upgrade head
uvicorn union_ledger.main:app --reload
```

## Project Structure

- `src/union_ledger`: FastAPI 앱과 도메인 모델
- `alembic`: DB 마이그레이션
- `docs/api/openapi.yaml`: MVP API 명세 초안
- `docs/api/mvp-api-spec.md`: 엔드포인트/흐름 설명 문서
- `docs/architecture/mvp-architecture.md`: 도메인/아키텍처 정리
- `tests`: 기본 헬스체크 테스트

## Current Scope

이번 커밋은 본격 기능 구현 전 단계의 부트스트랩입니다.

- 애플리케이션 실행 구조와 환경설정
- 핵심 도메인 기준의 초기 스키마
- Alembic 마이그레이션 기반
- MVP용 REST API 명세 초안
- 헬스체크 및 메타 엔드포인트

## Assumptions Captured From The Spec

- 조직 모델은 `단과대 > 학과 학생회`를 표현하되, 초기 스키마에서는 `college_name`과 `department_name`을 가진 단일 `organizations` 테이블로 단순화했습니다.
- 역할은 `student`, `treasurer`, `auditor`, `admin`으로 시작합니다.
- 증빙-거래내역 대조는 MVP 기준 `날짜 + 금액` 중심의 1:1 매칭을 가정합니다.
- 공개 범위는 감사 승인된 결산안으로 한정합니다.

## Next Recommended Steps

1. 이메일 인증 방식과 파일 저장소 전략을 확정합니다.
2. OCR/텍스트 추출 비동기 파이프라인을 설계합니다.
3. 인증/인가와 파일 업로드 API부터 우선 구현합니다.
