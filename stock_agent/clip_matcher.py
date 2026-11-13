"""CLIP Late Fusion 필터 — 무신사 스냅 착샷(쿼리) ↔ 재고 후보 멀티모달 유사도

쿼리 임베딩: image_emb * 0.6 + text_emb * 0.4  (착샷 이미지를 열 수 없으면 text 1.0)
    text는 착샷의 02 속성(category_minor, fit, length_body, patterns, materials, details)
후보 임베딩: STOCK_CLIP_INDEX_PATH(기본 data/clip_index.npz)에 미리 계산한 fusion 임베딩 (scripts/build_clip_index.py)
cosine >= 0.60인 후보만 남기고, 하나도 없으면 유사도 상위 FALLBACK_N=20개를 남긴다 (후보를 0개로 만들지 않음).

clip_filter는 (남은 후보, 정보)를 돌려준다. 정보(ClipInfo.to_dict)는 응답 search.clip에 그대로 들어간다.
    applied       필터를 실제로 걸었는가
    skip_reason   걸지 않은 이유: no_candidates(재고 후보 0개), no_index(인덱스 파일 없음),
                  no_match(후보가 인덱스에 하나도 없음)
    image_used    착샷 이미지 임베딩을 썼는가 (false면 텍스트만)
    n_before, n_after, n_indexed(인덱스에 있던 후보 수), threshold, fallback(기준 미달이라 상위 N개)
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

IMAGE_WEIGHT = 0.6
TEXT_WEIGHT = 0.4
THRESHOLD = 0.60
FALLBACK_N = 20

# 환경변수 STOCK_CLIP_INDEX_PATH 우선, 없으면 프로젝트 루트 data/
DEFAULT_INDEX_PATH = Path(
    os.environ.get("STOCK_CLIP_INDEX_PATH", "")
    or Path(__file__).parent.parent / "data" / "clip_index.npz"
)

_model = None
_tokenizer = None
_preprocess = None


def _get_model():
    global _model, _tokenizer, _preprocess
    if _model is None:
        import open_clip
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _model, _, _preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="openai", device=device
        )
        _tokenizer = open_clip.get_tokenizer("ViT-B-32")
        _model.eval()
        logger.info("CLIP 모델 로드 완료 (ViT-B-32, device=%s)", device)
    return _model, _tokenizer, _preprocess


def _encode_text(texts: list[str]) -> np.ndarray:
    """텍스트 목록 → 정규화된 임베딩 (N, 512)"""
    import torch
    model, tokenizer, _ = _get_model()
    tokens = tokenizer(texts)
    with torch.no_grad():
        emb = model.encode_text(tokens)
        emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.cpu().numpy()


def _encode_image_from_url(url: str) -> Optional[np.ndarray]:
    """URL에서 이미지 로드 → 정규화된 임베딩 (512,). 실패 시 None."""
    import torch
    from PIL import Image
    import requests
    model, _, preprocess = _get_model()
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        tensor = preprocess(img).unsqueeze(0)
        with torch.no_grad():
            emb = model.encode_image(tensor)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        return emb.cpu().numpy()[0]
    except Exception as e:
        logger.warning("착샷 이미지 로드 실패 (%s): %s", url, e)
        return None


def _snap_text(ref: dict) -> str:
    """착샷 ref dict → CLIP 텍스트 (ProductMatch 필드)"""
    parts = []
    for key in ("category_minor", "fit", "length_body"):
        v = ref.get(key)
        if v and isinstance(v, str):
            parts.append(v)
    for key in ("patterns", "materials"):
        lst = ref.get(key) or []
        if isinstance(lst, list):
            parts.extend(x for x in lst if isinstance(x, str))
    for d in (ref.get("details") or []):
        if isinstance(d, dict) and d.get("name"):
            parts.append(d["name"])
    return " ".join(parts) or "의류"


class ClipIndex:
    """사전 계산된 재고 후보 임베딩 인덱스 (clip_index.npz)."""

    def __init__(self, path: Path = DEFAULT_INDEX_PATH):
        data = np.load(path, allow_pickle=True)
        self._ids: np.ndarray = data["article_ids"]
        self._embs: np.ndarray = data["embeddings"].astype(np.float32)
        self._id_to_idx = {str(aid): i for i, aid in enumerate(self._ids)}

    def lookup(self, article_ids: list) -> tuple[list, np.ndarray]:
        """article_ids 중 인덱스에 있는 것 → (ids, embs)"""
        found_ids, found_embs = [], []
        for aid in article_ids:
            idx = self._id_to_idx.get(str(aid))
            if idx is not None:
                found_ids.append(aid)
                found_embs.append(self._embs[idx])
        if not found_ids:
            return [], np.empty((0, self._embs.shape[1]), dtype=np.float32)
        return found_ids, np.stack(found_embs)


_indexes: dict[Path, ClipIndex] = {}      # 경로별로 한 번만 읽는다


def _get_index(path: Path = DEFAULT_INDEX_PATH) -> Optional[ClipIndex]:
    path = Path(path)
    if path not in _indexes:
        if not path.exists():
            logger.warning("CLIP 인덱스 없음 (%s): 필터 skip", path)
            return None
        _indexes[path] = ClipIndex(path)
        logger.info("CLIP 인덱스 로드 완료 (%d개)", len(_indexes[path]._ids))
    return _indexes[path]


@dataclass
class ClipInfo:
    applied: bool
    skip_reason: Optional[str] = None      # no_candidates / no_index / no_match
    image_used: bool = False
    n_before: int = 0
    n_after: int = 0
    n_indexed: int = 0
    threshold: float = THRESHOLD
    fallback: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def index_status(index_path: Path = DEFAULT_INDEX_PATH) -> dict:
    """health용: 인덱스를 불러올 수 있는가, 상품 몇 개인가."""
    try:
        index = _get_index(index_path)
    except Exception as e:  # 파일이 깨짐 등: 서버는 뜨고 요청마다 no_index로 기록
        logger.warning("CLIP 인덱스를 읽지 못함 (%s): %s", index_path, e)
        return {"clip_index_loaded": False, "clip_n_indexed": 0}
    return {"clip_index_loaded": index is not None, "clip_n_indexed": 0 if index is None else len(index._ids)}


def warm_up(index_path: Path = DEFAULT_INDEX_PATH) -> dict:
    """서버 시작 때 인덱스와 모델을 미리 불러온다 (첫 요청이 느리지 않게). 인덱스가 없으면 모델도 불러오지 않는다."""
    status = index_status(index_path)
    if status["clip_index_loaded"]:
        try:
            _get_model()
        except Exception as e:
            logger.warning("CLIP 모델을 미리 불러오지 못함: %s", e)
    return status


def clip_filter(
    cand: pd.DataFrame,
    snap_ref: dict,
    index_path: Path = DEFAULT_INDEX_PATH,
) -> tuple[pd.DataFrame, ClipInfo]:
    """착샷 ↔ 재고 후보 유사도 필터 → (남은 후보, ClipInfo).

    인덱스가 없거나 후보가 인덱스에 하나도 없으면 후보를 그대로 돌려주고 applied=False.
    """
    n = int(len(cand))
    if cand.empty:
        return cand, ClipInfo(False, "no_candidates")

    index = _get_index(index_path)
    if index is None:
        return cand, ClipInfo(False, "no_index", n_before=n, n_after=n)

    article_ids = cand["article_id"].tolist()
    found_ids, found_embs = index.lookup(article_ids)
    if not found_ids:
        logger.warning("CLIP 인덱스에 후보 article_id가 없음: 필터 skip")
        return cand, ClipInfo(False, "no_match", n_before=n, n_after=n)

    # 쿼리 임베딩 구성
    text_emb = _encode_text([_snap_text(snap_ref)])[0]  # (512,)
    image_url = snap_ref.get("image_url")
    img_emb = _encode_image_from_url(image_url) if image_url else None
    query_emb = IMAGE_WEIGHT * img_emb + TEXT_WEIGHT * text_emb if img_emb is not None else text_emb
    norm = np.linalg.norm(query_emb)
    if norm > 0:
        query_emb = query_emb / norm

    # cosine 유사도 (임베딩이 이미 정규화돼 있으므로 내적 = cosine). 인덱스에 없는 후보는 0
    sims = found_embs @ query_emb  # (M,)
    id_to_sim = {str(aid): float(s) for aid, s in zip(found_ids, sims)}
    cand = cand.copy()
    cand["_clip_sim"] = cand["article_id"].map(lambda x: id_to_sim.get(str(x), 0.0))

    passing = cand[cand["_clip_sim"] >= THRESHOLD]
    fallback = len(passing) == 0
    if fallback:
        logger.info("CLIP threshold 미달 전체 — 상위 %d개 fallback", FALLBACK_N)
        result = cand.nlargest(FALLBACK_N, "_clip_sim")
    else:
        result = passing
        logger.info("CLIP 필터 통과: %d / %d개", len(result), n)
    result = result.drop(columns=["_clip_sim"]).reset_index(drop=True)
    return result, ClipInfo(True, None, img_emb is not None, n, int(len(result)), len(found_ids), THRESHOLD, fallback)
