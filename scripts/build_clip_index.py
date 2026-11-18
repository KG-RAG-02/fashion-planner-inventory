"""재고 CLIP 인덱스 만들기 → data/clip_index.npz (만든 파일은 R2에 올리고 R2_CLIP_INDEX_KEY로 지정)

재고 상품마다 CLIP 임베딩을 미리 계산해 파일 하나에 담는다. 서버는 요청마다 기준 이미지 1장만 계산하고 이 파일과 비교한다.
규칙은 clip_matcher와 같다 (INDEX_RULE이 파일에 기록되고, 서버는 규칙이 다르면 쓰지 않는다).
    이미지: 재고 사진을 흑백으로 바꿔 임베딩 (clip_matcher.to_gray)
    텍스트: article_attrs.materials + 상품 설명(detail_desc)에서 뽑은 디테일(attributes.extract_details)의
            영어 대응어 (clip_vocab.clip_text). 색·카테고리·핏은 넣지 않는다
    합치기: image 0.6 + text 0.4. 텍스트가 비면 이미지만, 사진이 없으면 텍스트만, 둘 다 없으면 인덱스에서 뺀다

대상: virtual_stock에 있는 의류(article_attrs.kf_area 있음) 전체.
DB: STOCK_DB_PATH, 없으면 R2 DB 캐시(없으면 받음). 사진: R2(R2_IMAGE_PREFIX), --image-dir를 주면 그 폴더.

파일: article_ids(str), embeddings(float16, N×512, 정규화), rule, model, n_with_image, n_with_text

실행: python scripts/build_clip_index.py [--out data/clip_index.npz] [--workers 16] [--limit N] [--image-dir PATH]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fashion_planner_inventory import clip_matcher  # noqa: E402
from fashion_planner_inventory.attributes import extract_details  # noqa: E402
from fashion_planner_inventory.clip_vocab import clip_text  # noqa: E402
from fashion_planner_inventory.env import read_dotenv  # noqa: E402
from fashion_planner_inventory.images import image_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_clip_index")

ROOT = Path(__file__).resolve().parents[1]
BATCH = 64
CHUNK = 1024          # 사진을 이만큼씩 받아서 임베딩


def open_db(store):
    path = os.environ.get("STOCK_DB_PATH", "").strip()
    if not path:
        if store is None:
            raise SystemExit("STOCK_DB_PATH도 R2 설정도 없습니다")
        logger.info("R2 DB 확인 (캐시에 없으면 받음)")
        path, version = store.prepare_database(allow_download=True)
        logger.info("DB 버전 %s", version)
    return duckdb.connect(str(path), read_only=True)


def make_reader(store, image_dir: Path | None):
    """article_id → 이미지 bytes (없으면 None)."""
    if image_dir is not None:
        def read(aid: str):
            p = image_path(image_dir, aid)
            return p.read_bytes() if p.is_file() else None
        return read
    if store is None or not store.images_configured:
        raise SystemExit("사진 위치가 없습니다: R2_IMAGE_PREFIX 또는 --image-dir")

    def read(aid: str):
        for attempt in range(3):
            try:
                return store.fetch_image(aid)
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        logger.warning("사진을 받지 못함: %s", aid)
        return None
    return read


def to_pil(raw: bytes | None):
    if raw is None:
        return None
    from PIL import Image
    try:
        img = Image.open(BytesIO(raw))
        img.load()
        return img
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="재고 CLIP 인덱스 만들기")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "clip_index.npz")
    ap.add_argument("--workers", type=int, default=16, help="R2 사진 동시 다운로드 수")
    ap.add_argument("--limit", type=int, default=None, help="앞에서 N개만 (시험용)")
    ap.add_argument("--image-dir", type=Path, default=None, help="R2 대신 이 폴더에서 사진을 읽음")
    ap.add_argument("--device", default=None, help="cpu / mps / cuda (기본: 있으면 cuda→mps→cpu)")
    args = ap.parse_args(argv)

    read_dotenv()
    from fashion_planner_inventory.storage import R2ObjectStore, R2Settings, StorageConfigurationError
    try:
        store = R2ObjectStore(R2Settings.from_env())
    except StorageConfigurationError:
        store = None

    con = open_db(store)
    rows = con.execute("""
        SELECT t.article_id, t.materials, a.detail_desc
        FROM article_attrs t
        JOIN articles a USING (article_id)
        JOIN virtual_stock s USING (article_id)
        WHERE t.kf_area IS NOT NULL
        ORDER BY t.article_id
    """).fetchall()
    con.close()
    if args.limit:
        rows = rows[:args.limit]
    ids = [str(r[0]) for r in rows]
    texts = [clip_text(list(r[1]) if r[1] is not None else [], extract_details(r[2])) for r in rows]
    logger.info("대상 %d개, 텍스트 있는 상품 %d개", len(ids), sum(bool(t) for t in texts))

    import torch
    device = args.device or ("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    clip_matcher._get_model(device)

    # 텍스트: 같은 문장이 많아 고유 문장만 계산
    uniq = sorted({t for t in texts if t})
    text_emb: dict[str, np.ndarray] = {}
    for i in range(0, len(uniq), BATCH):
        batch = uniq[i:i + BATCH]
        for t, e in zip(batch, clip_matcher.encode_texts(batch)):
            text_emb[t] = e
    logger.info("텍스트 임베딩 %d종", len(text_emb))

    read = make_reader(store, args.image_dir)
    out_ids, out_embs, n_img, n_txt, started = [], [], 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(ids), CHUNK):
            chunk_ids = ids[start:start + CHUNK]
            images = list(pool.map(lambda a: to_pil(read(a)), chunk_ids))
            img_emb: dict[int, np.ndarray] = {}
            have = [k for k, img in enumerate(images) if img is not None]
            for b in range(0, len(have), BATCH):
                part = have[b:b + BATCH]
                for k, e in zip(part, clip_matcher.encode_images([images[k] for k in part])):
                    img_emb[k] = e
            for k, aid in enumerate(chunk_ids):
                t = texts[start + k]
                fused = clip_matcher.fuse(img_emb.get(k), text_emb.get(t) if t else None)
                if fused is None:
                    continue
                out_ids.append(aid)
                out_embs.append(fused)
                n_img += k in img_emb
                n_txt += bool(t)
            done = min(start + CHUNK, len(ids))
            rate = done / max(time.time() - started, 1e-6)
            logger.info("%d / %d (사진 %d개, %.0f개/초, 남은 시간 약 %.0f분)", done, len(ids), n_img, rate,
                        (len(ids) - done) / rate / 60)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.out, article_ids=np.array(out_ids), embeddings=np.stack(out_embs).astype(np.float16),
             rule=np.array(clip_matcher.INDEX_RULE), model=np.array(f"{clip_matcher.MODEL_NAME}/{clip_matcher.PRETRAINED}"),
             n_with_image=np.array(n_img), n_with_text=np.array(n_txt))
    logger.info("저장 완료: %s (%d개, 사진 %d개, 텍스트 %d개, %.1fMB)", args.out, len(out_ids), n_img, n_txt,
                args.out.stat().st_size / 1e6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
