# inventory producer payload 계약 (1.0) 및 HTTP handoff (v0.1)

03 상품 재고 에이전트(`inventory`)의 producer 입력·결과 payload 형식은 이 레포 소유 버전 `1.0`입니다. `inventory-handoff`은 project_08 호출 경계의 공통 HTTP envelope 버전이며 payload `schema_version`과 별개입니다.

| 파일 | 내용 |
|---|---|
| `VERSION` | 응답 `schema_version` (`1.0`) |
| `schemas/inventory-request.schema.json` | producer input (JSON Schema 2020-12). TrendAnalysisResult 그대로 |
| `schemas/inventory-result.schema.json` | producer result payload 1.0 |
| `examples/` | 요청 1개, 응답 3개 (정상, 02 no_results, 시즌 없음). 레포의 `examples/`도 02 형식 요청 |

레거시 `POST /v1/inventory-products`도 canonical과 동일한 Bearer 인증을 요구합니다. Project_08용 canonical 경로는 `POST /v1/agents/inventory/runs`이며 `Authorization: Bearer ...`를 요구하고, 요청은 `{contract_version, request_id, input}` envelope입니다. `input`은 아래 producer input 그대로이며 응답은 `{contract_version, request_id, producer, status, result}`로 감싸되 `result` 전체를 보존합니다. `/docs`와 `/openapi.json`에 두 경로를 게시하며 FastAPI 422 대신 400 오류 payload를 사용합니다. `tests/test_contracts.py`는 producer payload, API 테스트는 HTTP envelope를 검사합니다.

## 흐름

```
사용자 쿼리 ──▶ 오케스트레이터 (Project_08): 쿼리 전처리 → ①②③④ 순차 blocking 호출 → 결과 통합 → 기획안 초안 UI
                 ① trend-analysis-agent (02)       ◀ 전처리된 쿼리                                  ▶ TrendResult + Evidence
                 ② inventory (03, 이 레포)        ◀ 전처리된 쿼리 + TrendResult                     ▶ InventoryProductResult
                 ③ new-product-agent (05)          ◀ 전처리된 쿼리 + TrendResult + InventoryProductResult ▶ PlanningDraft
                 ④ sales-market-agent (04)         ◀ PlanningDraft                                  ▶ MarketData
```

에이전트끼리는 직접 부르지 않고 오케스트레이터(Project_08)가 순서대로 부릅니다. Project_08은 직전 Trend producer의 result 객체(전처리된 쿼리 조건 `applied_filters` 포함)를 `input`에 그대로 넣습니다. 03 입구에서만 내부 처리 모양으로 바꾸며(`fashion_planner_inventory/trend_request.py`), 외부 input을 평탄화하거나 Trend 필드/버전을 다시 쓰지 않습니다. Project_08은 검증 뒤 변경하지 않은 Stock `result`를 다음 단계(new-product-agent)에 전달하고, 기획안 초안 조립에도 씁니다.

## 요청

요청은 02 트렌드 결과 그대로만 받습니다. `applied_filters`가 없으면 400 `INVALID_REQUEST`입니다.

| 02 필드 | 03에서 쓰는 곳 |
|---|---|
| `request_id` | 응답에 그대로 돌려줌 |
| `status` | `ok` → 처리, `no_results` → DB를 조회하지 않고 `no_results`, `error` → `INVALID_REQUEST` (`reason: upstream_error`) |
| `applied_filters.season` | 시즌 |
| `applied_filters.categories.include` | 사용자 카테고리. `minor`, 없으면 `major`. `raw`만 있는 항목은 쓰지 않고 limitations에 적음 |
| `applied_filters.categories.exclude` | 제외 카테고리 (같은 방식) |
| `applied_filters.attributes` 중 `type: color` | 사용자 색 |
| `applied_filters.attributes` 중 그 밖(소재·핏 등) | 쓰지 않음 |
| `applied_filters.gender` | 고객군 (아래) |
| `trends[].style`, `trends[].items` | 트렌드 색·카테고리 (아이템 자신의 소재·핏은 재고를 거르는 데 쓰지 않음) |
| `trends[].items[].similar_products` | `kind: snap_garment` 착샷(`image_ref`로 R2에서 읽음, `multi_wearer_suspected: true` 제외. 없으면 `catalog_item` 상품 이미지 `image_url` 또는 http `image_ref`) 중 `score`가 가장 높은 1장 → CLIP 유사도 필터 (아래). `materials`·`details`(`DetailName`)를 CLIP 텍스트로 씀 |
| `evidence`, `limitations` 등 | 쓰지 않음 |

**성별 → 고객군** (H&M 고객군)

| 성별 | 고객군 |
|---|---|
| 여 | Ladieswear, Divided |
| 남 | Menswear, Divided |
| 공용 | 전체 |

- 요청 성별이 없으면 트렌드마다 Item `gender`로 정합니다. 여러 성별이면 고객군을 합치고, `공용`이 있거나 모두 비어 있으면 전체입니다.
- 정한 결과는 `trend_results[i].search.segments`, `segment_basis`(`segment` / `gender` / `item_gender` / `all`)와 limitations에 나옵니다.

**CLIP 유사도 필터**: 사용자 카테고리 아이템(없으면 아이템 전체)의 `similar_products`에서 `snap_garment` 착샷(score 최고 1장)을, 없으면 `catalog_item` 상품 이미지(score 최고 1장)를 기준 이미지로 골라 항상 겁니다. 두 종류 모두 `has_image: false`는 뺍니다. 02 착샷은 `image_url` 없이 `image_ref`(`hf://datasets/disl/musinsa-snap-new@<revision>#row=<n>`)만 있으므로, 같은 revision의 착샷 JPEG를 R2 `<R2_SNAP_IMAGE_PREFIX>/<revision>/<n>.jpg`에 미리 두고 읽습니다. 여러 명이 찍혔을 수 있는 착샷(`multi_wearer_suspected: true`)은 다른 사람 옷이 섞이므로 뺍니다. 상품 이미지는 트렌드 아이템 카테고리라 짝 재고와 카테고리가 달라도 그대로 씁니다. 색은 조합 색상표가 정하므로 CLIP은 색을 보지 않습니다: 기준 이미지와 재고 사진을 흑백으로 임베딩하고, 텍스트는 소재 + 디테일(02 vocabulary_v1 `Material` 26개·`DetailName` 40개)의 영어 대응어만 씁니다(CLIP `ViT-B-32-quickgelu`는 영어 모델). 이미지 0.6 + 텍스트 0.4이고, 텍스트가 비면 이미지만, 이미지를 열 수 없으면 텍스트만 쓰고 그 이유를 `search.clip.image_skip`에 적습니다. 후보를 재고 색별로 나눠 각 색 안에서 유사도 상위 10%(올림, 색마다 최소 1개)를 남깁니다. 유사도 수준이 기준 이미지마다 달라 고정 기준값은 쓰지 않고, 색 안에서만 비교해 밝은 색 재고로 쏠리지 않게 합니다(후보를 0개로 만들지 않음). 둘 다 없으면 CLIP 필터 없이 찾습니다. 재고 후보 임베딩은 미리 만든 CLIP 인덱스(R2 `R2_CLIP_INDEX_KEY`를 `--prepare-cache`로 받은 캐시, 또는 `STOCK_CLIP_INDEX_PATH`)에서 읽고, 인덱스가 없거나 규칙이 다르거나 CLIP 모델을 불러오지 못하면 필터 없이 찾고 그 사실을 기록합니다. 기준 이미지는 03 서버가 직접 읽습니다: 상품 이미지 URL은 https이고 무신사 이미지 서버(`msscdn.net`)인 것만 5초·리다이렉트 없음·최대 10MB로 받고, 착샷은 R2에서 읽습니다. 외부 서비스로 보내지 않습니다. 무신사 착샷·상품 이미지는 02 트렌드 결과로 받은 이미지이며 비상업적 연구용으로만 씁니다.

**사용자 색**: 트렌드 아이템 색이 비어 있으면(02가 색을 확인하지 못함) "트렌드 아이템 색이 확인되지 않아(미확인)", 색은 있는데 안 겹치면 "트렌드 아이템 색에 없습니다"로 limitations에 구분해 적습니다. 두 경우 모두 사용자 색과 어울리는 색으로 찾습니다.

입구에서 바꾼 **내부 형식**(`season`, `target_category`, `exclude_categories`, `target_colors`, `gender`, `trends`)은 코드 안에서만 쓰고 바깥에서는 받지 않습니다.

## 응답

- 색: 기준 색과 어울리는 색 중 가장 많이 겹치는 색 + 기준 색 자체(톤온톤)에서 찾습니다. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 그 색이 있습니다.
- 완화: 재고 조회(짝 영역 · 색 · 시즌 · 고객군 · 재고 > 0 · 제외 카테고리) 결과가 0개면 트렌드 색 조건만 넓혀 다시 조회합니다. 순서는 `max_overlap`(가장 많이 겹치는 색) → `any`(하나라도 어울리는 색, 결과 색이 같으면 건너뜀) → `none`(트렌드 색 조건을 풀고 21색 전체)입니다. 사용자 색(`target_colors`)과 사용자 카테고리는 완화하지 않습니다. 조회한 단계는 `search.color_attempts`(`mode`, `colors`, `n_candidates`), 최종 단계는 `search.color_mode`, 완화한 조건은 `search.relaxed`(`color_rule` max_overlap → any, 또는 `trend_colors` → `[]`)와 limitations에 있습니다.
- 상품 선택: 과잉재고 순위에서 H&M 사진이 있는 최상위 상품입니다. 1위에 사진이 없으면 사진이 있는 다음 순위를 고르고(`rank`는 실제 순위) limitations에 적습니다.
- CLIP 기준 이미지: `search.outfit_reference`(`status` used/skipped, `skip_reason` no_reference/no_index/no_match/no_model/no_query/no_candidates, `kind` snap_garment/catalog_item, `product_id`, `image_url`, `image_ref`, `score`, `source`, `item_id`, `item_category`), `search.clip`(`applied`, `skip_reason`, `image_used`, `text_used`, `n_before`, `n_after`, `n_indexed`, `keep_ratio`, `keep_rule`, `image_skip` no_image/host_not_allowed/no_snap_store/not_found/load_failed; 기준 이미지가 없으면 `null`). CLIP 필터를 걸었으면 근거 `E-006`(`locator` = 기준 이미지 URL, 착샷은 `image_ref`, `source_type` 착샷 external_outfit_image / 상품 이미지 external_product_image)이 붙습니다.

- `products[i]` ↔ `trend_results[i]`. 트렌드마다 상품 1개(사진 포함). 못 고르면 `null`.
- producer result 1.0에는 `findings`와 최상위 `limitations`가 없습니다. 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 있습니다. HTTP envelope는 producer result 외부를 감싸며 필드를 이동·재구성하지 않습니다.
- 근거는 `evidence` 한 곳에 모으고, `trend_results[i].evidence_ids`가 그 트렌드의 근거 ID를 가리킵니다. ID 형식은 `inventory:<style.id>:E-001`. `manifest_id`는 계산에 쓴 DB 버전(`/health`의 `db_version`)이고, 외부 이미지 근거 E-006은 `null`입니다.
- `status`: 모두 ok → `ok`, 일부만 상품 있음 → `partial`, 모두 오류 → `error`, 그 밖 → `no_results`.

## 오류 코드

`errors[].code`는 팀 공통 코드, 03 안의 자세한 원인은 `reason`에 있습니다.

| reason | code | HTTP |
|---|---|---|
| `missing_fields`, `invalid_input`, `schema_version_mismatch`, `upstream_error` | `INVALID_REQUEST` | 400 |
| `db_unavailable`, `image_source_unavailable` | `SOURCE_UNAVAILABLE` | 503 |
| `timeout` (HTTP 처리 제한 시간 초과) | `TIMEOUT` | 504 |
| `invalid_response`, `internal` | `INTERNAL_ERROR` | 500 |
| `trend_failed` (최근 추세만 실패, 나머지 지표로 순위) | `INTERNAL_ERROR` | 200 (`status: partial`) |

HTTP 번호는 응답 `status`가 `error`일 때만 붙습니다. 트렌드 중 일부만 실패하면 200이고, 실패한 트렌드의 오류는 `trend_results[i].errors`에 있습니다. 모든 트렌드가 서로 다른 원인으로 실패하면 서버 쪽 코드(SOURCE_UNAVAILABLE → TIMEOUT → INTERNAL_ERROR → INVALID_REQUEST 순)를 쓰고 `reason`은 `mixed`입니다.

## HTTP 인증·전송 및 실행 제한

- canonical `/v1/agents/inventory/runs`는 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 일치하는 `Authorization: Bearer <token>`을 요구합니다. 누락/오류는 401 `UNAUTHORIZED`; 토큰은 코드, 응답, 로그에 기록하지 않습니다. 토큰은 Project_08의 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 배포 비밀 저장소에서 동일하게 주입합니다.
- 두 inventory route 모두 같은 fail-closed Bearer 인증을 본문 파싱 전에 요구합니다. token 미설정/누락/오류는 401이며 유효 token의 malformed JSON은 400입니다.
- 운영은 TLS 종료 지점에서 HTTPS를 제공하고 backend로 전달할 때 Authorization header를 보존합니다. Project_08은 operational HTTPS + bearer를 요구합니다. loopback HTTP는 개발에서만 사용하며 Project_08의 `VOGUE_AGENT_HTTP_DEV_MODE=1`와 함께 사용합니다. 이 서버는 HTTP/HTTPS scheme을 강제하지 않으므로 운영 scheme 강제는 ingress/reverse proxy 배포 설정의 책임입니다.
- Project_08은 Stock 응답을 210초까지 기다립니다. Stock route는 전체 HTTP 처리에 기본 60초 deadline(`STOCK_HANDOFF_TIMEOUT_SECONDS`, 0 초과 600 이하, 통합 배포는 180초)을 두고, 초과하면 504 `TIMEOUT`(`reason: timeout`) envelope를 반환합니다. 트렌드는 순서대로 처리하며, 트렌드마다 DB 조회와 기준 이미지가 있으면 그 이미지 읽기(상품 이미지 URL 5초, 착샷은 R2)·CLIP 임베딩 계산이 있습니다. 트렌드가 많은 호출자는 deadline을 늘리고 자기 read timeout을 그보다 길게 둡니다. Python thread의 동기 처리는 취소가 보장되지 않아, deadline 순간 이미 실행 중인 DB 조회·이미지 내려받기는 서버 쪽에서 끝까지 실행될 수 있습니다. 현재 구현은 응답 상한만 보장합니다.
- 각 요청은 DuckDB 공유 connection에서 요청별 `cursor()`를 만들고 닫습니다. readiness 검사는 startup에서 수행하고 health에는 저장된 검사 결과를 제공합니다. 병렬 cursor 안전성은 API 동시 요청 테스트에서 회귀 확인합니다.

## Health, API docs 및 환경 변수

- 포트 `8002`, `uvicorn fashion_planner_inventory.api:app --port 8002`, Docker 명령은 README대로 유지합니다. API 시작은 R2 HEAD·기존 checksummed cache 확인만 수행하고 자동 DB 다운로드를 하지 않습니다. DuckDB는 read-only로 엽니다. 이미지 object는 R2 prefix가 설정된 경우 요청되는 이미지에 한해 지연 조회·캐시합니다. `STOCK_DB_PATH`가 설정되면 API는 R2 설정·HEAD 없이 그 로컬 파일을 read-only로 엽니다(local-file 모드, readiness `db: local-file`).
- `/health/live`는 API 생존 200이며 `/health/ready`와 `/health`는 column/type, nonempty tables, sample snapshot 및 수치 검사를 통과해야 200입니다. 실패는 503입니다. `image_objects_verified=false`를 따로 표시합니다. `clip_index_loaded`·`clip_n_indexed`는 서버 시작 때 CLIP 인덱스를 불러왔는지와 상품 수이며, 200/503 판정에는 쓰지 않습니다(인덱스가 없으면 CLIP 필터만 건너뜀).
- `/docs` 및 `/openapi.json`은 레거시와 v0.1 route를 모두 제공합니다. OpenAPI version은 producer schema `1.0`; 개별 응답 producer metadata도 `1.0`, HTTP envelope는 `inventory-handoff`입니다.
- 주요 API 런타임 설정: `R2_ACCOUNT_ID`, `R2_ENDPOINT`(선택), `R2_BUCKET`, `R2_DB_KEY`, `R2_IMAGE_PREFIX`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `STOCK_CACHE_DIR`(선택), `STOCK_DB_PATH`(선택, local-file 모드), `VOGUE_AGENT_SERVICE_BEARER_TOKEN`(canonical run route 필수), `R2_CLIP_INDEX_KEY`(선택, R2 CLIP 인덱스 key, `--prepare-cache`가 캐시에 받음), `R2_SNAP_IMAGE_PREFIX`(선택, 무신사 착샷 JPEG prefix), `STOCK_CLIP_INDEX_PATH`(선택, 로컬 인덱스 파일), `STOCK_HANDOFF_TIMEOUT_SECONDS`(선택, 기본 60). 기본 S3 endpoint는 `https://<account-id>.r2.cloudflarestorage.com`, SDK region은 `auto`입니다. Jurisdiction bucket은 해당 공식 endpoint를 지정합니다. 토큰은 bucket 범위 `Object Read only` 권한을 사용합니다. 비밀은 `.env.example`에 빈 변수만 두며 실제 토큰/키는 배포 secret store를 사용합니다.

## 바꿀 때

스키마를 바꾸면 `examples/`, `fashion_planner_inventory/schemas.py`(설명서), `tests/test_contracts.py`를 같이 고치고 `pytest`를 돌립니다. 호환되지 않는 변경이면 `VERSION`을 올립니다.

### 입력 경계와 readiness

입력 producer version은 필수 `schema_version=1.1`이며 Stock 결과 1.0 및 handoff 0.1과 별개입니다. 원본 입력을 deepcopy한 내부 검색 입력을 사용하고 원본 body hash를 유지합니다. canonical 요청의 outer/inner ID와 version·status를 검증하며 producer agent_id가 명시되면 Trend identity와 같아야 합니다. 전체 Trend Evidence 의미 검증은 이 adapter의 범위가 아닙니다.

readiness는 column/type·nonempty·snapshot 및 수치 null/음수/금액 일관성을 검사합니다. snapshot은 2020-09-22/virtual_stock_v1/sample과 일치해야 합니다. no_results는 인증·입력검증 뒤 DB 없이 반환합니다. 양수재고 검색에서 no_results가 나와도 전체 재고 0이라는 뜻이 아닙니다. result 1.0 schema에 임의 top-level 관측 필드를 추가하지 않습니다.

포트·route·health의 기준은 `fashion_planner_inventory/service-manifest.json`(포트 8002)입니다.
