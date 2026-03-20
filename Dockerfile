FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

CMD ["uvicorn", "union_ledger.main:app", "--host", "0.0.0.0", "--port", "8000"]

