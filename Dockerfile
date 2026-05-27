FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Native libs the OCR stack needs at import time:
#   - libgl1 / libglib2.0-0  : opencv-python-headless still links against libGL
#   - libgomp1               : OpenMP runtime used by paddlepaddle
# Without these, `import cv2` fails with "libGL.so.1: cannot open shared object
# file" and the /ocr/* endpoints return ExtractionConfigurationError.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

CMD ["uvicorn", "union_ledger.main:app", "--host", "0.0.0.0", "--port", "8000"]

