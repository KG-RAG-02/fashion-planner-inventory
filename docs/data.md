# 데이터 카드

이 레포에는 **코드만** 있습니다. 데이터셋, 파생 테이블, 상품 사진, 인덱스는 들어 있지 않으며 넣어서도 안 됩니다.
MIT 라이선스는 이 레포의 코드에만 적용됩니다. 아래 데이터셋에는 적용되지 않으며, 각 데이터셋은 자기 조건을 따릅니다.

## 출처

| 출처 | 쓰는 곳 | 조건 | 이 레포에 있는 것 |
|---|---|---|---|
| [H&M Personalized Fashion Recommendations](https://www.kaggle.com/competitions/h-and-m-personalized-fashion-recommendations) (Kaggle) | `articles`, `transactions_train`, 상품 사진 | 대회 규칙: Kaggle에서 동의한 뒤 내려받습니다. 비상업 용도만 허용되고 데이터를 재배포할 수 없습니다(규칙 7.B). 행 단위 파생 파일도 대회 데이터로 봅니다. | 없음. 직접 받아 로컬에서 빌드합니다. |
| [`disl/musinsa-snap-new`](https://huggingface.co/datasets/disl/musinsa-snap-new) (Hugging Face) | CLIP 기준 이미지로 쓰는 착샷 | 데이터셋 카드에 라이선스 표기가 없습니다. 모든 권리가 유보된 것으로 보고, 자기 비상업 연구에만 쓰며, 파생 파일은 비공개로 두고, 재배포하지 않습니다. | 데이터셋 ID와 고정 revision만 |
| 무신사 상품 이미지 | trend 결과가 상품 이미지 URL을 줄 때의 CLIP 기준 이미지 | 제3자 이미지입니다. 요청을 처리할 때 허용된 호스트(`msscdn.net`)에서 https로만 받아 메모리에서 임베딩만 계산하고, 저장하거나 다른 곳으로 보내지 않습니다. | 없음 |
| [K-Fashion 가이드라인](https://www.aihub.or.kr/) (AI Hub, Ver 1.04) | `article_attrs`의 카테고리·색·소재 어휘 | 어휘만 씁니다. 이미지나 라벨은 쓰지 않습니다. | 코드 안의 대응 규칙 |
| OpenCLIP `ViT-B-32-quickgelu`, OpenAI 가중치 | 이미지·텍스트 임베딩 | `open_clip`이 실행 중 내려받습니다. 조건은 OpenCLIP과 OpenAI CLIP 저장소를 보세요. | 없음 |
| 합성 fixture(`fixture_db`) | 오프라인 stub과 테스트 | 이 레포의 코드가 만듭니다. H&M 데이터가 없습니다. | 필요할 때 만들며 커밋하지 않음 |

## 로컬에서 DB 만들기

준비물: 대회 규칙에 동의한 Kaggle 계정, [Kaggle CLI](https://github.com/Kaggle/kaggle-api), 전체 압축 파일을 받을 수십 GB의 여유 공간(대부분 상품 사진), `dev`·`clip` extras.

```bash
pip install -e ".[dev,clip]"
kaggle competitions download -c h-and-m-personalized-fashion-recommendations -p data/raw
unzip -j data/raw/h-and-m-personalized-fashion-recommendations.zip articles.csv transactions_train.csv -d data/

python scripts/build_db.py             # data/*.csv → data/stock.duckdb: articles, transactions(2020-09-22까지)
python scripts/build_attributes.py     # → article_attrs (K-Fashion 영역·카테고리·색·소재·핏·시즌 적합성)
python scripts/build_virtual_stock.py  # → virtual_stock (가상 재고·판매가, 고정 시드)
python scripts/extract_images.py --zip data/raw/h-and-m-personalized-fashion-recommendations.zip --max-side 512
                                       # → data/images/<앞 3자리>/<article_id>.jpg, 의류만
python scripts/build_clip_index.py --image-dir data/images   # → data/clip_index.npz
```

`data/`는 `.gitignore`와 `.dockerignore`에 들어 있습니다. 그 아래 파일은 커밋하지 마세요.

로컬 파일로 API를 실행합니다(local-file 모드, 객체 저장소 없음).

```bash
INVENTORY_DB_PATH=data/stock.duckdb INVENTORY_IMAGE_DIR=data/images INVENTORY_CLIP_INDEX_PATH=data/clip_index.npz \
  python -m fashion_planner_inventory.server
```

### 선택: 착샷 기준 이미지

trend 결과의 착샷은 `image_ref = hf://datasets/disl/musinsa-snap-new@<revision>#row=<n>`만 줍니다. 착샷 사진 자체를 쓰려면 아래처럼 자기 비공개 버킷에 올립니다(하지 않으면 착샷은 소재·디테일 텍스트만으로 CLIP을 겁니다).

```bash
# 고정 revision의 parquet를 직접 받은 뒤
python scripts/extract_snap_images.py      # → data/snap_images/<revision>/<row>.jpg
python scripts/upload_snap_images.py --dry-run
python scripts/upload_snap_images.py       # → 자기 버킷의 <R2_SNAP_IMAGE_PREFIX>/<revision>/<row>.jpg
```

업로드에는 이 실행에만 넣는 쓰기 권한 키(`R2_UPLOAD_ACCESS_KEY_ID`, `R2_UPLOAD_SECRET_ACCESS_KEY`)를 씁니다. API에는 읽기 전용 키만 줍니다. 버킷은 비공개로 두고, 올린 착샷을 다시 배포하지 마세요.

### 선택: 객체 저장소 배포

DuckDB 파일과 CLIP 인덱스를 자기 비공개 S3 호환 버킷에 올리고 `R2_*` 변수를 설정한 뒤 `python -m fashion_planner_inventory.server --prepare-cache`로 로컬 캐시를 채웁니다. API는 시작할 때 아무것도 내려받지 않습니다. 자세히: [cloudflare-r2.md](cloudflare-r2.md)

## 스키마

| 테이블 | 만드는 스크립트 | 주요 컬럼 |
|---|---|---|
| `articles` | `build_db.py` | `article_id`(앞자리 0을 살린 10자리 문자열), `product_code`, `prod_name`, `product_type_name`, `index_group_name`, `colour_group_name`, `detail_desc` 등 Kaggle 컬럼 전부 |
| `transactions` | `build_db.py` | `t_dat`(DATE, 2020-09-22까지), `customer_id`, `article_id`, `price`, `sales_channel_id` |
| `article_attrs` | `build_attributes.py` | `article_id`, `segment`, `kf_area`, `kf_category`, `kf_color`, `materials`(목록), `fit`, `season_fit`(목록). 의류가 아니면 `kf_area = NULL`이고 분석에서 뺍니다. |
| `virtual_stock` | `build_virtual_stock.py` | `article_id`, `n_sold`, `inbound_qty`, `stock_qty`, `sell_through`, `price_krw`, `stock_value_krw`, `as_of`, `version`, `data_nature = 'sample'` |

`clip_index.npz`: `article_ids`(문자열), `embeddings`(float16, N × 512, 정규화), `rule`, `model`, `n_with_image`, `n_with_text`. API는 `rule`이 자기 규칙과 다른 인덱스를 쓰지 않습니다.

readiness(`/health/ready`)는 테이블, 컬럼 타입, 빈 테이블 여부, 스냅샷 정보(`as_of = 2020-09-22`, 합성 `version`, `data_nature = sample`)를 확인합니다.

## 원본 데이터 규모 (참고)

빌드 당시 기록입니다. articles 105,542행, transactions 31,788,324행(2020-09-22까지). 이 중 의류로 분류된 상품은 79,178개(75.0%)이고, 사진이나 소재·디테일이 있어 CLIP 인덱스에 들어간 상품은 79,174개입니다. 의류 79,178개 중 79,058개(99.8%)에 사진이 있습니다.

## 합성된 것

- 재고, 입고량, 판매율, 판매가는 실제 판매에서 정해진 규칙과 고정 시드로 만듭니다([랭킹 규칙](ranking-rules.md#가상-재고)). 응답에는 `data_nature: sample`로 표시됩니다.
- stub용 fixture DB는 판매 기록까지 포함해 전부를 trend 서비스의 stub 결과에 맞춘 손으로 쓴 몇 행으로 바꿉니다.

## 이 레포에서 뺀 것

비공개 히스토리에는 실제 데이터로 만든 계약 예시(실제 상품 ID, 실제 거래로 계산한 값, 객체 저장소 버전 태그)와 실제 상품 이미지 URL이 든 검증 스크립트가 있었습니다. 둘 다 공개 히스토리에서 지웠고, 예시는 합성 fixture로 다시 만들었습니다.
