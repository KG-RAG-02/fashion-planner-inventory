"""CLIP 유사도 필터 — 기준 이미지(무신사 착샷·상품 이미지) ↔ 재고 후보, 색을 뺀 소재·질감·디테일 비교

색은 조합 색상표가 정하므로 CLIP은 색을 보지 않는다.
    이미지: 기준 이미지와 재고 사진 모두 흑백으로 바꿔 임베딩 (to_gray)
    텍스트: 소재 + 디테일의 영어 대응어 (clip_vocab.clip_text). 기준은 trend materials·details,
            재고는 article_attrs.materials + 상품 설명에서 뽑은 디테일(attributes.extract_details)
    합치기(fuse): image 0.6 + text 0.4. 텍스트가 비면 이미지만, 이미지가 없으면 텍스트만
기준 이미지 열기(load_reference_image): image_url을 먼저, 없거나 실패하면 착샷 image_ref를 R2에서
    (configure_snap_images, R2_SNAP_IMAGE_PREFIX/<revision>/<row>.jpg).
    image_url은 https이고 무신사 이미지 서버(ALLOWED_IMAGE_HOSTS: msscdn.net)인 것만, 5초·리다이렉트 없음·최대 10MB로 받는다.
기준 쪽과 인덱스 쪽(scripts/build_clip_index.py)이 이 모듈의 to_gray·fuse와 clip_vocab.clip_text를 같이 쓴다.
인덱스 파일에는 규칙 이름(INDEX_RULE)이 들어 있고, 다르면 인덱스를 쓰지 않는다(no_index).

후보 임베딩: 미리 계산한 인덱스(npz). 경로는 configure()로 정한다 — STOCK_CLIP_INDEX_PATH,
    없으면 R2_CLIP_INDEX_KEY를 사전 준비(--prepare-cache)로 받아 둔 캐시 파일 (api.lifespan), 기본 data/clip_index.npz.
남기는 규칙(색별 상위 KEEP_RATIO=10%): 후보를 재고 색(kf_color)별로 나눠, 각 색 안에서 유사도 상위 10%(올림, 색마다 최소 1개)를 남긴다.
    유사도 수준은 기준 이미지마다 달라(H&M 사진 기준 중앙값 0.81~0.86) 고정 기준값은 쓰지 않는다.
    색 안에서만 비교하므로 흑백에 남은 밝기 차이가 색 사이 쏠림을 만들지 않고, 조합 색상표가 고른 색 구성이 유지된다.
    색마다 최소 1개라 후보를 0개로 만들지 않는다. 인덱스에 없는 후보는 유사도 0.

clip_filter는 (남은 후보, 정보)를 돌려준다. 정보(ClipInfo.to_dict)는 응답 search.clip에 그대로 들어간다.
    applied       필터를 실제로 걸었는가
    skip_reason   걸지 않은 이유: no_candidates(재고 후보 0개), no_index(인덱스 파일 없음·규칙 다름),
                  no_match(후보가 인덱스에 하나도 없음), no_model(CLIP 모델을 불러오지 못함),
                  no_query(기준 이미지를 열 수 없고 소재·디테일도 없음)
    image_used    기준 이미지 임베딩을 썼는가 (false면 텍스트만)
    text_used     소재·디테일 텍스트를 썼는가 (false면 이미지만)
    n_before, n_after, n_indexed(인덱스에 있던 후보 수), keep_ratio(색별로 남긴 비율), keep_rule(per_color_top)
    image_skip    기준 이미지를 쓰지 못한 이유(이미지를 썼으면 null): no_image(이미지 참조 없음),
                  host_not_allowed(허용되지 않은 URL), no_snap_store(착샷 저장소 R2_SNAP_IMAGE_PREFIX 설정 없음),
                  not_found(착샷이 저장소에 없음, 다른 데이터셋 버전 등), load_failed(내려받기·열기 실패)
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from fashion_planner_inventory.clip_vocab import clip_text

logger = logging.getLogger(__name__)

MODEL_NAME = "ViT-B-32-quickgelu"      # OpenAI 가중치는 QuickGELU로 학습됨 (ViT-B-32로 부르면 정확도가 떨어짐)
PRETRAINED = "openai"
INDEX_RULE = "vit-b-32-quickgelu-openai/gray-image+material-detail-en"   # 인덱스와 쿼리가 같은 규칙(모델 설정 포함)인지 확인
IMAGE_WEIGHT = 0.6
TEXT_WEIGHT = 0.4
KEEP_RATIO = 0.10            # 색별로 유사도 상위 이 비율을 남긴다
KEEP_RULE = "per_color_top"
COLOR_COL = "kf_color"

DEFAULT_INDEX_PATH = Path(__file__).parent.parent / "data" / "clip_index.npz"
_index_path: Optional[Path] = Path(os.environ.get("STOCK_CLIP_INDEX_PATH", "") or DEFAULT_INDEX_PATH)


def configure(path) -> Optional[Path]:
    """서버가 쓸 인덱스 경로를 정한다 (api.lifespan). None이면 인덱스 없음."""
    global _index_path
    _index_path = None if path is None else Path(path)
    return _index_path


# ---------------------------------------------------------------- 모델·인코딩 (기준·인덱스 공용)

_model = None
_tokenizer = None
_preprocess = None
_device = "cpu"


def _get_model(device: str | None = None):
    global _model, _tokenizer, _preprocess, _device
    if _model is None:
        import open_clip
        import torch
        _device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        _model, _, _preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=PRETRAINED,
                                                                        device=_device)
        _tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        _model.eval()
        logger.info("CLIP 모델 로드 완료 (%s, device=%s)", MODEL_NAME, _device)
    return _model, _tokenizer, _preprocess


def to_gray(img):
    """PIL 이미지 → 흑백(3채널). 색을 빼고 명암·질감·형태만 남긴다."""
    return img.convert("L").convert("RGB")


def _normalize(x: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(x, axis=-1, keepdims=True)
    return np.where(norm > 0, x / np.where(norm > 0, norm, 1), x)


def encode_texts(texts: list[str]) -> np.ndarray:
    """텍스트 목록 → 정규화된 임베딩 (N, 512)."""
    import torch
    model, tokenizer, _ = _get_model()
    with torch.no_grad():
        emb = model.encode_text(tokenizer(texts).to(_device)).float()
    return _normalize(emb.cpu().numpy())


def encode_images(images: list) -> np.ndarray:
    """PIL 이미지 목록 → 흑백으로 바꿔 정규화된 임베딩 (N, 512)."""
    import torch
    model, _, preprocess = _get_model()
    batch = torch.stack([preprocess(to_gray(img)) for img in images]).to(_device)
    with torch.no_grad():
        emb = model.encode_image(batch).float()
    return _normalize(emb.cpu().numpy())


def fuse(image_emb: Optional[np.ndarray], text_emb: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """image 0.6 + text 0.4 → 정규화. 한쪽이 없으면 다른 쪽만, 둘 다 없으면 None."""
    if image_emb is None and text_emb is None:
        return None
    if image_emb is None:
        out = text_emb
    elif text_emb is None:
        out = image_emb
    else:
        out = IMAGE_WEIGHT * image_emb + TEXT_WEIGHT * text_emb
    return _normalize(np.asarray(out, dtype=np.float32))


ALLOWED_IMAGE_HOSTS = ("msscdn.net",)    # 기준 이미지 URL은 무신사 이미지 서버(image.msscdn.net 등)만 내려받는다
MAX_IMAGE_BYTES = 10 * 1024 * 1024        # 기준 이미지 1장 최대 크기


def image_host_allowed(url) -> bool:
    """https이고 호스트가 ALLOWED_IMAGE_HOSTS(또는 그 하위 도메인)인 URL만 허용한다."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)


def _load_url_image(url: str):
    """허용된 https URL → PIL 이미지 (5초, 리다이렉트 없음, 최대 MAX_IMAGE_BYTES). 실패하면 None."""
    from PIL import Image
    import requests
    try:
        with requests.get(url, timeout=5, stream=True, allow_redirects=False) as resp:
            resp.raise_for_status()
            raw = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"이미지가 {MAX_IMAGE_BYTES:,} bytes보다 큼")
        return Image.open(BytesIO(raw))
    except Exception as e:
        logger.warning("기준 이미지 로드 실패 (%s): %s", url, e)
        return None


_snap_source = None      # fetch_snap_image(image_ref) → bytes | None 를 가진 객체 (R2ObjectStore). api.lifespan이 정함


def configure_snap_images(source) -> None:
    """착샷 image_ref를 읽을 곳을 정한다. None이면 착샷 이미지를 열지 않는다(텍스트만)."""
    global _snap_source
    _snap_source = source


# 기준 이미지를 쓰지 못한 이유 (ClipInfo.image_skip)
IMAGE_SKIPS = ("no_image", "host_not_allowed", "no_snap_store", "not_found", "load_failed")


def _load_snap_image(image_ref: str):
    """착샷 image_ref → (PIL 이미지, None) 또는 (None, 이유). R2에서 읽는다."""
    from PIL import Image
    if _snap_source is None:
        logger.warning("착샷 이미지 저장소(R2_SNAP_IMAGE_PREFIX) 설정 없음: %s", image_ref)
        return None, "no_snap_store"
    try:
        raw = _snap_source.fetch_snap_image(image_ref)
        return (None, "not_found") if raw is None else (Image.open(BytesIO(raw)), None)
    except Exception as e:
        logger.warning("착샷 이미지 로드 실패 (%s): %s", image_ref, e)
        return None, "load_failed"


def load_reference_image(ref: dict):
    """기준 이미지 → (PIL 이미지, None) 또는 (None, 이유 IMAGE_SKIPS).

    image_url(허용된 무신사 이미지 서버 https)을 먼저, 없거나 실패하면 착샷 image_ref(R2).
    """
    reason = "no_image"
    url = ref.get("image_url")
    if url:
        if not image_host_allowed(url):
            logger.warning("허용되지 않은 기준 이미지 주소: %s", url)
            reason = "host_not_allowed"
        else:
            img = _load_url_image(url)
            if img is not None:
                return img, None
            reason = "load_failed"
    if ref.get("image_ref"):
        img, reason = _load_snap_image(ref["image_ref"])
        if img is not None:
            return img, None
    return None, reason


def query_embedding(ref: dict) -> tuple[Optional[np.ndarray], bool, bool, Optional[str]]:
    """기준 이미지 ref → (쿼리 임베딩, image_used, text_used, image_skip). 모델을 못 불러오면 예외."""
    text = clip_text(ref.get("materials"), ref.get("details"))
    text_emb = encode_texts([text])[0] if text else None
    img, skip = load_reference_image(ref)
    img_emb = None
    if img is not None:
        try:
            img_emb = encode_images([img])[0]
        except Exception as e:
            logger.warning("기준 이미지 임베딩 실패: %s", e)
            skip = "load_failed"
    return fuse(img_emb, text_emb), img_emb is not None, text_emb is not None, skip


# ---------------------------------------------------------------- 인덱스

class ClipIndex:
    """사전 계산된 재고 후보 임베딩 인덱스 (npz: article_ids, embeddings, rule)."""

    def __init__(self, path: Path):
        data = np.load(path, allow_pickle=False)
        rule = str(data["rule"]) if "rule" in data.files else None
        if rule != INDEX_RULE:
            raise ValueError(f"CLIP 인덱스 규칙이 다릅니다 (인덱스 {rule}, 필요 {INDEX_RULE})")
        self._ids: np.ndarray = data["article_ids"]
        self._embs: np.ndarray = data["embeddings"].astype(np.float32)
        self._id_to_idx = {str(aid): i for i, aid in enumerate(self._ids)}

    def __len__(self) -> int:
        return len(self._ids)

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


def _get_index(path: Path | None = None) -> Optional[ClipIndex]:
    path = path or _index_path
    if path is None:
        return None
    path = Path(path)
    if path not in _indexes:
        if not path.exists():
            logger.warning("CLIP 인덱스 없음: 필터 skip")
            return None
        _indexes[path] = ClipIndex(path)
        logger.info("CLIP 인덱스 로드 완료 (%d개)", len(_indexes[path]))
    return _indexes[path]


@dataclass
class ClipInfo:
    applied: bool
    skip_reason: Optional[str] = None      # no_candidates / no_index / no_match / no_model / no_query
    image_used: bool = False
    text_used: bool = False
    n_before: int = 0
    n_after: int = 0
    n_indexed: int = 0
    keep_ratio: float = KEEP_RATIO
    keep_rule: str = KEEP_RULE
    image_skip: Optional[str] = None       # 기준 이미지를 쓰지 못한 이유 (IMAGE_SKIPS). 이미지를 썼으면 None

    def to_dict(self) -> dict:
        return asdict(self)


def index_status(path: Path | None = None) -> dict:
    """health용: 인덱스를 불러올 수 있는가, 상품 몇 개인가."""
    try:
        index = _get_index(path)
    except Exception as e:  # 파일이 깨짐·규칙 다름: 서버는 뜨고 요청마다 no_index로 기록
        logger.warning("CLIP 인덱스를 읽지 못함: %s", e)
        return {"clip_index_loaded": False, "clip_n_indexed": 0}
    return {"clip_index_loaded": index is not None, "clip_n_indexed": 0 if index is None else len(index)}


def warm_up(path: Path | None = None) -> dict:
    """서버 시작 때 인덱스와 모델을 미리 불러온다 (첫 요청이 느리지 않게). 인덱스가 없으면 모델도 불러오지 않는다."""
    status = index_status(path)
    if status["clip_index_loaded"]:
        try:
            _get_model()
        except Exception as e:
            logger.warning("CLIP 모델을 미리 불러오지 못함: %s", e)
    return status


def clip_filter(cand: pd.DataFrame, ref: dict, path: Path | None = None) -> tuple[pd.DataFrame, ClipInfo]:
    """기준 이미지 ↔ 재고 후보 유사도 필터 → (남은 후보, ClipInfo).

    걸지 못하면(인덱스 없음·후보가 인덱스에 없음·모델 없음·쿼리 없음) 후보를 그대로 돌려주고 applied=False.
    """
    n = int(len(cand))
    if cand.empty:
        return cand, ClipInfo(False, "no_candidates")
    try:
        index = _get_index(path)
    except Exception as e:
        logger.warning("CLIP 인덱스를 읽지 못함: %s", e)
        index = None
    if index is None:
        return cand, ClipInfo(False, "no_index", n_before=n, n_after=n)

    found_ids, found_embs = index.lookup(cand["article_id"].tolist())
    if not found_ids:
        return cand, ClipInfo(False, "no_match", n_before=n, n_after=n)

    try:
        query, image_used, text_used, image_skip = query_embedding(ref)
    except Exception as e:      # 모델 가중치를 못 받음 등: CLIP 없이 순위를 매긴다
        logger.warning("CLIP 모델로 기준 임베딩을 만들지 못함: %s", e)
        return cand, ClipInfo(False, "no_model", n_before=n, n_after=n)
    if query is None:
        return cand, ClipInfo(False, "no_query", n_before=n, n_after=n, image_skip=image_skip)

    # cosine 유사도 (임베딩이 이미 정규화돼 있으므로 내적 = cosine). 인덱스에 없는 후보는 0
    sims = found_embs @ query
    id_to_sim = {str(aid): float(s) for aid, s in zip(found_ids, sims)}
    cand = cand.copy()
    cand["_clip_sim"] = cand["article_id"].map(lambda x: id_to_sim.get(str(x), 0.0))

    result = keep_top_per_color(cand).drop(columns=["_clip_sim"]).reset_index(drop=True)
    return result, ClipInfo(True, None, image_used, text_used, n, int(len(result)), len(found_ids),
                            image_skip=None if image_used else image_skip)


def keep_top_per_color(cand: pd.DataFrame, ratio: float = KEEP_RATIO) -> pd.DataFrame:
    """_clip_sim이 붙은 후보 → 색(kf_color)별 유사도 상위 ratio(올림, 최소 1개). 동점은 article_id 순. 원래 순서 유지."""
    color = cand[COLOR_COL].fillna("") if COLOR_COL in cand else pd.Series("", index=cand.index)
    order = cand.assign(_color=color).sort_values(["_color", "_clip_sim", "article_id"], ascending=[True, False, True])
    rank = order.groupby("_color").cumcount() + 1
    size = order.groupby("_color")["_color"].transform("size")
    keep = rank <= size.map(lambda k: max(1, math.ceil(k * ratio - 1e-9)))
    mask = pd.Series(False, index=cand.index)
    mask[order.index[keep.to_numpy()]] = True
    return cand[mask.to_numpy()]
