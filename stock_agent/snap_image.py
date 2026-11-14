"""무신사 스냅 착샷 이미지 참조(image_ref) → R2 객체 key

02 트렌드 결과의 착샷(kind=snap_garment)은 image_url이 없고 image_ref만 있다:
    hf://datasets/disl/musinsa-snap-new@<revision>#row=<row>
착샷 원본은 HF parquet 행이라 URL로 열 수 없으므로, scripts/extract_snap_images.py로 행마다 JPEG를 꺼내
R2 <R2_SNAP_IMAGE_PREFIX>/<revision>/<row>.jpg 에 둔다. 경로에 revision이 들어가므로
다른 버전의 참조는 객체를 찾지 못해 이미지 없이(텍스트만) 처리된다.
"""

from __future__ import annotations

import re

SNAP_DATASET = "disl/musinsa-snap-new"
SNAP_REVISION = "4335d233ef3b54bc5132a4d572d589042a41219d"     # R2에 올린 버전 (02 graph_schema §2.3)

_REF = re.compile(r"^hf://datasets/(?P<ds>[\w.-]+/[\w.-]+)@(?P<rev>[0-9a-f]{7,64})#row=(?P<row>\d+)$")


def parse_image_ref(ref) -> tuple[str, int] | None:
    """착샷 image_ref → (revision, row). 이 데이터셋의 참조가 아니면 None."""
    m = _REF.match(ref.strip()) if isinstance(ref, str) else None
    if not m or m["ds"] != SNAP_DATASET:
        return None
    return m["rev"], int(m["row"])


def snap_object_key(prefix: str, ref) -> str | None:
    """image_ref → R2 key (<prefix>/<revision>/<row>.jpg). 해석할 수 없으면 None."""
    parsed = parse_image_ref(ref)
    if parsed is None:
        return None
    rev, row = parsed
    return "/".join(p for p in (prefix.strip("/"), rev, f"{row}.jpg") if p)
