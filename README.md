# stock-agent (과잉재고 에이전트)

VOGUE 기획 파이프라인 **04 상품 재고** 단계. 요청 카테고리의 짝 카테고리에서 트렌드 색과 어울리는 색의
과잉재고를 순위로 매겨 **1위 상품 1개(사진 포함)**와 상위 50개 속성 요약을 반환한다. 응답은 팀 공통 envelope + `InventoryProductResult` 형식.

- agent_id: `stock-agent`
- 재고·판매가는 가상 데이터(`data_nature: sample`). 상세 규칙은 `docs/issues_draft.md`

## 1. 데이터 준비 (한 번만)

원본 데이터와 DB는 Git·이미지에 넣지 않는다(Kaggle 대회 규정). `data/` 폴더에 직접 만든다.

```powershell
pip install -r requirements-dev.txt
python scripts/build_db.py          # H&M CSV → data/stock.duckdb
python scripts/build_attributes.py  # K-Fashion 속성 → article_attrs
python scripts/build_stock_v1.py    # 가상 재고 → stock_v1
python scripts/extract_images.py --zip data/raw/h-and-m-personalized-fashion-recommendations.zip
                                    # 의류 상품 이미지만 → data/images/<앞 3자리>/<article_id>.jpg
python -m pytest
```

- 이미지는 Kaggle 대회 전체 압축 파일(`kaggle competitions download -c h-and-m-personalized-fashion-recommendations -p data/raw`)에서
  의류 상품 것만 꺼낸다. `--max-side 512`를 주면 긴 변 512px로 줄여 저장해 응답이 가벼워진다(Pillow 필요).
- 이미지 폴더는 환경변수 `STOCK_IMAGE_DIR`(기본 `data/images`).

## 2. 로컬 실행

```powershell
python scripts/run_agent.py --request examples/request-example.json   # 요청 1건
python scripts/validate_requests.py                                   # 정확도 검증 10건
uvicorn stock_agent.api:app --port 8002                                 # HTTP API
```

## 3. Docker 실행

```powershell
docker build -t stock-agent:v1 .
docker run --rm -p 8002:8002 -v "${PWD}/data:/data:ro" stock-agent:v1
```

- `data/stock.duckdb`와 `data/images/`를 컨테이너의 `/data`에 읽기 전용(`:ro`)으로 연결한다.
- 확인: 브라우저에서 http://localhost:8002/health 또는 http://localhost:8002/docs
  (`/docs`에서 POST → Try it out → Examples에서 예시 요청을 골라 Execute)

요청 보내기 (다른 PowerShell 창):

```powershell
Invoke-RestMethod -Uri http://localhost:8002/v1/inventory-products -Method Post `
  -ContentType "application/json; charset=utf-8" -InFile examples/request-example.json |
  ConvertTo-Json -Depth 10
```

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health` | DB 연결과 필수 테이블(articles, transactions, article_attrs, stock_v1) 확인 |
| POST | `/v1/inventory-products` | 요청 1건 → 응답. 입력 오류 400, 내부 오류 500, 그 외 200 (`status`로 ok/partial/no_results 구분) |

요청 필드:

| 필드 | 주는 단계 | 필수 | 설명 |
|---|---|---|---|
| `request_id` | - | 예 | |
| `season` | 02 | 예 | 예: `2027-SS`, `{"value": "2027 봄", "source": "inferred"}` |
| `target_category` | 02 | 아니오 | 사용자 기획 카테고리(상의/하의/아우터/원피스 또는 세부 카테고리). 있으면 이것의 짝을 찾음 |
| `trend_categories` | 03 | 아니오 | 트렌드 카테고리 목록. 사용자 카테고리가 없을 때 각 짝의 교집합을 찾음. 공통 짝이 없으면 트렌드 카테고리와 그 짝 전체 |
| `trend_colors` | 03 | 있으면 반드시 사용 | K-Fashion 21색으로 정제된 목록. 짝 카테고리 안에서 이 색과 어울리는 조합색만 찾음. 트렌드에 색이 없을 때만 비어서 오고, 그때는 색 조건 없이 찾음 |
| `segment` | 02 | 아니오 | 고객군 |
| `schema_version` | - | 아니오 | `1.0` |

카테고리(`target_category`·`trend_categories`)와 `trend_colors`가 모두 없으면 400 오류다. 카테고리 없이 색만 있으면 전체 영역에서 조합색으로 찾는다.

응답 `products`는 H&M 이미지가 있는 최상위 상품 1개다(1위에 이미지가 없으면 다음 순위, `rank`는 실제 순위). 사진은 `products[].image`에 base64 JPEG로 들어간다(`media_type`, `encoding`, `data`, `size_bytes`, `source`). 이미지가 하나도 없으면 1위를 `image: null`로 반환한다. `attribute_summary`는 상위 50개 후보의 분포다(`scope` 필드).

**트렌드 여러 개**: 같은 주소에 `trends` 목록(각 항목 `trend_id`, `name`, `trend_colors`, `trend_categories`)을 담으면 트렌드마다 1위 1개, 총 n개를 돌려준다. 상품에 `trend_id`·`trend_name`이 붙고, 트렌드별 요약은 `trend_results`에 있다. 예시: `examples/request-trends-example.json`, 코드: `stock_agent/multi.py`.

포트(8002)·경로는 팀 공통 OpenAPI가 정해지면 맞춘다.

## 구조

```
stock_agent/   season.py  attributes.py  synthetic.py  overstock.py  agent.py  api.py
scripts/     데이터 생성·실행·검증 스크립트
tests/       단위·SQL·API 테스트 (duckdb·fastapi가 없으면 해당 테스트는 건너뜀)
examples/    예시 요청
docs/        이슈 초안(범위·계약·결정 사항)
```
