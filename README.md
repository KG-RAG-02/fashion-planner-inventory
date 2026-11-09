# stock-agent (과잉재고 에이전트)

VOGUE 기획 파이프라인의 **03 상품 재고** 에이전트. 02 트렌드마다 **LLM agent**가 도구(짝 카테고리·어울리는 색·재고 조회·소재/핏 거르기·순위)를
골라 부르며 조건을 정하고, 과잉재고 중 **트렌드마다 상품 1개(사진 포함)**와 상위 50개 속성 요약을 반환한다.

```
01 사용자 쿼리 ──▶ 02 쿼리 전처리·트렌드 분석 ──(결과 그대로)──▶ 03 상품 재고 (stock-agent) ──▶ 05 새 상품 기획·최종 기획서
                                                                    04 타사판매·시장성 (앞 단계 입력 없음) ──▶ 05
```

단계는 01~05이고 에이전트는 02~05의 4개다. 오케스트레이터는 없다. 02가 자기 결과(`TrendAnalysisResult`)를 그대로 03에 보낸다(push).
04는 앞 단계 입력 없이 타사 판매 자료로 원가·마진·타사 평균가를 만든다. 03 결과는 04를 거치지 않고 05로 가며,
누가 05에 넘기는지(02가 받은 응답을 넘기는지, 03이 직접 부르는지)는 아직 정하지 않았다. 요청·응답 형식은 [`contracts/`](contracts/README.md)(초안).

- LLM은 OpenAI `gpt-4o-mini`(`stock_agent/llm_openai.py`). agent는 교체 가능한 인터페이스(`stock_agent/llm.py`)만 알고, 키가 없으면 트렌드마다 `SOURCE_UNAVAILABLE`(`reason: no_llm`) 오류
- OpenAI로 트렌드 정의·설명과 H&M 후보 상품 정보(상품 ID·카테고리·색·재고·판매 수치)가 전송된다(팀 확인 완료, 재고 수치는 가상 데이터)
- LLM이 실패하면 규칙 방식으로 대신 내지 않고 오류로 돌려준다(평가할 수 있게)
- 규칙 방식(`mode="rule"`)은 비교 기준선·정확도 검증용으로 남겨 둔다

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

## 2. LLM 설정 (OpenAI)

`.env.example`을 복사해 `.env`를 만들고 `LLM_API_KEY`에 OpenAI 키를 넣는다. `.env`는 커밋하지 않는다(`.gitignore`).

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `LLM_API_KEY` | 없음 | OpenAI 키. 없으면 LLM 연결 전으로 보고 `no_llm` 오류 |
| `LLM_MODEL` | `gpt-4o-mini` | 모델 |
| `LLM_TIMEOUT` | `30` | 호출 1번당 시간 제한(초). 넘으면 `TIMEOUT`(504) |

- 로컬 실행은 현재 폴더의 `.env`를 읽는다(이미 있는 환경변수는 덮어쓰지 않음). Docker는 `--env-file .env`로 넘긴다.
- 호출 설정: `temperature 0`, 매 차례 도구 1개(`tool_choice=required`, `parallel_tool_calls=false`), 연결 오류·429·5xx는 2번 다시 시도. 트렌드 1개당 최대 12번 호출.
- `/health`의 `llm_connected`, `llm_model`, `llm_error`로 연결 상태를 본다. 키가 맞는지는 실제 요청에서 알 수 있다(틀리면 `SOURCE_UNAVAILABLE`, `reason: llm_unavailable`).

## 3. 로컬 실행

```powershell
python scripts/run_agent.py --request examples/request-example.json   # 02 결과 1건 (LLM agent, 키가 없으면 no_llm 오류)
python scripts/run_agent.py --policy                                  # agent 구조 점검 (PolicyLLM: 정해진 순서로 도구를 부르는 가짜 LLM)
python scripts/run_agent.py --mode rule                               # 규칙 방식 (기준선)
python scripts/validate_requests.py                                   # 규칙 방식 정확도 검증 10건 (성별 여·남·공용·없음)
uvicorn stock_agent.api:app --port 8002                                 # HTTP API
```

## 4. Docker 실행

```powershell
docker build -t stock-agent:v1 .
docker run --rm -p 8002:8002 --env-file .env -v "${PWD}/data:/data:ro" stock-agent:v1
```

- `data/stock.duckdb`와 `data/images/`를 컨테이너의 `/data`에 읽기 전용(`:ro`)으로 연결한다.
- `--env-file .env`로 LLM 키를 넘긴다. 이미지 안에는 `.env`가 들어가지 않는다(`stock_agent/`만 복사).
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
| GET | `/health` | DB 연결과 필수 테이블(articles, transactions, article_attrs, stock_v1) 확인, `llm_connected` |
| POST | `/v1/inventory-products` | 요청 1건 → 응답. `status`가 error일 때만 오류 번호(아래 오류 코드 표), 그 외 200 (`status`로 ok/partial/no_results 구분) |

### 요청

**요청은 02 트렌드 결과(`TrendAnalysisResult`) 그대로만 받는다**(예시: `examples/request-example.json`, 트렌드 여러 개: `examples/request-trends-example.json`).
`applied_filters`가 없으면 400 `INVALID_REQUEST`. 입구(`multi.run_request` → `stock_agent/trend_request.py`)에서 내부 형식으로 바꾼다.

| 02 필드 | 03에서 |
|---|---|
| `request_id` | 그대로 돌려줌 |
| `status` | `ok` → 처리, `no_results` → DB 조회 없이 `no_results`, `error` → `INVALID_REQUEST`(`reason: upstream_error`) |
| `applied_filters.season` | `season` |
| `applied_filters.categories.include` / `exclude` | `target_category` / `exclude_categories` (`minor`, 없으면 `major`) |
| `applied_filters.attributes` 중 color | `target_colors` (소재·핏 등 다른 사용자 속성은 쓰지 않음) |
| `applied_filters.gender` | `gender` → 고객군 |
| `trends` | 그대로. `similar_products`·`evidence_ids`·`image_urls` 등은 쓰지 않고 LLM에도 보내지 않음 |

**성별 → 고객군**: `여` → Ladieswear + Divided, `남` → Menswear + Divided, `공용` → 전체.
요청 성별이 없으면 트렌드마다 Item `gender`로 정한다(여러 성별이면 합치고, `공용`이 있거나 모두 비면 전체).
결과는 `search.segments`·`segment_basis`와 limitations에 나온다.

**내부 형식** (02 결과를 바꾼 것. 바깥에서는 받지 않고, 안쪽 함수 `multi.run_internal`·`agent.run`·`llm_agent.run_agent`와 그 테스트가 쓴다):

| 필드 | 설명 |
|---|---|
| `request_id` | 필수 |
| `season` | 필수. SeasonRequest `{"year": 2027, "season": "SS", "origin": "user_requested"}`. `origin`이 `default_next`면 출처 inferred |
| `target_category` | 사용자 기획 카테고리(영역 또는 세부 카테고리) 목록. 있으면 이것의 짝을 찾음 |
| `exclude_categories` | 제외 카테고리. 영역이면 짝 영역에서 빼고, 세부 카테고리면 그 상품을 후보에서 뺌. 모르는 값이거나 `target_category`와 겹치면 400 |
| `target_colors` | 사용자 색, 21색 이름. 21색이 아니면 400 |
| `gender` | 남/여/공용 |
| `segment` | 고객군을 직접 줄 때만(안쪽 테스트·검증용). 있으면 `gender`보다 먼저 |
| `trends` / `trend` | 트렌드 목록(`run_internal`) / 트렌드 1개(`agent.run`, `run_agent`) |

트렌드 1개를 처리하는 규칙 (`stock_agent/trend_input.py`, `stock_agent/agent.py`):

- 트렌드 카테고리: 아이템의 `category_minor`, 없으면 `category_major`
- 트렌드 소재·핏: vocabulary_v1에 없는 값과 Item `approx`에 든 값(근사 매핑)은 쓰지 않고 limitations에 기록
- 기준 색 (사용자 쿼리가 가장 중요해서 사용자 색이 먼저)
  - 사용자 색(`target_colors`)이 있으면 트렌드 아이템 **전체**의 색과 비교: 겹치는 사용자 색이 있으면 그 색만(안 겹치는 사용자 색은 빼고 limitations에 기록), 하나도 안 겹치면 사용자 색 전부(limitations에 "트렌드 아이템 색에 없음" 기록)
  - 없으면 트렌드 색: 사용자 카테고리 아이템의 색 → 없으면 아이템 전체의 색 → 없으면 색 조건 없이 21색 전체. 개수 제한 없음
- 짝 카테고리: 사용자 카테고리의 짝 → 없으면 트렌드 카테고리 짝의 교집합(없으면 트렌드 카테고리와 그 짝 전체) → 둘 다 없으면 네 영역 전체
- 후보 색: 21색마다 기준 색 몇 개와 어울리는지(조합표 v1) 세어, 기준 색과 어울리는 색 중 **가장 많이 겹치는 색** + **기준 색 자체(톤온톤)**. 동점이면 모두. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 적음
- 카테고리와 색이 모두 없는 트렌드는 그 트렌드만 오류
- 사용자 카테고리가 여러 개면 각 짝의 교집합(없으면 합집합). 제외 카테고리는 짝 영역·후보에서 뺀다

LLM agent (`stock_agent/llm_agent.py`): 위 규칙으로 정한 기준 색·카테고리에서 시작해, LLM이 도구를 골라 부르고 결과를 보고 다음 행동을 정한다.

| 도구 | 하는 일 |
|---|---|
| `find_categories` | 짝 영역 정하기. 사용자 카테고리 여러 개의 교집합/합집합 선택. 제외 영역은 자동으로 빠짐 |
| `find_colors` | 기준 색과 어울리는 색 + 기준 색 자체(톤온톤). `max_overlap`(가장 많이 겹치는 색) 또는 `any`(하나라도 어울리는 색) |
| `get_inventory` | 영역·색 조건으로 재고 후보 조회, 후보 수와 속성 분포 |
| `filter_attributes` | 소재·핏으로 거르기. 트렌드 아이템에 있으면 그 안에서, 없으면 LLM이 트렌드 설명을 보고 추론. 정보가 없는 상품(미확인)은 남기고 정보가 있는데 다른 상품만 뺌(일치·미확인 수를 돌려줌) |
| `rank_candidates` | 과잉재고 순위 상위 몇 개 |
| `finish` | LLM이 고른 상품과 이유를 받아 검사하고 끝냄. 1위가 아닌 상품이면 `why_not_higher`(더 높은 순위 대신 고른 이유)가 필수. 응답은 기존 코드로 `product`·`trend_result`를 만듦 |

- 순위는 과잉재고 순서라 기본은 1위다. LLM이 1위가 아닌 상품을 고르면 더 높은 순위 상품과 실제로 다른 점(사진 없음, 트렌드 소재·핏 일치 여부 등)을 `why_not_higher`에 써야 하고, 없으면 `invalid_final`. 이유는 `reason.llm_why_not_higher`에 남는다(1위면 `null`)

- 규칙에 맞지 않는 호출(`rule_violation`, `invalid_final`)은 실행하지 않고 오류를 LLM에게 돌려줘 다시 고르게 한다. 트렌드당 2번까지, 3번째 위반에서 실패. 막힌 호출도 `search.steps`에 `error`(`reason`, `message`)와 함께 남고, 다시 고른 경우 limitations에 적는다
- LLM이 `get_inventory`에서 짝 영역·찾을 색을 일부만 고르면(좁히면) `search.narrowed`와 limitations에 남기고, summary는 실제로 쓴 색 기준으로 쓴다. 짝 영역 안의 영역 차이는 `why_not_higher`의 이유가 될 수 없다고 LLM에게 알린다
- 후보가 0개면 **트렌드 쪽 조건만** 완화(트렌드 색·트렌드 카테고리·소재·핏·색 규칙). 사용자 조건(`target_category`, `target_colors`)은 완화하지 않음
- LLM이 규칙을 어기거나 실패하면 그 트렌드는 오류(`product: null`). 원인 `reason`: `no_llm`, `llm_unavailable`(키·권한·모델 이름·네트워크), `timeout`, `invalid_response`(도구를 안 부름·없는 도구·인자 오류), `rule_violation`(사용자 조건 변경, 제외 카테고리 다시 넣기, 21색·어휘 밖 값, 도구 순서), `step_limit`(도구 12번 초과), `invalid_final`(순위 결과에 없는 상품, 후보가 있는데 안 고름, 1위가 아닌데 `why_not_higher` 없음, 이유에 근거 없는 숫자)
- 응답 `search`에 `mode: "llm"`, `steps`(부른 도구·조건·후보 수), `relaxed`(완화한 조건, 무엇을·무엇에서·무엇으로·왜), `trend_materials`·`trend_fit`·`trend_attributes_source`(`trend_item`/`llm_inferred`), `attribute_filter`(거른 소재·핏, `counts`: 소재·핏별 일치·미확인 수), `narrowed`(LLM이 앞 도구 결과보다 좁혀 쓴 영역·색, 무엇에서·무엇으로·왜, 톤온톤만인지). `reason`에 `attributes`(소재·핏 맞음, `materials_match`·`fit_match`: true/false/null=미확인), `decided_by: "llm"`, `llm_reason`, `llm_why_not_higher`

### 응답

`stock_agent/multi.py`가 트렌드마다 LLM agent(`mode="rule"`이면 `agent.run`)를 돌려 순서대로 이어 붙인다.
**팀 공통 envelope의 `findings`와 최상위 `limitations`는 쓰지 않는다.** 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 담는다
(팀 공통 형식은 초안이고 에이전트마다 맞게 바꾸기로 함. 02도 envelope를 쓰지 않음).

| 필드 | 설명 |
|---|---|
| `products` | 트렌드 순서대로 n개. 상품 정보만(`style_id`, `sku_ids`, `category`, `season`, `attributes`, `inventory`, `image`). 상품을 못 고른 트렌드는 `null`. 같은 상품이 여러 번 나올 수 있음 |
| `trend_results` | n개. `trend_results[i]` ↔ `products[i]` (`product_index`). `style`, `status`, `rank`, `reason`(근거 숫자), `summary`(추천 이유 한 줄), `attribute_summary`, `search`(찾은 조건), `limitations`, `errors`, `evidence_ids`(이 트렌드의 근거 ID) |
| `evidence` | 트렌드마다 데이터 출처 E-001~E-005. `evidence_id` = `stock-agent:<style.id>:E-001`, `style_id`. 근거는 여기 한 곳에 모으고 `evidence_ids`로 가리킴 |
| `status` | 모두 ok → ok / 일부만 상품 있음 → partial / 모두 오류 → error / 상품 0개 → no_results |

### 오류 코드

`errors[].code`는 팀 공통 코드, 03 안의 자세한 원인은 `reason` (`stock_agent/errors.py`).

| reason | code | HTTP |
|---|---|---|
| `missing_fields`, `invalid_input`, `schema_version_mismatch`, `upstream_error` | `INVALID_REQUEST` | 400 |
| `db_unavailable`, `no_llm`, `llm_unavailable` | `SOURCE_UNAVAILABLE` | 503 |
| `timeout` | `TIMEOUT` | 504 |
| `invalid_response`, `rule_violation`, `step_limit`, `invalid_final`, `internal` | `INTERNAL_ERROR` | 500 |
| `trend_failed` (최근 추세만 실패, 나머지 지표로 순위) | `INTERNAL_ERROR` | 200 (`status: partial`) |

일부 트렌드만 실패하면 200이고 오류는 `trend_results[i].errors`에 있다. 규칙 방식 `agent.run`(트렌드 1개, 검증용)은 안쪽 코드를 그대로 쓴다.

- 규칙 방식: 상품은 H&M 이미지가 있는 최상위 1개(1위에 이미지가 없으면 다음 순위, `rank`는 실제 순위). LLM agent: LLM이 순위 결과(`has_image` 포함) 중에서 고름. 사진은 `image`에 base64 JPEG. 이미지가 하나도 없으면 1위를 `image: null`로.
- 카테고리·소재 이름은 트렌드 팀 vocabulary_v1을 따른다(예: `티셔츠`, `울/캐시미어`). 예전 이름으로 만든 DB도 응답은 새 이름으로 나간다.

포트(8002)·경로는 팀 공통 OpenAPI가 정해지면 맞춘다.

## 구조

```
stock_agent/   season.py  attributes.py  synthetic.py  overstock.py  trend_input.py  trend_request.py  agent.py
               llm.py  llm_openai.py  llm_agent.py  multi.py  errors.py  api.py  schemas.py
contracts/   요청·응답 JSON Schema, 예시 (초안)
scripts/     데이터 생성·실행·검증 스크립트
tests/       단위·SQL·API·계약 테스트 (duckdb·fastapi·jsonschema가 없으면 해당 테스트는 건너뜀)
examples/    예시 요청 (02 트렌드 결과 형식)
.env.example LLM 환경변수 예시 (값 없음. 복사해 .env를 만들고, .env는 커밋하지 않음)
docs/        이슈 초안(범위·계약·결정 사항)
```
