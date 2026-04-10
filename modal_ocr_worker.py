"""Modal serverless GPU worker for PaddleOCR inference.

Deploy:
    modal deploy modal_ocr_worker.py

Test locally:
    modal run modal_ocr_worker.py

The worker exposes a web endpoint that accepts image bytes and returns
OCR results (text, confidence, bounding boxes) as JSON.
"""

from __future__ import annotations

import modal

app = modal.App("union-ledger-ocr")

ocr_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")
    .pip_install(
        "paddlepaddle-gpu==3.0.0",
        extra_index_url="https://www.paddlepaddle.org.cn/packages/stable/cu126/",
    )
    .pip_install(
        "paddleocr==3.3.3",
        "opencv-python-headless>=4.12.0",
        "numpy>=2.0.0",
        "pillow>=11.0.0",
        "fastapi[standard]",
    )
    .env(
        {
            "DISABLE_MODEL_SOURCE_CHECK": "True",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        }
    )
)


@app.cls(
    image=ocr_image,
    gpu="T4",
    timeout=120,
    scaledown_window=300,
)
class OCRWorker:
    """PaddleOCR inference on GPU.  Kept warm for 5 minutes after last request."""

    @modal.enter()
    def load_model(self) -> None:
        import os

        os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

        from paddleocr import PaddleOCR

        candidate_kwargs_list = [
            {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "text_detection_model_name": "PP-OCRv5_mobile_det",
                "text_recognition_model_name": "korean_PP-OCRv5_mobile_rec",
            },
            {
                "lang": "korean",
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            },
            {"lang": "korean"},
            {},
        ]
        self.engine = None
        for kwargs in candidate_kwargs_list:
            try:
                self.engine = PaddleOCR(**kwargs)
                break
            except (TypeError, ValueError):
                continue
        if self.engine is None:
            raise RuntimeError("PaddleOCR 초기화 실패")

    @modal.method()
    def run_ocr(self, image_bytes: bytes) -> dict:
        """Run OCR on a single image.

        Args:
            image_bytes: PNG/JPEG image bytes.

        Returns:
            {"lines": [{"text": str, "confidence": float, "box": list}]}
        """
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = Path(tmp.name)

        try:
            if hasattr(self.engine, "predict"):
                raw_result = list(self.engine.predict(str(tmp_path)))
            else:
                raw_result = self.engine.ocr(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

        return {"lines": _parse_result(raw_result)}


@app.function(image=ocr_image, gpu="T4", timeout=120)
@modal.fastapi_endpoint(method="POST")
def ocr_endpoint(data: dict) -> dict:
    """HTTP endpoint alternative — accepts base64 image, returns OCR lines.

    Body: {"image_b64": "<base64 encoded image>"}
    """
    import base64

    image_bytes = base64.b64decode(data["image_b64"])
    worker = OCRWorker()
    return worker.run_ocr.remote(image_bytes)


def _parse_result(raw_result: object) -> list[dict]:
    """Normalize PaddleOCR output into a flat list of line dicts."""
    lines: list[dict] = []
    for item in _iter_items(raw_result):
        if isinstance(item, dict) and "rec_texts" in item:
            texts = item.get("rec_texts") or []
            scores = item.get("rec_scores") or []
            boxes = item.get("rec_polys") or item.get("dt_polys") or []
            for i, text in enumerate(texts):
                confidence = float(scores[i]) if i < len(scores) else 0.0
                box = _normalize_box(boxes[i]) if i < len(boxes) else []
                lines.append({"text": str(text).strip(), "confidence": confidence, "box": box})
            continue
        box, text, confidence = _unpack_item(item)
        if text:
            lines.append({"text": text, "confidence": confidence, "box": box})
    return lines


def _iter_items(raw_result: object) -> list:
    if raw_result is None:
        return []
    if isinstance(raw_result, list):
        items: list = []
        for page in raw_result:
            if isinstance(page, list):
                items.extend(page)
            elif page:
                items.append(page)
        return items
    return [raw_result]


def _unpack_item(item: object) -> tuple[list, str, float]:
    if isinstance(item, dict):
        box = item.get("bbox") or item.get("box") or []
        text = str(item.get("text") or "").strip()
        confidence = float(item.get("score") or item.get("confidence") or 0.0)
        return _normalize_box(box), text, confidence
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        box = _normalize_box(item[0])
        content = item[1]
        if isinstance(content, (list, tuple)) and len(content) >= 2:
            return box, str(content[0]).strip(), float(content[1])
        if isinstance(content, str):
            confidence = float(item[2]) if len(item) > 2 else 0.0
            return box, content.strip(), confidence
    return [], str(item).strip(), 0.0


def _normalize_box(raw_box: object) -> list[list[float]]:
    if not isinstance(raw_box, (list, tuple)):
        return []
    box: list[list[float]] = []
    for point in raw_box:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            box.append([float(point[0]), float(point[1])])
    return box
