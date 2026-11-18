# inventory 계약

inventory가 받는 입력과 돌려주는 결과의 형식입니다. 서비스 사이의 전체 계약(다른 서비스 포함)은 [fashion-planner-orchestrator](https://github.com/KG-RAG-02/fashion-planner-orchestrator)의 `contracts/workflow/`가 기준이며, 그쪽의 inventory 결과 스키마는 이 레포의 `schemas/inventory-result.schema.json`을 옮긴 것입니다.

| 경로 | 내용 |
|---|---|
| `schemas/inventory-request.schema.json` | 입력: trend 결과(TrendAnalysisResult) 그대로. JSON Schema 2020-12 |
| `schemas/inventory-result.schema.json` | 결과 payload. envelope의 `result`에 그대로 들어감 |
| `examples/request-trend-result.json` | 입력 예시. `fashion_planner_inventory.schemas.TREND_RESULT_EXAMPLE`과 같음(테스트가 대조) |
| `examples/response-*.json` | 결과 예시 3개(정상, upstream no_results, 시즌 없음 400). 합성 fixture DB로 `scripts/build_contract_examples.py`가 만든다 |
| `VERSION` | 결과 payload의 `schema_version`(`1.0.0`) |

`$id`는 `https://contracts.fashion-planner.invalid/inventory/<파일 이름>`입니다. `.invalid`는 해석되지 않는 예약 TLD라 실제로 받아 오지 않습니다.

## 전송(HTTP)

```
POST /v1/agents/inventory/runs
Authorization: Bearer <FASHION_PLANNER_SERVICE_TOKEN>
Content-Type: application/json

{"contract_version": "inventory-handoff", "request_id": "<비어 있지 않은 문자열>", "input": <trend 결과>}
```

응답 envelope:

```json
{"contract_version": "inventory-handoff", "request_id": "<같은 값>",
 "producer": {"agent_id": "inventory", "schema_version": "1.0.0"},
 "status": "<result.status와 같음>", "result": { "...": "inventory 결과 payload" }}
```

- 인증은 본문을 읽기 전에 확인합니다. 토큰이 설정되지 않았거나 헤더가 없거나 다르면 401 `UNAUTHORIZED`입니다. 토큰은 응답·로그에 남기지 않습니다.
- 본문은 중복 key와 NaN·Infinity가 없는 JSON이어야 합니다(400).
- `request_id`와 `input.request_id`는 비어 있지 않고 같아야 합니다. 다르면 바깥 `request_id`로 400을 돌려줍니다.
- `contract_version`이 `inventory-handoff`가 아니거나 envelope 필드가 빠졌으면 400 `INVALID_REQUEST`(`reason: invalid_input`)입니다. FastAPI 기본 422 대신 400을 씁니다.
- 요청 전체에 제한 시간(`INVENTORY_HANDOFF_TIMEOUT_SECONDS`, 기본 60초, 통합 배포 180초)이 있고, 넘으면 504 `TIMEOUT` envelope입니다. 이미 시작한 동기 DB 조회·이미지 내려받기는 서버에서 끝까지 돌 수 있습니다. 호출자의 읽기 제한 시간은 이보다 길게 둡니다(orchestrator 210초).
- 운영에서는 TLS를 앞단에서 끝내고 `Authorization` 헤더를 그대로 넘깁니다. loopback HTTP는 개발용입니다(orchestrator의 `FASHION_PLANNER_HTTP_DEV_MODE=1`).

## 입력에서 쓰는 값

입력은 trend 결과 그대로이며 `schema_version` `1.0.0`이어야 합니다. 생산자 `agent_id`가 있으면 `trend`여야 합니다. 여기 없는 필드는 쓰지 않아도 그대로 받아 보존합니다.

| trend 결과 필드 | 쓰는 곳 |
|---|---|
| `request_id` | 그대로 돌려줌 |
| `status` | `ok` → 처리, `no_results` → DB 조회 없이 `no_results`, `error` → 400 `INVALID_REQUEST`(`reason: upstream_error`) |
| `applied_filters.season` | 요청 시즌(필수). 없으면 400 `missing_fields` |
| `applied_filters.categories.include`·`exclude` | 사용자 카테고리·제외 카테고리 |
| `applied_filters.attributes` 중 색 | 사용자 색(21색) |
| `applied_filters.gender` | 고객군 |
| `trends[]` | 트렌드별 카테고리·색, `items[].similar_products`의 CLIP 기준 이미지 |

규칙은 [docs/ranking-rules.md](../docs/ranking-rules.md)에 있습니다.

## 결과 payload

| 필드 | 설명 |
|---|---|
| `schema_version` | `1.0.0` |
| `request_id`, `agent_id`(`inventory`), `status`, `summary` | `status`: 모두 ok → `ok`, 일부만 상품 있음 → `partial`, 모두 오류 → `error`, 상품 0개 → `no_results` |
| `products` | 트렌드 순서대로 n개. 상품을 못 고른 트렌드는 `null`. 같은 상품이 여러 번 나올 수 있음 |
| `trend_results` | 트렌드 순서대로 n개. `trend_results[i]` ↔ `products[i]`(`product_index`). `style`, `status`, `rank`, `reason`, `summary`, `attribute_summary`, `search`, `limitations`, `errors`, `evidence_ids` |
| `season_basis` | 요청 시즌과 대신 쓴 과거 기간 |
| `evidence` | 트렌드마다 E-001 가상 재고(`source_id: virtual_stock`), E-002·E-003 판매, E-004 상품 속성, E-005 상품 사진(있을 때), E-006 CLIP 기준 이미지(썼을 때). `evidence_id`는 `inventory:<style.id>:E-00n`, `manifest_id`는 계산에 쓴 DB 버전(E-006은 `null`) |
| `errors` | 공통 입력 오류, 또는 모든 트렌드가 실패했을 때 한 줄 |
| `input_provenance` | `source_agent_id: trend`, `source_schema_version`, `adapter_version: inventory-trend-input-adapter`, `source_payload_sha256`(원본 trend 결과 본문의 python-json-sort 해시) |

`products[].inventory`는 `quantity`, `unit: pieces`, `as_of: 2020-09-22`, `definition: virtual_stock_from_hm_sales`, `data_nature: sample`입니다. 재고와 판매가는 합성 값입니다([데이터 카드](../docs/data.md)).

## 오류 코드

`errors[].code`는 서비스 공통 코드이고, 자세한 원인은 `reason`에 있습니다(`fashion_planner_inventory/errors.py`).

| `reason` | `code` | HTTP |
|---|---|---|
| `missing_fields`, `invalid_input`, `schema_version_mismatch`, `upstream_error` | `INVALID_REQUEST` | 400 |
| (인증 실패) | `UNAUTHORIZED` | 401 |
| `db_unavailable`, `image_source_unavailable` | `SOURCE_UNAVAILABLE` | 503 |
| `timeout` | `TIMEOUT` | 504 |
| `invalid_response`, `internal` | `INTERNAL_ERROR` | 500 |
| `trend_failed`(최근 추세만 실패, 나머지 지표로 순위) | `INTERNAL_ERROR` | 200(`status: partial`) |

HTTP 오류 번호는 결과 `status`가 `error`일 때만 붙습니다. 일부 트렌드만 실패하면 200이고 오류는 `trend_results[i].errors`에 있습니다. 모든 트렌드가 서로 다른 원인으로 실패하면 `SOURCE_UNAVAILABLE` → `TIMEOUT` → `INTERNAL_ERROR` → `INVALID_REQUEST` 순으로 하나를 고르고 `reason: mixed`입니다.

## 바꿀 때

- 스키마를 바꾸면 `fashion_planner_inventory/schemas.py`(OpenAPI 문서 모델)와 테스트를 함께 고치고, `python scripts/build_contract_examples.py`로 예시를 다시 만든 뒤 `python -m pytest`를 돌립니다.
- 호환되지 않는 변경이면 `VERSION`과 `schema_version`을 올리고, orchestrator 레포의 계약 사본도 같이 고칩니다. 이름에는 버전을 넣지 않습니다.
- 포트(8002)·경로·health 경로의 기준은 `fashion_planner_inventory/service-manifest.json`입니다.
