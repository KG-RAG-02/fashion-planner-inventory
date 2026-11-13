# stock-agent producer payload 계약 (1.0) 및 HTTP handoff (v0.1)

03 상품 재고 에이전트(`stock-agent`)의 producer 입력·결과 payload 형식은 이 레포 소유 버전 `1.0`입니다. `vogue-handoff/0.1`은 project_08 호출 경계의 공통 HTTP envelope 버전이며 payload `schema_version`과 별개입니다. Project_08의 제안 문서는 이 HTTP 계약 상태를 합의됨으로 기록하지만, 이 레포의 producer payload 스키마 변경 승인 주체와 운영 배포 승인은 별도입니다. 이 문서는 project_08 문서와 connector/core 코드 기준으로 호환 경계를 구현한 사실만 기록하며, 운영 승인이나 원격 서비스 확인을 뜻하지 않습니다.

| 파일 | 내용 |
|---|---|
| `VERSION` | 응답 `schema_version` (`1.0`) |
| `schemas/stock-agent-request.schema.json` | producer input (JSON Schema 2020-12). TrendAnalysisResult 그대로 |
| `schemas/stock-agent-result.schema.json` | producer result payload 1.0 |
| `examples/` | 요청 1개, 응답 3개 (정상, 02 no_results, 시즌 없음). 레포의 `examples/`도 02 형식 요청 |

기존 `POST /v1/inventory-products`도 canonical과 동일한 Bearer 인증을 요구합니다. Project_08용 새 경로는 `POST /v1/agents/stock-agent/runs`이며 `Authorization: Bearer ...`를 요구하고, 요청은 `{contract_version, request_id, input}` envelope입니다. `input`은 아래 producer input 그대로이며 응답은 `{contract_version, request_id, producer, status, result}`로 감싸되 `result` 전체를 보존합니다. `/docs`와 `/openapi.json`에 두 경로를 게시하며 FastAPI 422 대신 400 오류 payload를 사용합니다. `tests/test_contracts.py`는 producer payload, API 테스트는 HTTP envelope를 검사합니다.

## 흐름

```
01 사용자 쿼리 ──▶ 02 쿼리 전처리·트렌드 분석 ──(결과 그대로)──▶ 03 상품 재고 (stock-agent) ──▶ 05 새 상품 기획·최종 기획서
                                                                    04 타사판매·시장성 (앞 단계 입력 없음) ──▶ 05
```

Project_08은 직전 Trend producer의 result 객체를 `input`에 그대로 넣습니다. 03 입구에서만 내부 처리 모양으로 바꾸며(`stock_agent/trend_request.py`), 외부 input을 평탄화하거나 Trend 필드/버전을 다시 쓰지 않습니다. Project_08은 검증 뒤 변경하지 않은 Stock `result`를 다음 단계에 전달합니다.

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
| `trends[].items[].similar_products` | `kind: snap_garment`이고 `image_url`이 있는 것 중 `score`가 가장 높은 착샷 1장 → CLIP 유사도 필터 (아래) |
| `evidence`, `limitations` 등 | 쓰지 않음 |

**성별 → 고객군** (H&M 고객군)

| 성별 | 고객군 |
|---|---|
| 여 | Ladieswear, Divided |
| 남 | Menswear, Divided |
| 공용 | 전체 |

- 요청 성별이 없으면 트렌드마다 Item `gender`로 정합니다. 여러 성별이면 고객군을 합치고, `공용`이 있거나 모두 비어 있으면 전체입니다.
- 정한 결과는 `trend_results[i].search.segments`, `segment_basis`(`gender` / `item_gender` / `all`)와 limitations에 나옵니다.

**착샷 CLIP 필터**: 사용자 카테고리 아이템(없으면 아이템 전체)의 `similar_products` 중 `snap_garment` 착샷(score 최고 1장)이 있으면 항상, 재고 후보를 착샷과의 시각·스타일 유사도(CLIP ViT-B-32, 이미지 0.6 + 착샷 02 속성 텍스트 0.4)로 거릅니다. cosine 0.60 이상인 후보만 남기고, 하나도 없으면 유사도 상위 20개를 남깁니다(후보를 0개로 만들지 않음). 착샷 이미지를 열 수 없으면 텍스트만으로 계산합니다. 스냅이 없으면(`catalog_item`만) CLIP 필터 없이 찾습니다. 재고 후보 임베딩은 미리 만든 CLIP 인덱스(`STOCK_CLIP_INDEX_PATH`)에서 읽고, 인덱스가 없으면 필터 없이 찾고 그 사실을 기록합니다. 착샷 이미지는 03 서버가 직접 내려받으며 외부 서비스로 보내지 않습니다.

**사용자 색**: 트렌드 아이템 색이 비어 있으면(02가 색을 확인하지 못함) "트렌드 아이템 색이 확인되지 않아(미확인)", 색은 있는데 안 겹치면 "트렌드 아이템 색에 없습니다"로 limitations에 구분해 적습니다. 두 경우 모두 사용자 색과 어울리는 색으로 찾습니다.

입구에서 바꾼 **내부 형식**(`season`, `target_category`, `exclude_categories`, `target_colors`, `gender`, `trends`)은 코드 안에서만 쓰고 바깥에서는 받지 않습니다.

## 응답

- 색: 기준 색과 어울리는 색 중 가장 많이 겹치는 색 + 기준 색 자체(톤온톤)에서 찾습니다. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 그 색이 있습니다.
- 완화: 재고 조회(짝 영역 · 색 · 시즌 · 고객군 · 재고 > 0 · 제외 카테고리) 결과가 0개면 트렌드 색 조건만 넓혀 다시 조회합니다. 순서는 `max_overlap`(가장 많이 겹치는 색) → `any`(하나라도 어울리는 색, 결과 색이 같으면 건너뜀) → `none`(트렌드 색 조건을 풀고 21색 전체)입니다. 사용자 색(`target_colors`)과 사용자 카테고리는 완화하지 않습니다. 조회한 단계는 `search.color_attempts`(`mode`, `colors`, `n_candidates`), 최종 단계는 `search.color_mode`, 완화한 조건은 `search.relaxed`(`color_rule` max_overlap → any, 또는 `trend_colors` → `[]`)와 limitations에 있습니다.
- 상품 선택: 과잉재고 순위에서 H&M 사진이 있는 최상위 상품입니다. 1위에 사진이 없으면 사진이 있는 다음 순위를 고르고(`rank`는 실제 순위) limitations에 적습니다.
- 착샷: `search.outfit_reference`(`status` used/skipped, `skip_reason` no_snap/no_index/no_match/no_candidates, `product_id`, `image_url`, `score`, `source`, `item_id`, `item_category`), `search.clip`(`applied`, `skip_reason`, `image_used`, `n_before`, `n_after`, `n_indexed`, `threshold`, `fallback`; 착샷이 없으면 `null`). CLIP 필터를 걸었으면 근거 `E-006`(`locator` = 착샷 URL)이 붙습니다.

- `products[i]` ↔ `trend_results[i]`. 트렌드마다 상품 1개(사진 포함). 못 고르면 `null`.
- producer result 1.0에는 `findings`와 최상위 `limitations`가 없습니다. 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 있습니다. HTTP envelope는 producer result 외부를 감싸며 필드를 이동·재구성하지 않습니다.
- 근거는 `evidence` 한 곳에 모으고, `trend_results[i].evidence_ids`가 그 트렌드의 근거 ID를 가리킵니다. ID 형식은 `stock-agent:<style.id>:E-001`.
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

- 새 `/v1/agents/stock-agent/runs`는 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 일치하는 `Authorization: Bearer <token>`을 요구합니다. 누락/오류는 401 `UNAUTHORIZED`; 토큰은 코드, 응답, 로그에 기록하지 않습니다. 토큰은 Project_08의 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 배포 비밀 저장소에서 동일하게 주입합니다.
- 두 inventory route 모두 같은 fail-closed Bearer 인증을 본문 파싱 전에 요구합니다. token 미설정/누락/오류는401이며 유효 token의 malformed JSON은400입니다.
- 운영은 TLS 종료 지점에서 HTTPS를 제공하고 backend로 전달할 때 Authorization header를 보존합니다. Project_08은 operational HTTPS + bearer를 요구합니다. loopback HTTP는 개발에서만 사용하며 Project_08의 `VOGUE_AGENT_HTTP_DEV_MODE=1`와 함께 사용합니다. 이 서버는 HTTP/HTTPS scheme을 강제하지 않으므로 운영 scheme 강제는 ingress/reverse proxy 배포 설정의 책임입니다.
- Project_08 agent 요청 timeout은 60초입니다. Stock v0.1 route도 전체 HTTP 처리에 기본 60초 deadline(`STOCK_HANDOFF_TIMEOUT_SECONDS`, 0 초과 600 이하)을 두고, 초과하면 504 `TIMEOUT`(`reason: timeout`) envelope를 반환합니다. 트렌드는 순서대로 처리하며, 트렌드마다 DB 조회와 착샷이 있으면 착샷 이미지 내려받기(요청당 최대 5초)·CLIP 임베딩 계산이 있습니다. 트렌드가 많은 호출자는 deadline을 늘리고 자기 read timeout을 그보다 길게 둡니다. Python thread의 동기 처리는 취소가 보장되지 않아, deadline 순간 이미 실행 중인 DB 조회·이미지 내려받기는 서버 쪽에서 끝까지 실행될 수 있습니다. 현재 구현은 응답 상한만 보장합니다.
- 각 요청은 DuckDB 공유 connection에서 요청별 `cursor()`를 만들고 닫습니다. readiness 검사는 startup에서 수행하고 health에는 저장된 검사 결과를 제공합니다. 병렬 cursor 안전성은 API 동시 요청 테스트에서 회귀 확인합니다.

## Health, API docs 및 환경 변수

- 포트 `8002`, `uvicorn stock_agent.api:app --port 8002`, Docker 명령은 README대로 유지합니다. API 시작은 R2 HEAD·기존 checksummed cache 확인만 수행하고 자동 DB 다운로드를 하지 않습니다. DuckDB는 read-only로 엽니다. 이미지 object는 R2 prefix가 설정된 경우 요청되는 이미지에 한해 지연 조회·캐시합니다. `STOCK_DB_PATH`가 설정되면 API는 R2 설정·HEAD 없이 그 로컬 파일을 read-only로 엽니다(local-file 모드, readiness `db: local-file`).
- `/health/live`는 API 생존 200이며 `/health/ready`와 `/health`는 column/type, nonempty tables, sample snapshot 및 수치 검사를 통과해야 200입니다. 실패는 503입니다. `image_objects_verified=false`를 따로 표시합니다. `clip_index_loaded`·`clip_n_indexed`는 서버 시작 때 CLIP 인덱스를 불러왔는지와 상품 수이며, 200/503 판정에는 쓰지 않습니다(인덱스가 없으면 착샷 CLIP 필터만 건너뜀).
- `/docs` 및 `/openapi.json`은 레거시와 v0.1 route를 모두 제공합니다. OpenAPI version은 producer schema `1.0`; 개별 응답 producer metadata도 `1.0`, HTTP envelope는 `vogue-handoff/0.1`입니다.
- 주요 API 런타임 설정: `R2_ACCOUNT_ID`, `R2_ENDPOINT`(선택), `R2_BUCKET`, `R2_DB_KEY`, `R2_IMAGE_PREFIX`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `STOCK_CACHE_DIR`(선택), `STOCK_DB_PATH`(선택, local-file 모드), `VOGUE_AGENT_SERVICE_BEARER_TOKEN`(canonical run route 필수), `STOCK_CLIP_INDEX_PATH`(선택, 기본 `data/clip_index.npz`, Docker `/data/clip_index.npz`), `STOCK_HANDOFF_TIMEOUT_SECONDS`(선택, 기본 60). 기본 S3 endpoint는 `https://<account-id>.r2.cloudflarestorage.com`, SDK region은 `auto`입니다. Jurisdiction bucket은 해당 공식 endpoint를 지정합니다. 토큰은 bucket 범위 `Object Read only` 권한을 사용합니다. 비밀은 `.env.example`에 빈 변수만 두며 실제 토큰/키는 배포 secret store를 사용합니다.

## 바꿀 때

스키마를 바꾸면 `examples/`, `stock_agent/schemas.py`(설명서), `tests/test_contracts.py`를 같이 고치고 `pytest`를 돌립니다. 호환되지 않는 변경이면 `VERSION`을 올리고 02·05 담당에게 알립니다.

### 기존 경계 강화 (2026-11-12)

입력 producer version은 필수 `schema_version=1.1`이며 Stock 결과1.0 및 handoff0.1과 별개입니다. 원본 입력을 deepcopy한 내부 검색 입력을 사용하고 원본 body hash를 유지합니다. canonical 요청의 outer/inner ID와 version·status를 검증하며 producer agent_id가 명시되면 Trend identity와 같아야 합니다. 전체 Trend Evidence 의미 검증은 이 adapter의 범위가 아닙니다.

readiness는 column/type·nonempty·snapshot 및 수치 null/음수/금액 일관성을 검사합니다. snapshot은2020-09-22/virtual_stock_v1/sample과 일치해야 합니다. no_results는 인증·입력검증 뒤 DB 없이 반환합니다. 양수재고 검색에서 no_results가 나와도 전체 재고0이라는 뜻이 아닙니다. result1.0 schema에 임의 top-level 관측 필드를 추가하지 않습니다.

포트/route/health의 기준은 `stock_agent/service-manifest.json`입니다. Infra/P08은 이를 소비하고 기존8769 default를8002로 변경해야 합니다. 이 저장소는 다른 owner 코드를 변경하지 않습니다.
