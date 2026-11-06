"""상품 이미지: base64로 응답에 넣고, 이미지가 없으면 다음 순위로"""

import base64

from stock_agent.agent import run, validate_response
from stock_agent.images import image_path, load_image

JPG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
REQ = {"schema_version": "1.0", "request_id": "req_img", "season": "2027-SS",
       "target_category": "상의", "segment": "Ladieswear", "trend_colors": []}   # 후보 a1, a2


def _put(tmp_path, aid):
    p = image_path(tmp_path, aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(JPG)


def test_load_image_roundtrip(tmp_path):
    assert load_image(tmp_path, "0999990001") is None
    _put(tmp_path, "0999990001")
    assert image_path(tmp_path, "0999990001").as_posix().endswith("099/0999990001.jpg")
    img = load_image(tmp_path, "0999990001")
    assert base64.b64decode(img["data"]) == JPG
    assert img["media_type"] == "image/jpeg" and img["size_bytes"] == len(JPG)


def test_top1_with_image(con, tmp_path):
    first = run(REQ, con, image_dir=str(tmp_path / "none"))["products"][0]["sku_ids"][0]
    _put(tmp_path, first)
    resp = run(REQ, con, image_dir=str(tmp_path))
    p = resp["products"][0]
    assert p["rank"] == 1 and p["sku_ids"][0] == first
    assert base64.b64decode(p["image"]["data"]) == JPG
    assert "stock-agent:E-005" in p["evidence_ids"] and validate_response(resp) == []


def test_skips_to_next_rank_when_top1_has_no_image(con, tmp_path):
    ranked = run({**REQ, "request_id": "x"}, con, image_dir=str(tmp_path / "none"), n=2)["products"]
    second = ranked[1]["sku_ids"][0]
    _put(tmp_path, second)                                  # 2위만 이미지 있음
    resp = run(REQ, con, image_dir=str(tmp_path))
    p = resp["products"][0]
    assert p["sku_ids"][0] == second and p["rank"] == 2 and p["image"] is not None
    assert any("이미지가 있는 최상위 2위" in x for x in resp["limitations"])
    assert "2위" in resp["summary"]


def test_no_image_anywhere_returns_top1_without_image(con, tmp_path):
    tmp_path.joinpath("empty").mkdir()
    resp = run(REQ, con, image_dir=str(tmp_path / "empty"))
    p = resp["products"][0]
    assert p["rank"] == 1 and p["image"] is None
    assert any("이미지가 있는 상품이 없어" in x for x in resp["limitations"])
    assert validate_response(resp) == []


def test_missing_image_folder(con, tmp_path):
    resp = run(REQ, con, image_dir=str(tmp_path / "nope"))
    assert resp["products"][0]["image"] is None
    assert any("이미지 폴더" in x for x in resp["limitations"])
