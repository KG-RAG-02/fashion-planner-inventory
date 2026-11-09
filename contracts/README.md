# stock-agent 계약 (초안)

03 상품 재고 에이전트(`stock-agent`)의 요청·응답 형식입니다. **팀과 합의하기 전 초안**이고, 다른 에이전트 레포와 맞춘 공통 형식이 아닙니다. 팀 공통 형식(Notion)은 초안으로 두고 각 에이전트가 필요에 맞게 바꾸기로 했기 때문에, 이 파일은 03 코드가 실제로 주고받는 형식을 적었습니다.

| 파일 | 내용 |
|---|---|
| `VERSION` | 응답 `schema_version` (`1.0`) |
| `schemas/stock-agent-request.schema.json` | 요청 (JSON Schema 2020-12). 02 트렌드 결과 그대로 |
| `schemas/stock-agent-result.schema.json` | 응답 |
| `examples/` | 요청 1개, 응답 4개 (정상, 02 no_results, 시즌 없음, LLM 미연결). 레포의 `examples/`도 02 형식 요청 |

API 경로는 `POST /v1/inventory-products`이고, 서버를 띄우면 `/docs`와 `/openapi.json`에서 같은 형식을 볼 수 있습니다. 팀 경로·포트가 정해지면 `openapi/`를 추가합니다. `tests/test_contracts.py`가 예시와 실제 응답이 스키마에 맞는지 검사합니다.

## 흐름

```
01 사용자 쿼리 ──▶ 02 쿼리 전처리·트렌드 분석 ──(결과 그대로)──▶ 03 상품 재고 (stock-agent) ──▶ 05 새 상품 기획·최종 기획서
                                                                    04 타사판매·시장성 (앞 단계 입력 없음) ──▶ 05
```

오케스트레이터는 없습니다. 02가 자기 결과를 그대로 03에 보내고, 03이 입구에서 내부 형식으로 바꿉니다(`stock_agent/trend_request.py`). 03 결과는 05 새 상품 기획·최종 기획서가 씁니다. 누가 05에 넘기는지는 아직 정하지 않았습니다. 04 타사판매·시장성은 앞 단계 입력을 받지 않습니다.

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
| `trends[].items[].similar_products` | `kind: snap_garment`이고 `image_url`이 있는 것 중 `score`가 가장 높은 착샷 1장 (아래) |
| `evidence`, `limitations` 등 | 쓰지 않음 (LLM에도 보내지 않음) |

**성별 → 고객군** (H&M 고객군)

| 성별 | 고객군 |
|---|---|
| 여 | Ladieswear, Divided |
| 남 | Menswear, Divided |
| 공용 | 전체 |

- 요청 성별이 없으면 트렌드마다 Item `gender`로 정합니다. 여러 성별이면 고객군을 합치고, `공용`이 있거나 모두 비어 있으면 전체입니다.
- 정한 결과는 `trend_results[i].search.segments`, `segment_basis`(`gender` / `item_gender` / `all`)와 limitations에 나옵니다.

**착샷 소재·핏** (2026-11-09): 사용자 카테고리 아이템(없으면 아이템 전체)의 `similar_products` 중 `snap_garment` 착샷(score 최고 1장)을 03 LLM이 보고, 모델이 함께 입은 하의·아우터 등의 소재·핏을 뽑습니다. 착샷에 없는 짝 영역은 빼고, 재고는 A(소재·핏 일치) → B(소재 일치) → C(카테고리·색만) 순으로 단계를 정해 그 안에서 과잉재고 순위로 고릅니다. 스냅이 없거나(`catalog_item`만), 아이템만 잘라낸 사진이거나, 이미지를 열 수 없으면 소재·핏 조건 없이 찾고 limitations에 적습니다. 02 `image_url`은 OpenAI로 전송됩니다(팀 확인 완료).

**사용자 색**: 트렌드 아이템 색이 비어 있으면(02가 색을 확인하지 못함) "트렌드 아이템 색이 확인되지 않아(미확인)", 색은 있는데 안 겹치면 "트렌드 아이템 색에 없습니다"로 limitations에 구분해 적습니다. 두 경우 모두 사용자 색과 어울리는 색으로 찾습니다.

입구에서 바꾼 **내부 형식**(`season`, `target_category`, `exclude_categories`, `target_colors`, `gender`, `trends`)은 코드 안에서만 쓰고 바깥에서는 받지 않습니다.

## 응답

- 색: 기준 색과 어울리는 색 + 기준 색 자체(톤온톤)에서 찾습니다. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 그 색이 있습니다.
- LLM은 짝 영역·찾을 색을 일부만 골라 조회할 수 없습니다(어울리는 색과 톤온톤을 함께 봄). 좁히려는 호출은 실행되지 않고 다시 고르게 합니다.
- LLM이 규칙에 맞지 않게 부른 도구 호출도 `search.steps`에 `error`(`reason`, `message`)와 함께 남습니다.
- 상품 선택: 순위는 과잉재고 순서라 기본은 1위입니다. LLM이 1위가 아닌 상품을 고르면 더 높은 순위 대신 고른 이유가 `trend_results[i].reason.llm_why_not_higher`에 있습니다(1위면 `null`).
- 착샷: `search.outfit_reference`(`status` used/skipped/failed, `skip_reason` no_snap/not_full_outfit/image_error/no_vision, `product_id`, `image_url`, `score`, `garments` 영역별 `visible`·`materials`·`fit`(LLM 이미지 판단), `areas_removed`), `search.attribute_tier`(`tier` A/B/C, `counts`, `relaxed` fit/materials, 착샷 조건이 없으면 null). 상품별 비교는 `reason.attributes`(`outfit_materials`, `outfit_fit`, `materials_match`·`fit_match` true/false/null=미확인, `tier`). 착샷을 쓰면 근거 `E-006`(`locator` = 착샷 URL)이 붙습니다.

- `products[i]` ↔ `trend_results[i]`. 트렌드마다 상품 1개(사진 포함). 못 고르면 `null`.
- **팀 공통 envelope의 `findings`와 최상위 `limitations`는 쓰지 않습니다.** 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 있습니다.
- 근거는 `evidence` 한 곳에 모으고, `trend_results[i].evidence_ids`가 그 트렌드의 근거 ID를 가리킵니다. ID 형식은 `stock-agent:<style.id>:E-001`.
- `status`: 모두 ok → `ok`, 일부만 상품 있음 → `partial`, 모두 오류 → `error`, 그 밖 → `no_results`.

## 오류 코드

`errors[].code`는 팀 공통 코드, 03 안의 자세한 원인은 `reason`에 있습니다.

| reason | code | HTTP |
|---|---|---|
| `missing_fields`, `invalid_input`, `schema_version_mismatch`, `upstream_error` | `INVALID_REQUEST` | 400 |
| `db_unavailable`, `no_llm`, `llm_unavailable` | `SOURCE_UNAVAILABLE` | 503 |
| `timeout` | `TIMEOUT` | 504 |
| `invalid_response`, `rule_violation`, `step_limit`, `invalid_final`, `internal` | `INTERNAL_ERROR` | 500 |
| `trend_failed` (최근 추세만 실패, 나머지 지표로 순위) | `INTERNAL_ERROR` | 200 (`status: partial`) |

HTTP 번호는 응답 `status`가 `error`일 때만 붙습니다. 트렌드 중 일부만 실패하면 200이고, 실패한 트렌드의 오류는 `trend_results[i].errors`에 있습니다. 모든 트렌드가 서로 다른 원인으로 실패하면 서버 쪽 코드(SOURCE_UNAVAILABLE → TIMEOUT → INTERNAL_ERROR → INVALID_REQUEST 순)를 쓰고 `reason`은 `mixed`입니다.

## 바꿀 때

스키마를 바꾸면 `examples/`, `stock_agent/schemas.py`(설명서), `tests/test_contracts.py`를 같이 고치고 `pytest`를 돌립니다. 호환되지 않는 변경이면 `VERSION`을 올리고 02·05 담당에게 알립니다.
