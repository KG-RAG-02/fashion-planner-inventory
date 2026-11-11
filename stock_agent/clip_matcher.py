"""CLIP Late Fusion 필터 — 착샷(쿼리) ↔ 재고 후보 멀티모달 유사도

쿼리 임베딩: image_emb * 0.6 + text_emb * 0.4  (이미지 없으면 text 1.0)
후보 임베딩: data/clip_index.npz에 사전 계산된 fusion 임베딩 (build_clip_index.py)
cosine >= 0.60 통과, 미달 시 상위 FALLBACK_N=20개 반환
"""
from __future__ import annotations

import logging
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

DEFAULT_INDEX_PATH = Path(__file__).parent.parent / "data" / "clip_index.npz"

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
        data = np.load(path, allow_pickle=False)
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


_index: Optional[ClipIndex] = None


def _get_index(path: Path = DEFAULT_INDEX_PATH) -> Optional[ClipIndex]:
    global _index
    if _index is None:
        if not path.exists():
            logger.warning("CLIP 인덱스 없음 (%s): 필터 skip", path)
            return None
        _index = ClipIndex(path)
        logger.info("CLIP 인덱스 로드 완료 (%d개)", len(_index._ids))
    return _index


def clip_filter(
    cand: pd.DataFrame,
    snap_ref: dict,
    index_path: Path = DEFAULT_INDEX_PATH,
) -> pd.DataFrame:
    """착샷 ↔ 재고 후보 유사도 필터.

    인덱스 파일이 없으면 cand 그대로 반환.
    cosine >= THRESHOLD 통과; 미달 시 상위 FALLBACK_N개 반환.
    """
    if cand.empty:
        return cand

    index = _get_index(index_path)
    if index is None:
        return cand

    # 쿼리 임베딩 구성
    snap_text = _snap_text(snap_ref)
    text_emb = _encode_text([snap_text])[0]  # (512,)

    image_url = snap_ref.get("image_url")
    img_emb = _encode_image_from_url(image_url) if image_url else None

    if img_emb is not None:
        query_emb = IMAGE_WEIGHT * img_emb + TEXT_WEIGHT * text_emb
    else:
        query_emb = text_emb

    norm = np.linalg.norm(query_emb)
    if norm > 0:
        query_emb = query_emb / norm

    # 후보 임베딩 조회
    article_ids = cand["article_id"].tolist()
    found_ids, found_embs = index.lookup(article_ids)

    if not found_ids:
        logger.warning("CLIP 인덱스에 후보 article_id가 없음: 필터 skip")
        return cand

    # cosine 유사도 (임베딩이 이미 정규화돼 있으므로 내적 = cosine)
    sims = found_embs @ query_emb  # (M,)

    id_to_sim = {str(aid): float(s) for aid, s in zip(found_ids, sims)}
    sim_col = cand["article_id"].map(lambda x: id_to_sim.get(str(x), 0.0))

    cand = cand.copy()
    cand["_clip_sim"] = sim_col

    passing = cand[cand["_clip_sim"] >= THRESHOLD]
    if len(passing) == 0:
        logger.info("CLIP threshold 미달 전체 — 상위 %d개 fallback", FALLBACK_N)
        result = cand.nlargest(FALLBACK_N, "_clip_sim")
    else:
        result = passing
        logger.info("CLIP 필터 통과: %d / %d개", len(result), len(cand))

    return result.drop(columns=["_clip_sim"]).reset_index(drop=True)
