"""재고 상품 CLIP 임베딩 사전 계산 → data/clip_index.npz

Late Fusion: image_emb * 0.6 + text_emb * 0.4 (이미지 없으면 text 1.0)
이미지: data/images/{article_id[:3]}/{article_id}.jpg

실행: python scripts/build_clip_index.py [--data-dir PATH]
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
IMAGE_WEIGHT = 0.6
TEXT_WEIGHT = 0.4


def _candidate_text(row) -> str:
    """후보(재고) 텍스트 — article_attrs + articles JOIN 결과"""
    parts = []
    for field in ("kf_category", "kf_color", "fit",
                  "graphical_appearance_name", "perceived_colour_value_name",
                  "garment_group_name"):
        v = getattr(row, field, None)
        if v and isinstance(v, str) and v.strip():
            parts.append(v.strip())
    mats = getattr(row, "materials", None)
    if mats is not None:
        try:
            parts.extend(m for m in list(mats) if isinstance(m, str))
        except TypeError:
            pass
    return " ".join(parts) or "의류"


def main(data_dir: Path):
    import open_clip
    import torch
    from PIL import Image

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("CLIP 모델 로드 (ViT-B-32, device=%s)", device)
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="openai", device=device
    )
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    db_path = data_dir / "stock.duckdb"
    logger.info("DB 연결: %s", db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute("""
        SELECT a.article_id,
               t.kf_category, t.kf_color, t.fit, t.materials,
               a.graphical_appearance_name,
               a.perceived_colour_value_name,
               a.garment_group_name
        FROM article_attrs t
        JOIN articles a USING (article_id)
        WHERE t.kf_area IS NOT NULL
    """).fetchdf()
    con.close()
    logger.info("총 %d개 상품 임베딩 계산 시작", len(df))

    image_dir = data_dir / "images"
    batch_size = 64

    # 텍스트 임베딩 배치 처리
    logger.info("텍스트 임베딩 계산...")
    texts = [_candidate_text(r) for r in df.itertuples()]
    text_embs = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        tokens = tokenizer(batch).to(device)
        with torch.no_grad():
            emb = model.encode_text(tokens)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        text_embs.append(emb.cpu().numpy())
        if i % 2000 == 0:
            logger.info("  텍스트 %d / %d", i, len(texts))
    text_embs = np.concatenate(text_embs, axis=0)  # (N, 512)

    # 이미지 임베딩 (없으면 None)
    logger.info("이미지 임베딩 계산...")
    img_embs = []
    for i, row in enumerate(df.itertuples()):
        aid = str(row.article_id)
        prefix = aid[:3]
        img_path = image_dir / prefix / f"{aid}.jpg"
        if img_path.exists():
            try:
                img = Image.open(img_path).convert("RGB")
                tensor = preprocess(img).unsqueeze(0).to(device)
                with torch.no_grad():
                    emb = model.encode_image(tensor)
                    emb = emb / emb.norm(dim=-1, keepdim=True)
                img_embs.append(emb.cpu().numpy()[0])
            except Exception as e:
                logger.debug("이미지 로드 실패 %s: %s", aid, e)
                img_embs.append(None)
        else:
            img_embs.append(None)
        if i % 2000 == 0:
            logger.info("  이미지 %d / %d", i, len(df))

    img_count = sum(1 for x in img_embs if x is not None)
    logger.info("이미지 임베딩 완료: %d / %d개 (나머지 텍스트 전용)", img_count, len(df))

    # Late Fusion 조합
    logger.info("Late Fusion 임베딩 조합...")
    final_embs = []
    article_ids = []
    for i, (t_emb, i_emb) in enumerate(zip(text_embs, img_embs)):
        fused = IMAGE_WEIGHT * i_emb + TEXT_WEIGHT * t_emb if i_emb is not None else t_emb
        norm = np.linalg.norm(fused)
        if norm > 0:
            fused = fused / norm
        final_embs.append(fused)
        article_ids.append(str(df.iloc[i]["article_id"]))

    out_path = data_dir / "clip_index.npz"
    np.savez(
        out_path,
        article_ids=np.array(article_ids, dtype=object),
        embeddings=np.stack(final_embs).astype(np.float32),
    )
    logger.info("저장 완료: %s (%d개)", out_path, len(article_ids))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="재고 CLIP 인덱스 빌드")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="data/ 디렉터리 경로 (기본: 프로젝트 루트/data)")
    args = parser.parse_args()
    main(args.data_dir)
