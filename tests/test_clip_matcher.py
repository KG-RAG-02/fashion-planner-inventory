"""clip_matcher: CLIP 필터 결과·기록 (모델 없이: 임베딩 함수를 가짜로 바꿈)"""

import numpy as np
import pandas as pd
import pytest

import stock_agent.clip_matcher as clip

DIM = 4


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


@pytest.fixture
def index(tmp_path, monkeypatch):
    """a1은 쿼리와 같은 방향, a2는 직교, a3는 반대. a9는 인덱스에 없음."""
    path = tmp_path / "clip_index.npz"
    np.savez(path, article_ids=np.array(["a1", "a2", "a3"]),
             embeddings=np.stack([unit(1, 0, 0, 0), unit(0, 1, 0, 0), unit(-1, 0, 0, 0)]))
    monkeypatch.setattr(clip, "_indexes", {})
    monkeypatch.setattr(clip, "_encode_text", lambda texts: np.stack([unit(1, 0, 0, 0)]))
    monkeypatch.setattr(clip, "_encode_image_from_url", lambda url: unit(1, 0, 0, 0))
    return path


def cand(*ids):
    return pd.DataFrame({"article_id": list(ids), "stock_qty": [1] * len(ids)})


REF = {"image_url": "https://img.example/s.jpg", "category_minor": "블라우스"}


def test_keeps_candidates_above_threshold(index):
    out, info = clip.clip_filter(cand("a1", "a2", "a3", "a9"), REF, index)
    assert list(out["article_id"]) == ["a1"] and "_clip_sim" not in out
    assert info.to_dict() == {"applied": True, "skip_reason": None, "image_used": True, "n_before": 4, "n_after": 1,
                              "n_indexed": 3, "threshold": clip.THRESHOLD, "fallback": False}


def test_fallback_keeps_top_n_when_nothing_passes(index, monkeypatch):
    monkeypatch.setattr(clip, "_encode_image_from_url", lambda url: unit(0, 0, 1, 0))
    monkeypatch.setattr(clip, "_encode_text", lambda texts: np.stack([unit(0, 0, 1, 0)]))
    monkeypatch.setattr(clip, "FALLBACK_N", 2)
    out, info = clip.clip_filter(cand("a1", "a2", "a3"), REF, index)
    assert info.applied and info.fallback and info.n_after == 2 == len(out)


def test_image_failure_uses_text_only(index, monkeypatch):
    monkeypatch.setattr(clip, "_encode_image_from_url", lambda url: None)
    out, info = clip.clip_filter(cand("a1", "a2"), REF, index)
    assert info.applied and info.image_used is False and list(out["article_id"]) == ["a1"]


def test_no_index_returns_candidates_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(clip, "_indexes", {})
    c = cand("a1", "a2")
    out, info = clip.clip_filter(c, REF, tmp_path / "missing.npz")
    assert out is c and (info.applied, info.skip_reason, info.n_before, info.n_after) == (False, "no_index", 2, 2)


def test_no_match_returns_candidates_unchanged(index):
    out, info = clip.clip_filter(cand("x1", "x2"), REF, index)
    assert list(out["article_id"]) == ["x1", "x2"] and (info.applied, info.skip_reason) == (False, "no_match")


def test_empty_candidates(index):
    out, info = clip.clip_filter(cand(), REF, index)
    assert out.empty and (info.applied, info.skip_reason) == (False, "no_candidates")


def test_index_status_and_warm_up(index, tmp_path, monkeypatch):
    loaded = []
    monkeypatch.setattr(clip, "_get_model", lambda: loaded.append(1))
    assert clip.warm_up(index) == {"clip_index_loaded": True, "clip_n_indexed": 3} and loaded == [1]
    assert clip.warm_up(tmp_path / "missing.npz") == {"clip_index_loaded": False, "clip_n_indexed": 0}
    assert loaded == [1]                                      # 인덱스가 없으면 모델도 불러오지 않음
