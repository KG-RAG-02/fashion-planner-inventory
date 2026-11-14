"""clip_matcher·clip_vocab: CLIP 필터 결과·기록, 텍스트 규칙, 흑백 (모델 없이: 임베딩 함수를 가짜로 바꿈)"""

import numpy as np
import pandas as pd
import pytest

import stock_agent.clip_matcher as clip
from stock_agent.attributes import extract_details
from stock_agent.clip_vocab import DETAIL_EN, MATERIAL_EN, clip_text, detail_names


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def save_index(path, ids, embs, rule=clip.INDEX_RULE):
    np.savez(path, article_ids=np.array(ids), embeddings=np.stack(embs).astype(np.float16), rule=np.array(rule))
    return path


@pytest.fixture
def fake(monkeypatch):
    """쿼리 이미지·텍스트 임베딩을 정한다. 기본은 둘 다 (1,0,0,0)."""
    state = {"image": unit(1, 0, 0, 0), "text": unit(1, 0, 0, 0), "texts": []}
    monkeypatch.setattr(clip, "_indexes", {})
    monkeypatch.setattr(clip, "_load_url_image", lambda url: None if state["image"] is None else object())
    monkeypatch.setattr(clip, "encode_images", lambda imgs: np.stack([state["image"]]))

    def enc_text(texts):
        state["texts"].extend(texts)
        return np.stack([state["text"]])
    monkeypatch.setattr(clip, "encode_texts", enc_text)
    return state


@pytest.fixture
def index(tmp_path):
    """a1은 쿼리와 같은 방향, a2는 직교, a3는 반대. a9는 인덱스에 없음."""
    return save_index(tmp_path / "clip_index.npz", ["a1", "a2", "a3"],
                      [unit(1, 0, 0, 0), unit(0, 1, 0, 0), unit(-1, 0, 0, 0)])


def cand(*ids, colors=None):
    return pd.DataFrame({"article_id": list(ids), "kf_color": colors or ["블랙"] * len(ids), "stock_qty": [1] * len(ids)})


REF = {"kind": "snap_garment", "image_url": "https://img.example/s.jpg", "materials": ["시폰"],
       "details": [{"type": "detail", "value": "러플"}, {"type": "neckline", "value": "브이넥"}]}


# ---- 필터
def test_keeps_top_per_color(index, fake):
    out, info = clip.clip_filter(cand("a1", "a2", "a3", "a9"), REF, index)
    assert list(out["article_id"]) == ["a1"] and "_clip_sim" not in out             # 블랙 4개 → 상위 1개(올림·최소 1)
    assert info.to_dict() == {"applied": True, "skip_reason": None, "image_used": True, "text_used": True,
                              "n_before": 4, "n_after": 1, "n_indexed": 3, "keep_ratio": 0.1,
                              "keep_rule": "per_color_top"}
    assert fake["texts"] == ["chiffon, ruffle"]          # 영어 소재 + 디테일, 넥라인·색·카테고리는 없음


def test_each_color_keeps_at_least_one(index, fake):
    # 색이 다르면 색마다 따로 비교: 화이트 a3는 유사도가 가장 낮아도 화이트 안 1위라 남는다
    out, info = clip.clip_filter(cand("a1", "a2", "a3", colors=["블랙", "블랙", "화이트"]), REF, index)
    assert sorted(out["article_id"]) == ["a1", "a3"] and info.n_after == 2


def test_keep_top_per_color_ratio_rounds_up():
    import numpy as np
    c = pd.DataFrame({"article_id": [f"a{i:02d}" for i in range(25)],
                      "kf_color": ["블랙"] * 20 + ["화이트"] * 3 + [None] * 2, "_clip_sim": np.linspace(0, 1, 25)})
    out = clip.keep_top_per_color(c)
    assert list(out["article_id"]) == ["a18", "a19", "a22", "a24"]       # 20→2, 3→1(올림), 색 없음 2→1
    assert list(clip.keep_top_per_color(c, 0.5)["article_id"]) == [f"a{i:02d}" for i in range(10, 20)] + ["a21", "a22", "a24"]


def test_image_failure_uses_text_only(index, fake):
    fake["image"] = None
    out, info = clip.clip_filter(cand("a1", "a2"), REF, index)
    assert info.applied and (info.image_used, info.text_used) == (False, True) and list(out["article_id"]) == ["a1"]


def test_no_text_uses_image_only(index, fake):
    out, info = clip.clip_filter(cand("a1", "a2"), {"image_url": "https://img.example/c.jpg"}, index)
    assert info.applied and (info.image_used, info.text_used) == (True, False) and fake["texts"] == []


def test_no_image_and_no_text_is_no_query(index, fake):
    fake["image"] = None
    c = cand("a1", "a2")
    out, info = clip.clip_filter(c, {"image_url": "https://img.example/c.jpg", "materials": []}, index)
    assert out is c and (info.applied, info.skip_reason) == (False, "no_query")


def test_no_index_returns_candidates_unchanged(tmp_path, fake):
    c = cand("a1", "a2")
    out, info = clip.clip_filter(c, REF, tmp_path / "missing.npz")
    assert out is c and (info.applied, info.skip_reason, info.n_before, info.n_after) == (False, "no_index", 2, 2)


def test_index_with_other_rule_is_not_used(tmp_path, fake):
    old = save_index(tmp_path / "old.npz", ["a1"], [unit(1, 0, 0, 0)], rule="color-image/v0")
    out, info = clip.clip_filter(cand("a1"), REF, old)
    assert (info.applied, info.skip_reason) == (False, "no_index")
    assert clip.index_status(old) == {"clip_index_loaded": False, "clip_n_indexed": 0}


def test_no_match_returns_candidates_unchanged(index, fake):
    out, info = clip.clip_filter(cand("x1", "x2"), REF, index)
    assert list(out["article_id"]) == ["x1", "x2"] and (info.applied, info.skip_reason) == (False, "no_match")


def test_empty_candidates(index, fake):
    out, info = clip.clip_filter(cand(), REF, index)
    assert out.empty and (info.applied, info.skip_reason) == (False, "no_candidates")


def test_configured_path_and_none(index, fake, monkeypatch):
    monkeypatch.setattr(clip, "_index_path", None)
    assert clip.clip_filter(cand("a1"), REF)[1].skip_reason == "no_index"       # 경로 없음 = 인덱스 없음
    clip.configure(index)
    assert clip.clip_filter(cand("a1"), REF)[1].applied


def test_index_status_and_warm_up(index, tmp_path, monkeypatch):
    monkeypatch.setattr(clip, "_indexes", {})
    loaded = []
    monkeypatch.setattr(clip, "_get_model", lambda *a: loaded.append(1))
    assert clip.warm_up(index) == {"clip_index_loaded": True, "clip_n_indexed": 3} and loaded == [1]
    assert clip.warm_up(tmp_path / "missing.npz") == {"clip_index_loaded": False, "clip_n_indexed": 0}
    assert loaded == [1]                                      # 인덱스가 없으면 모델도 불러오지 않음


# ---- 합치기·흑백
def test_fuse():
    i, t = unit(1, 0, 0, 0), unit(0, 1, 0, 0)
    assert np.allclose(clip.fuse(i, t), unit(0.6, 0.4, 0, 0))
    assert np.allclose(clip.fuse(i, None), i) and np.allclose(clip.fuse(None, t), t)
    assert clip.fuse(None, None) is None


def test_to_gray_removes_color():
    Image = pytest.importorskip("PIL.Image")
    red, blue = Image.new("RGB", (4, 4), (200, 30, 30)), Image.new("RGB", (4, 4), (30, 30, 200))
    r, b = clip.to_gray(red), clip.to_gray(blue)
    assert r.mode == "RGB" and len(set(r.getpixel((0, 0)))) == 1            # R=G=B
    assert r.getpixel((0, 0)) != red.getpixel((0, 0)) and b.getpixel((0, 0))[0] == b.getpixel((0, 0))[2]


# ---- 텍스트 규칙 (clip_vocab)
def test_vocab_covers_02_standard():
    assert len(MATERIAL_EN) == 26 and len(DETAIL_EN) == 40
    from stock_agent import attributes
    assert {n for n, _ in attributes._DETAILS} == set(DETAIL_EN)          # 재고 디테일 = 02 DetailName


def test_clip_text():
    assert clip_text(["시폰", "울·캐시미어"], ["러플"]) == "chiffon, wool cashmere, ruffle"   # 예전 이름도 표준으로
    assert clip_text([{"value": "데님"}], [{"type": "collar", "value": "셔츠칼라"}, {"name": "포켓"}]) == "denim, pockets"
    assert clip_text(["모르는소재"], ["모르는디테일"]) == "" and clip_text(None, None) == ""
    assert detail_names(["러플", {"type": "neckline", "value": "후드"}, {"type": "detail", "value": "후드"}]) == ["러플", "후드"]


@pytest.mark.parametrize("desc, expected", [
    ("Blouse in chiffon with ruffles, puff sleeves and a tie belt at the waist.", ["러플", "퍼프", "띠"]),
    ("Shirt with press-studs down the front and two chest pockets.", ["포켓"]),       # press-stud는 스터드 아님
    ("Jeans in washed denim with hard-worn details and slits at the hems.", ["슬릿", "디스트로이드"]),
    ("Studded leather belt.", ["스터드", "띠"]),
    ("Plain jersey top.", []),
    (None, []),
])
def test_extract_details(desc, expected):
    assert extract_details(desc) == expected
