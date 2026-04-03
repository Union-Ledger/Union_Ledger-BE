from __future__ import annotations

import argparse
import shutil
import urllib.request
from pathlib import Path

from union_ledger.core.config import get_settings

TESSDATA_BASE_URLS = {
    "fast": "https://github.com/tesseract-ocr/tessdata_fast/raw/main",
    "best": "https://github.com/tesseract-ocr/tessdata_best/raw/main",
}


def main() -> int:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Set up Korean Tesseract language data locally.")
    parser.add_argument("--variant", choices=("fast", "best"), default="best")
    parser.add_argument("--target-dir", default=str(settings.tessdata_dir))
    parser.add_argument("--tesseract-cmd", default=settings.tesseract_cmd)
    args = parser.parse_args()

    target_dir = Path(args.target_dir).resolve()
    tesseract_cmd = Path(args.tesseract_cmd).resolve()
    system_tessdata_dir = tesseract_cmd.parent / "tessdata"

    if not tesseract_cmd.exists():
        raise FileNotFoundError(f"Tesseract 실행 파일을 찾지 못했습니다: {tesseract_cmd}")
    if not system_tessdata_dir.exists():
        raise FileNotFoundError(
            f"시스템 tessdata 디렉터리를 찾지 못했습니다: {system_tessdata_dir}"
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    _copy_if_missing(system_tessdata_dir / "eng.traineddata", target_dir / "eng.traineddata")
    _download_file(
        f"{TESSDATA_BASE_URLS[args.variant]}/kor.traineddata",
        target_dir / "kor.traineddata",
    )

    print("Korean OCR setup complete")
    print(f"- target_dir: {target_dir}")
    print(f"- files: {[path.name for path in sorted(target_dir.glob('*.traineddata'))]}")
    return 0


def _copy_if_missing(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    if not source.exists():
        raise FileNotFoundError(f"기본 언어팩을 찾지 못했습니다: {source}")
    shutil.copy2(source, destination)


def _download_file(url: str, destination: Path) -> None:
    if destination.exists():
        return
    with urllib.request.urlopen(url) as response, destination.open("wb") as output_file:
        shutil.copyfileobj(response, output_file)


if __name__ == "__main__":
    raise SystemExit(main())
