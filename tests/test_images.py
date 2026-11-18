"""상품 이미지: base64로 응답에 넣고, 이미지가 없으면 다음 순위로"""

import base64

from fashion_planner_inventory.agent import run, validate_output
from fashion_planner_inventory.images import image_path, load_image

JPG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
TREND = {"style": {"id": "style_x", "name": "색 없음"},
         "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": []}]}
REQ = {"schema_version": "1.0", "request_id": "req_img", "season": "2027-SS",
       "target_category": "상의", "segment": "Ladieswear", "trend": TREND}   # 후보 a1, a2


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
    first = run(REQ, con, image_dir=str(tmp_path / "none"))["product"]["sku_ids"][0]
    _put(tmp_path, first)
    resp = run(REQ, con, image_dir=str(tmp_path))
    p = resp["product"]
    assert resp["trend_result"]["rank"] == 1 and p["sku_ids"][0] == first
    assert base64.b64decode(p["image"]["data"]) == JPG
    assert "inventory:E-005" in {e["evidence_id"] for e in resp["evidence"]} and validate_output(resp) == []


def test_skips_to_next_rank_when_top1_has_no_image(con, tmp_path):
    first = run(REQ, con, image_dir=str(tmp_path / "none"))["product"]["sku_ids"][0]
    second = ({"a1", "a2"} - {first}).pop()
    _put(tmp_path, second)                                  # 2위만 이미지 있음
    resp = run(REQ, con, image_dir=str(tmp_path))
    tr = resp["trend_result"]
    assert resp["product"]["sku_ids"][0] == second and tr["rank"] == 2 and resp["product"]["image"] is not None
    assert any("이미지가 있는 최상위 2위" in x for x in tr["limitations"])
    assert "2위" in tr["summary"]


def test_no_image_anywhere_returns_top1_without_image(con, tmp_path):
    tmp_path.joinpath("empty").mkdir()
    resp = run(REQ, con, image_dir=str(tmp_path / "empty"))
    assert resp["trend_result"]["rank"] == 1 and resp["product"]["image"] is None
    assert any("이미지가 있는 상품이 없어" in x for x in resp["trend_result"]["limitations"])
    assert "inventory:E-005" not in {e["evidence_id"] for e in resp["evidence"]}
    assert validate_output(resp) == []


def test_missing_image_folder(con, tmp_path):
    resp = run(REQ, con, image_dir=str(tmp_path / "nope"))
    assert resp["product"]["image"] is None
    assert any("이미지 폴더" in x for x in resp["trend_result"]["limitations"])
