# stock-agent producer payload 계약 (1.0) 및 HTTP handoff (v0.1)

03 상품 재고 에이전트(`stock-agent`)의 producer 입력·결과 payload 형식은 이 레포 소유 버전 `1.0`입니다. `vogue-handoff/0.1`은 project_08 호출 경계의 공통 HTTP envelope 버전이며 payload `schema_version`과 별개입니다. Project_08의 제안 문서는 이 HTTP 계약 상태를 합의됨으로 기록하지만, 이 레포의 producer payload 스키마 변경 승인 주체와 운영 배포 승인은 별도입니다. 이 문서는 project_08 문서와 connector/core 코드 기준으로 호환 경계를 구현한 사실만 기록하며, 운영 승인이나 원격 서비스 확인을 뜻하지 않습니다.

| 파일 | 내용 |
|---|---|
| `VERSION` | 응답 `schema_version` (`1.0`) |
| `schemas/stock-agent-request.schema.json` | producer input (JSON Schema 2020-12). TrendAnalysisResult 그대로 |
| `schemas/stock-agent-result.schema.json` | producer result payload 1.0 |
| `examples/` | 요청 1개, 응답 4개 (정상, 02 no_results, 시즌 없음, LLM 미연결). 레포의 `examples/`도 02 형식 요청 |

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
- producer result 1.0에는 `findings`와 최상위 `limitations`가 없습니다. 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 있습니다. HTTP envelope는 producer result 외부를 감싸며 필드를 이동·재구성하지 않습니다.
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

## HTTP 인증·전송 및 실행 제한

- 새 `/v1/agents/stock-agent/runs`는 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 일치하는 `Authorization: Bearer <token>`을 요구합니다. 누락/오류는 401 `UNAUTHORIZED`; 토큰은 코드, 응답, 로그에 기록하지 않습니다. 토큰은 Project_08의 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 배포 비밀 저장소에서 동일하게 주입합니다.
- 두 inventory route 모두 같은 fail-closed Bearer 인증을 본문 파싱 전에 요구합니다. token 미설정/누락/오류는401이며 유효 token의 malformed JSON은400입니다.
- 운영은 TLS 종료 지점에서 HTTPS를 제공하고 backend로 전달할 때 Authorization header를 보존합니다. Project_08은 operational HTTPS + bearer를 요구합니다. loopback HTTP는 개발에서만 사용하며 Project_08의 `VOGUE_AGENT_HTTP_DEV_MODE=1`와 함께 사용합니다. 이 서버는 HTTP/HTTPS scheme을 강제하지 않으므로 운영 scheme 강제는 ingress/reverse proxy 배포 설정의 책임입니다.
- Project_08 agent 요청 timeout은 60초입니다. Stock v0.1 route도 전체 HTTP 처리에 60초 deadline을 둡니다. 기존 LLM 설정은 호출당 최대 30초, SDK 재시도 최대 2회(최악 호출 예산은 30×(1+2)=90초, 이미지 fallback 포함 시 추가 호출 가능), trend당 최대 12 tool steps이며 multi는 trend 수만큼 순차 처리하므로 원래 설정만으로는 무제한 초과할 수 있습니다. 따라서 동기 handler 전체 deadline을 60초로 제한하고 초과 시 504 `TIMEOUT` envelope를 반환합니다. Python thread의 동기 처리 cancellation은 보장되지 않아 deadline 순간 이미 실행 중인 SDK/DB 호출은 서버 쪽에서 중단되지 않을 수 있습니다. 운영에서 비용 누적 방지를 완전히 보장하려면 SDK transport timeout/retries를 남은 요청 budget 이하로 줄이고, 외부 이미지 fetch timeout 10초도 같은 deadline에 포함하도록 별도 취소 지원이나 job 모델을 도입해야 합니다. 현재 구현은 응답 상한만 보장하며 이미 시작한 blocking SDK 호출의 강제 취소까지 보장하지 않습니다.
- 각 요청은 DuckDB 공유 connection에서 요청별 `cursor()`를 만들고 닫습니다. readiness 검사는 startup에서 수행하고 health에는 저장된 검사 결과를 제공합니다. 병렬 cursor 안전성은 API 동시 요청 테스트에서 회귀 확인합니다.

## Health, API docs 및 환경 변수

- 포트 `8002`, `uvicorn stock_agent.api:app --port 8002`, Docker 명령은 README대로 유지합니다. API 시작은 R2 HEAD·기존 checksummed cache 확인만 수행하고 자동 DB 다운로드를 하지 않습니다. DuckDB는 read-only로 엽니다. 이미지 object는 R2 prefix가 설정된 경우 요청되는 이미지에 한해 지연 조회·캐시합니다. `STOCK_DB_PATH`가 설정되면 API는 R2 설정·HEAD 없이 그 로컬 파일을 read-only로 엽니다(local-file 모드, readiness `db: local-file`).
- `/health/live`는 API 생존200이며 `/health/ready`와 `/health`는 column/type, nonempty tables, sample snapshot 및 수치 검사를 통과해야200입니다. 실패는503입니다. image_objects_verified=false, llm_authenticated=null을 따로 표시합니다. llm_connected는 기존 client 구성 여부 별칭이며 원격 인증 성공이 아닙니다.
- `/docs` 및 `/openapi.json`은 레거시와 v0.1 route를 모두 제공합니다. OpenAPI version은 producer schema `1.0`; 개별 응답 producer metadata도 `1.0`, HTTP envelope는 `vogue-handoff/0.1`입니다.
- 주요 API 런타임 설정: `R2_ACCOUNT_ID`, `R2_ENDPOINT`(선택), `R2_BUCKET`, `R2_DB_KEY`, `R2_IMAGE_PREFIX`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `STOCK_CACHE_DIR`(선택), `STOCK_DB_PATH`(선택, local-file 모드), `VOGUE_AGENT_SERVICE_BEARER_TOKEN`(canonical run route 필수), `STOCK_LLM_PROVIDER`(`openai` 기본 | `policy` = 결정적 PolicyLLM), `LLM_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT`. 기본 S3 endpoint는 `https://<account-id>.r2.cloudflarestorage.com`, SDK region은 `auto`입니다. Jurisdiction bucket은 해당 공식 endpoint를 지정합니다. 토큰은 bucket 범위 `Object Read only` 권한을 사용합니다. 비밀은 `.env.example`에 빈 변수만 두며 실제 토큰/키는 배포 secret store를 사용합니다.

## 바꿀 때

스키마를 바꾸면 `examples/`, `stock_agent/schemas.py`(설명서), `tests/test_contracts.py`를 같이 고치고 `pytest`를 돌립니다. 호환되지 않는 변경이면 `VERSION`을 올리고 02·05 담당에게 알립니다.

### 기존 경계 강화 (2026-11-12)

입력 producer version은 필수 `schema_version=1.1`이며 Stock 결과1.0 및 handoff0.1과 별개입니다. 원본 입력을 deepcopy한 내부 검색 입력을 사용하고 원본 body hash를 유지합니다. canonical 요청의 outer/inner ID와 version·status를 검증하며 producer agent_id가 명시되면 Trend identity와 같아야 합니다. 전체 Trend Evidence 의미 검증은 이 adapter의 범위가 아닙니다.

readiness는 column/type·nonempty·snapshot 및 수치 null/음수/금액 일관성을 검사합니다. snapshot은2020-09-22/virtual_stock_v1/sample과 일치해야 합니다. no_results는 인증·입력검증 뒤 DB 없이 반환합니다. 양수재고 검색에서 no_results가 나와도 전체 재고0이라는 뜻이 아닙니다. result1.0 schema에 임의 top-level 관측 필드를 추가하지 않습니다.

포트/route/health의 기준은 `stock_agent/service-manifest.json`입니다. Infra/P08은 이를 소비하고 기존8769 default를8002로 변경해야 합니다. 이 저장소는 다른 owner 코드를 변경하지 않습니다.
