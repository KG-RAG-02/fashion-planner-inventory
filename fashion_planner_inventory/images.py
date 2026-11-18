"""H&M 상품 이미지

Kaggle H&M 데이터의 images 폴더 구조를 그대로 쓴다:
    <image_dir>/<article_id 앞 3자리>/<article_id>.jpg   예: data/images/099/0999990001.jpg

이미지는 데이터라서 Git·도커 이미지에 넣지 않는다. 실행할 때 data/를 연결한다.
폴더 위치는 환경변수 INVENTORY_IMAGE_DIR (기본 data/images).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

DEFAULT_IMAGE_DIR = os.environ.get("INVENTORY_IMAGE_DIR", "data/images")


def image_path(image_dir: str | Path, article_id: str) -> Path:
    return Path(image_dir) / article_id[:3] / f"{article_id}.jpg"


def has_image(image_dir: str | Path, article_id: str) -> bool:
    remote_check = getattr(image_dir, "image_exists", None)
    if callable(remote_check):
        return bool(remote_check(article_id))
    return image_path(image_dir, article_id).is_file()


def take_notes(image_dir) -> list[str]:
    """이미지 자료원이 남긴 한계 문장 (예: R2 이미지 캐시에 쓰지 못해 캐시 없이 전달함). 로컬 폴더는 없음."""
    take = getattr(image_dir, "take_image_notes", None)
    return list(take()) if callable(take) else []


def load_image(image_dir: str | Path, article_id: str) -> dict | None:
    """이미지를 base64로 읽어 응답에 넣을 dict로. 없으면 None."""
    remote_load = getattr(image_dir, "load_image", None)
    if callable(remote_load):
        raw = remote_load(article_id)
        if raw is None:
            return None
        return {
            "media_type": "image/jpeg",
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
            "size_bytes": len(raw),
            "source": f"H&M images/{article_id[:3]}/{article_id}.jpg",
            "data_nature": "fact",
        }
    p = image_path(image_dir, article_id)
    if not p.is_file():
        return None
    raw = p.read_bytes()
    return {
        "media_type": "image/jpeg",
        "encoding": "base64",
        "data": base64.b64encode(raw).decode("ascii"),
        "size_bytes": len(raw),
        "source": f"H&M images/{article_id[:3]}/{article_id}.jpg",
        "data_nature": "fact",
    }
