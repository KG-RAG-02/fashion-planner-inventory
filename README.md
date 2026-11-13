# stock-agent (과잉재고 에이전트)

VOGUE 기획 파이프라인의 **03 상품 재고** 에이전트. 02 트렌드마다 **LLM agent**가 무신사 스냅 착샷에서 함께 입은 옷의 소재·핏을 보고,
도구(짝 카테고리·어울리는 색·재고 조회·순위)를 골라 부르며 조건을 정해, 과잉재고 중 **트렌드마다 상품 1개(사진 포함)**와 상위 50개 속성 요약을 반환한다.

```
01 사용자 쿼리 ──▶ 02 쿼리 전처리·트렌드 분석 ──(결과 그대로)──▶ 03 상품 재고 (stock-agent) ──▶ 05 새 상품 기획·최종 기획서
                                                                    04 타사판매·시장성 (앞 단계 입력 없음) ──▶ 05
```

단계는 01~05이고 에이전트는 02~05의 4개다. 오케스트레이터는 없다. 02가 자기 결과(`TrendAnalysisResult`)를 그대로 03에 보낸다(push).
04는 앞 단계 입력 없이 타사 판매 자료로 원가·마진·타사 평균가를 만든다. 03 결과는 04를 거치지 않고 05로 가며,
누가 05에 넘기는지는 아직 정하지 않았다. Producer payload와 Project_08 공통 HTTP envelope의 역할은 [`contracts/`](contracts/README.md)에 구분해 기록한다.

- LLM은 OpenAI `gpt-4o-mini`(`stock_agent/llm_openai.py`). agent는 교체 가능한 인터페이스(`stock_agent/llm.py`)만 알고, 키가 없으면 트렌드마다 `SOURCE_UNAVAILABLE`(`reason: no_llm`) 오류
- OpenAI로 트렌드 정의·설명, H&M 후보 상품 정보(상품 ID·카테고리·색·재고·판매 수치), 02의 무신사 스냅 착샷 이미지(URL, 열지 못하면 내려받은 이미지)가 전송된다(팀 확인 완료, 재고 수치는 가상 데이터)
- LLM이 실패하면 규칙 방식으로 대신 내지 않고 오류로 돌려준다(평가할 수 있게)
- 규칙 방식(`mode="rule"`)은 비교 기준선·정확도 검증용으로 남겨 둔다

- producer agent_id: `stock-agent`; payload schema: `1.0`; Project_08 HTTP envelope: `vogue-handoff/0.1`
- 재고·판매가는 가상 데이터(`data_nature: sample`). 상세 규칙은 `docs/issues_draft.md`

## 1. 데이터 준비 및 R2 배치

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
- 생성된 DuckDB와 이미지 데이터는 Git이나 Docker 이미지에 넣지 않는다. 배포 전 담당자가 Cloudflare R2의 private bucket에 업로드하고, 실제 DB object key와 이미지 prefix를 환경 설정에 등록한다.
- 로컬 빌드·검증 스크립트는 `STOCK_DB_PATH`(기본 `data/stock.duckdb`)와 `STOCK_IMAGE_DIR`(기본 `data/images`)를 사용한다. API도 `STOCK_DB_PATH`가 설정되면 R2 대신 그 파일을 read-only로 연다(local-file 모드, 아래 2절).

## 2. LLM 설정 (OpenAI)

`.env.example`을 복사해 `.env`를 만들고 `LLM_API_KEY`에 OpenAI 키를 넣는다. `.env`는 커밋하지 않는다(`.gitignore`).

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `STOCK_LLM_PROVIDER` | `openai` | `openai` 또는 `policy`. `policy`는 결정적 `PolicyLLM`(키·네트워크 없음, `no_llm`이 나지 않음, 트렌드 결과 limitations에 policy 실행임을 적음)으로 stub 프로필·오프라인 점검용. 그 밖의 값은 `llm_unavailable`. `/health/ready`의 `llm_provider`로 보인다 |
| `LLM_API_KEY` | 없음 | OpenAI 키. 없으면 LLM 연결 전으로 보고 `no_llm` 오류 (`STOCK_LLM_PROVIDER=openai`일 때) |
| `LLM_MODEL` | `gpt-4o-mini` | 모델 |
| `LLM_TIMEOUT` | `30` | 호출 1번당 시간 제한(초). 넘으면 `TIMEOUT`(504) |
| `R2_ACCOUNT_ID` | 없음 | Cloudflare 계정 ID. S3 endpoint는 `https://<account-id>.r2.cloudflarestorage.com`로 구성 |
| `R2_ENDPOINT` | 기본 account endpoint | 선택 사항. EU/US/FedRAMP 등 jurisdiction bucket은 Cloudflare가 안내한 jurisdiction endpoint 지정 |
| `R2_BUCKET` | 없음 | 비공개 R2 bucket 이름 |
| `R2_DB_KEY` | 없음 | bucket 안의 DuckDB object key |
| `R2_IMAGE_PREFIX` | 없음 | 이미지 object key prefix. 비우면 로컬 이미지 디렉터리를 사용 |
| `R2_ACCESS_KEY_ID` | 없음 | R2 읽기 전용 API token의 access key ID |
| `R2_SECRET_ACCESS_KEY` | 없음 | R2 읽기 전용 API token의 secret access key |
| `STOCK_CACHE_DIR` | OS 임시 폴더의 `stock-agent` | 선택 사항. 내려받은 DB와 요청된 이미지의 로컬 캐시 |
| `STOCK_IMAGE_DIR` | `data/images` | 로컬 개발/폴백 이미지 디렉터리 |
| `STOCK_IMAGE_CACHE_DIR` | `$STOCK_CACHE_DIR/images` | R2에서 받은 상품 이미지 캐시. 쓸 수 없으면(read-only mount 등) 캐시 없이 사진을 응답에 넣고 trend 결과 limitations에 적는다(500이 아님) |
| `STOCK_DB_PATH` | 없음 | 설정하면 local-file 모드: R2 DB 설정·HEAD 없이 이 DuckDB 파일을 read-only로 연다. 옆에 `<이름>.sha256` sidecar가 있으면 일치해야 한다. `/health/ready`는 `db: local-file`, `db_version: sha256:<파일 해시>`. 이미지는 `STOCK_IMAGE_DIR` |
| `VOGUE_AGENT_SERVICE_BEARER_TOKEN` | 없음 | 두 inventory route의 공통 Bearer token. Project_08의 `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 secret store에서 같은 값 사용 |

- R2 SDK region 값은 `auto`입니다. 접속은 S3 호환 API와 계정 endpoint를 사용합니다. jurisdiction bucket은 `R2_ENDPOINT`에 해당 공식 endpoint를 지정해야 합니다.
- R2 키는 해당 bucket의 `Object Read only` 권한만 부여하고, `R2_ACCESS_KEY_ID`·`R2_SECRET_ACCESS_KEY`는 배포 secret store에 둡니다. `.env`는 로컬 개발 전용이며 커밋하지 않습니다.
- API는 시작 시 R2 HEAD의 ETag/version과 기존 cache의 size/SHA-256 sidecar를 확인합니다. DB 다운로드는 자동 실행하지 않습니다. cache 준비는 별도 데이터 작업 `python -m stock_agent.server --prepare-cache`입니다: 같은 R2 설정으로 객체 버전마다 한 번 내려받아 `STOCK_CACHE_DIR/stock-<sha256(bucket/key/version)>.duckdb`와 `.sha256` sidecar를 쓰고, read-only로 열어 readiness를 검사한 뒤 DuckDB 라이브러리 버전과 파일 storage version을 JSON 한 줄로 출력합니다(비밀·bucket·key는 출력하지 않음). 준비되지 않으면 exit 1.
- 확인된 이미지 key 구조는 `<R2_IMAGE_PREFIX>/<article_id 앞 3자리>/<article_id>.jpg`입니다. 예: `images/099/0999991001.jpg`. `/health`의 `images_configured`는 prefix/폴더 설정 여부를 나타내고 개별 object는 요청 중 확인합니다.
- `STOCK_DB_PATH`가 설정되면 API는 local-file 모드로 시작해 R2 DB 설정·HEAD를 쓰지 않습니다(stub 프로필·오프라인 점검용). schema·snapshot readiness 검사는 같습니다.
- 호출 설정: `temperature 0`, 매 차례 도구 1개(`tool_choice=required`, `parallel_tool_calls=false`), 연결 오류·429·5xx는 2번 다시 시도. 트렌드 1개당 최대 12번 호출.
- Project_08 Stock timeout과 새 route의 HTTP deadline은 기본 60초입니다(`STOCK_HANDOFF_TIMEOUT_SECONDS`, 0 초과 600 이하; 통합 배포는 90초). 기존 LLM timeout 30초에 SDK 재시도 2회면 한 호출 예산이 최대 90초이고, 이미지 fallback 및 순차 multi-trend 호출은 더해집니다. 초과 요청은 504로 응답하지만 동기 SDK/DB 호출의 강제 취소는 보장하지 않습니다. 엄격한 비용 상한을 위해 운영 배포 전 남은 요청 budget 기반 SDK timeout/retry와 image fetch 취소 또는 cancellable job 모델이 필요합니다.
- `/health`의 `llm_provider`, `llm_connected`, `llm_model`, `llm_error`로 연결 상태를 본다. 키가 맞는지는 실제 요청에서 알 수 있다(틀리면 `SOURCE_UNAVAILABLE`, `reason: llm_unavailable`).

## 3. 로컬 실행

```powershell
python scripts/run_agent.py --request examples/request-example.json   # 02 결과 1건 (LLM agent, 키가 없으면 no_llm 오류)
python scripts/run_agent.py --policy                                  # agent 구조 점검 (PolicyLLM: 정해진 순서로 도구를 부르는 가짜 LLM)
python scripts/run_agent.py --mode rule                               # 규칙 방식 (기준선)
python scripts/validate_requests.py                                   # 규칙 방식 정확도 검증 10건 (성별 여·남·공용·없음)
python -m stock_agent.server                                 # HTTP API
```

## 4. Docker 실행

```powershell
docker build -t stock-agent:v1 .
docker run --rm --env-file .env -v "$STOCK_CACHE_DIR:/cache" stock-agent:v1 python -m stock_agent.server --prepare-cache   # cache 준비 (데이터 작업)
docker run --rm -p 127.0.0.1:8002:8002 --env-file .env --read-only --tmpfs /tmp -v "$STOCK_CACHE_DIR:/cache:ro" -e STOCK_IMAGE_CACHE_DIR=/tmp/stock-images stock-agent:v1
```

- 이미지는 uid/gid 10001, `HOME=/tmp`, `STOCK_CACHE_DIR=/cache`로 실행되고 `/app`에 쓰지 않는다. read-only root filesystem에서는 `/tmp` tmpfs와 `/cache`(volume 또는 tmpfs)만 쓰기 가능하게 둔다. `/cache`를 read-only로 붙이면 R2 이미지 캐시는 `STOCK_IMAGE_CACHE_DIR`(예: `/tmp/stock-images`)로 옮긴다. `--prepare-cache`로 host 폴더를 채울 때 Linux host에서는 그 폴더를 uid 10001이 쓸 수 있어야 한다.
- `HEALTHCHECK`는 `python -m stock_agent.server --check-ready`(`GET /health/ready`, 외부 호출 없음)라서 DB가 준비되어야 healthy가 된다. liveness는 `GET /health/live`.

- 시작 시 R2 HEAD와 기존 cache의 size/SHA-256 sidecar만 검사하고 DuckDB read-only 연결을 엽니다. DB 다운로드는 자동 실행하지 않습니다. cache가 없거나 sidecar 검증·schema·sample snapshot 검사가 실패하면 `/health/ready`는 503입니다. 기존 cache 경로를 `STOCK_CACHE_DIR`로 지정하며 cache 파일은 같은 이미지로 `python -m stock_agent.server --prepare-cache`를 실행해 준비합니다.
- `--env-file .env`로 런타임 설정을 전달합니다. 이미지 안에는 `.env`, DB, 이미지가 들어가지 않습니다.
- 확인: 브라우저에서 http://localhost:8002/health 또는 http://localhost:8002/docs
  (`/docs`에서 POST → Try it out → Examples에서 예시 요청을 골라 Execute)

요청 보내기 (다른 PowerShell 창):

```powershell
Invoke-RestMethod -Uri http://localhost:8002/v1/inventory-products -Method Post `
  -Headers @{ Authorization = "Bearer $env:VOGUE_AGENT_SERVICE_BEARER_TOKEN" } `
  -ContentType "application/json; charset=utf-8" -InFile examples/request-example.json |
  ConvertTo-Json -Depth 10
```

## 5. Stub 프로필 (오프라인 결정적 실행)

통합 Docker 배포의 stub 프로필은 R2·OpenAI 없이 합성 fixture DB와 결정적 `PolicyLLM`으로 Stock을 실행한다.

```bash
python scripts/build_fixture_db.py /tmp/stock-fixture.duckdb   # 이미지 안에서는 python -m stock_agent.fixture_db <out.duckdb>
STOCK_DB_PATH=/tmp/stock-fixture.duckdb STOCK_LLM_PROVIDER=policy VOGUE_AGENT_SERVICE_BEARER_TOKEN=<token> \
  python -m stock_agent.server
```

- fixture DB(`stock_agent/fixture_db.py`): `tests/conftest.py`의 작은 표(a1~a6) + trend-analysis-agent stub 결과(발레코어·상의·핑크 → 짝 하의·아우터, 봄·여름)에 맞춘 합성 상품 3개 + 표지 표 `stock_fixture`. H&M 원본이 아니고 내용은 매번 같다(`content_sha256`). storage version v1.0.0으로 써서 DuckDB 1.x·2.x 모두 읽는다. 있는 파일은 `--force` 없이 덮어쓰지 않는다.
- `/health/ready`: `db: local-file`, `llm_provider: policy`, `synthetic_fixture: true`. 트렌드 결과마다 limitations에 PolicyLLM이 골랐다는 문장과 합성 DB라는 문장이 붙는다.
- 요청: `examples/stub/stock-request.json` — `vogue-handoff/0.1`, `input`은 trend stub 결과 그대로(`trends[].style.example_images` 포함).
- 기대 응답: `examples/stub/stock-response-summary.json` — HTTP 200 `ok`, 상품 1개(`0910001001` 스커트·화이트, 사진 없음), bearer가 없으면 401. `tests/test_fixture_db.py`가 대조한다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health/live` | API 생존만 확인 (DB 미준비에도 200) |
| GET | `/health/ready`, `/health` | DB readiness·snapshot, `llm_configured`, `llm_authenticated=null` |
| POST | `/v1/inventory-products` | 요청 1건 → 응답. `status`가 error일 때만 오류 번호(아래 오류 코드 표), 그 외 200 (`status`로 ok/partial/no_results 구분) |
| POST | `/v1/agents/stock-agent/runs` | Project_08 `vogue-handoff/0.1`; Bearer 인증; TrendAnalysisResult를 `input`으로 받고 producer payload 1.0을 `result`에 보존 |

두 inventory endpoint 모두 본문 파싱 전에 동일한 Bearer 인증을 요구합니다. `VOGUE_AGENT_SERVICE_BEARER_TOKEN`과 `Authorization: Bearer <token>`이 필요하며 누락/불일치는 401입니다. token을 오류나 로그에 노출하지 않습니다. 운영은 HTTPS를 ingress/TLS termination에서 제공하고 authorization header를 backend까지 전달해야 합니다. loopback 개발 때 Project_08에 `VOGUE_AGENT_HTTP_DEV_MODE=1`을 설정합니다. 토큰 미설정도 401이며 malformed JSON도 인증보다 먼저 처리하지 않습니다.

Stock route는 기본 60초 전체 deadline을 적용합니다(`STOCK_HANDOFF_TIMEOUT_SECONDS`로 조정, 통합 배포는 90초이고 Project_08은 Stock 응답을 120초까지 기다립니다). 기존 LLM timeout 30초와 SDK 재시도 2회는 호출 하나에 최대 90초 예산이며 이미지 fallback/여러 trend 순차 처리도 더해집니다. 60초 뒤 HTTP는 504 envelope를 반환하지만, Python 동기 SDK/DB 호출을 강제로 멈추지 못하므로 이미 시작한 호출 비용이 계속될 수 있습니다. 운영에서 비용 상한을 보장하려면 남은 deadline 예산에 따라 SDK timeout/retries와 이미지 다운로드 취소를 제한하거나 cancellable job 모델이 필요합니다.

HTTP envelope `vogue-handoff/0.1`과 producer `schema_version="1.0"`은 별개입니다. 응답의 `result`는 원래 Stock payload 전체입니다. `/health/live`는 API 생존만 확인합니다. `/health/ready`와 호환 별칭 `/health`는 DB schema/column type·nonempty tables·sample/as_of/version·필수 수치 검사가 통과해야 200입니다. DB 미준비는 503입니다. 이미지 누락은 상태 필드로만 보고합니다. `llm_connected`는 client 객체 존재만 나타내며 원격 인증/연결 점검은 아닙니다. `/docs`와 `/openapi.json`에 두 endpoint를 모두 게시합니다.

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
| `trends` | 그대로. `similar_products` 중 `snap_garment` 착샷 1장만 이미지 분석에 씀(아래). `evidence_ids`·`image_urls` 등은 쓰지 않음 |

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
- 트렌드 아이템 자신의 소재·핏은 재고를 거르는 데 쓰지 않는다 (새 상품의 소재로 짝 재고를 거르면 코디 논리에 맞지 않음, 2026-11-09). 소재·핏 기준은 아래 착샷
- 기준 색 (사용자 쿼리가 가장 중요해서 사용자 색이 먼저)
  - 사용자 색(`target_colors`)이 있으면 트렌드 아이템 **전체**의 색과 비교: 겹치는 사용자 색이 있으면 그 색만(안 겹치는 사용자 색은 빼고 limitations에 기록), 하나도 안 겹치면 사용자 색 전부(limitations에 "트렌드 아이템 색에 없음" 기록)
  - 없으면 트렌드 색: 사용자 카테고리 아이템의 색 → 없으면 아이템 전체의 색 → 없으면 색 조건 없이 21색 전체. 개수 제한 없음
- 짝 카테고리: 사용자 카테고리의 짝 → 없으면 트렌드 카테고리 짝의 교집합(없으면 트렌드 카테고리와 그 짝 전체) → 둘 다 없으면 네 영역 전체
- 후보 색: 21색마다 기준 색 몇 개와 어울리는지(조합표 v1) 세어, 기준 색과 어울리는 색 중 **가장 많이 겹치는 색** + **기준 색 자체(톤온톤)**. 동점이면 모두. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 적음
- 카테고리와 색이 모두 없는 트렌드는 그 트렌드만 오류
- 사용자 카테고리가 여러 개면 각 짝의 교집합(없으면 합집합). 제외 카테고리는 짝 영역·후보에서 뺀다

**착샷 소재·핏** (`stock_agent/outfit.py`, 2026-11-09 결정): 새 상품(예: 시폰 블라우스)과 함께 팔 재고는
"트렌드 아이템과 비슷한 무신사 스냅 속 모델이 그 옷과 **함께 입은** 하의·아우터"의 소재·핏을 기준으로 고른다.
02는 같은 스냅 속 다른 옷의 속성을 주지 않으므로 03 LLM(gpt-4o-mini 이미지 입력)이 착샷을 보고 뽑는다.

- 착샷 고르기: 사용자 카테고리 아이템(없으면 아이템 전체)의 `similar_products` 중 `kind: snap_garment`이고 `image_url`이 있는 것 → `score`(트렌드 아이템과의 일치도)가 가장 높은 1장
- 착샷 반영을 생략하는 경우(소재·핏 조건 없이 카테고리·색으로만 찾고 limitations에 기록): `catalog_item`(판매 상품)만 있음(`no_snap`), 아이템만 잘라낸 사진(`not_full_outfit`), 이미지를 열 수 없음(`image_error`), 이미지를 못 보는 LLM(`no_vision`)
- 뽑는 값: 영역(상의·하의·아우터·원피스)마다 보이는지·소재(vocabulary_v1 26개)·핏(하의 스키니·노멀·와이드·루즈·벨보텀, 그 밖 타이트·노멀·루즈·오버사이즈). 슬림은 하의면 스키니. 세이프는 재고 DB에 없어 뽑지 않음. 어휘 밖 값은 버리고 limitations에 기록
- 짝 영역: 착샷에 없는 짝 영역은 뺀다(예: 아우터를 안 입음 → 하의만). 보이는 짝 영역이 하나도 없으면 원래 짝 영역 그대로
- 재고 단계 (코드가 정함, LLM이 바꿀 수 없음): **A** 소재·핏 모두 일치 → 없으면 **B** 소재 일치(핏을 풂) → 없으면 **C** 카테고리·색만 맞는 전체(소재까지 풂). C에서는 소재 정보가 없는 재고(미확인)와 다른 소재 재고를 같게 보고 과잉재고 순위로 고른다. 데님은 카테고리 청바지도 일치로 본다
- 착샷 분석 중 LLM 자체가 실패하면(시간 초과·연결·답 형식 오류) 다른 LLM 실패와 같이 그 트렌드는 오류. 이미지 URL 문제는 생략하고 진행
- OpenAI가 이미지 URL을 못 열면 03이 직접 내려받아(http/https, 10초, 5MB, image/*) base64로 다시 보낸다(컨테이너가 인터넷에 나갈 수 있어야 함)
- 착샷을 쓰면 근거 `E-006`(무신사 스냅 착샷 URL)이 붙는다. 무신사 이미지를 OpenAI로 보내는 것은 팀 확인 완료
- 규칙 방식(`mode="rule"`)은 착샷을 쓰지 않는다(LLM이 없음)

LLM agent (`stock_agent/llm_agent.py`): 착샷 분석 뒤, 위 규칙으로 정한 기준 색·카테고리에서 시작해 LLM이 도구를 골라 부르고 결과를 보고 다음 행동을 정한다.

| 도구 | 하는 일 |
|---|---|
| `find_categories` | 짝 영역 정하기. 사용자 카테고리 여러 개의 교집합/합집합 선택. 제외 영역과 착샷에 없는 영역은 자동으로 빠짐 |
| `find_colors` | 기준 색과 어울리는 색 + 기준 색 자체(톤온톤). `max_overlap`(가장 많이 겹치는 색) 또는 `any`(하나라도 어울리는 색) |
| `get_inventory` | 앞 두 도구 결과 **전부**로 재고 후보 조회(영역·색을 좁힐 수 없음). 착샷 소재·핏이 있으면 단계 A/B/C를 정해 그 단계 후보만 남김. 후보 수·단계·단계별 수·속성 분포 |
| `rank_candidates` | 정해진 단계 안에서 과잉재고 순위 상위 몇 개 |
| `finish` | LLM이 고른 상품과 이유를 받아 검사하고 끝냄. 1위가 아닌 상품이면 `why_not_higher`(더 높은 순위 대신 고른 이유)가 필수. 응답은 기존 코드로 `product`·`trend_result`를 만듦 |

- 순위는 과잉재고 순서라 기본은 1위다. LLM이 1위가 아닌 상품을 고르면 더 높은 순위 상품과 실제로 다른 점(사진 없음 등)을 `why_not_higher`에 써야 하고, 없으면 `invalid_final`. 이유는 자유 문장으로 받아 `reason.llm_why_not_higher`에 남기고(1위면 `null`) 평가에서 본다. 영역 차이·소재·핏·모든 후보에 공통인 점은 이유가 될 수 없다고 LLM에게 알린다(코드는 이유가 있는지와 숫자만 검사)
- `get_inventory`의 `areas`·`colors`는 생략하면 앞 결과 전부. 일부만 주면(좁히면) `rule_violation`이고 다시 고르게 한다 (2026-11-09 결정: 어울리는 색과 톤온톤을 함께 봄. 색 규칙을 바꾸려면 `find_colors`를 다시 부름)
- 규칙에 맞지 않는 호출(`rule_violation`, `invalid_final`)은 실행하지 않고 오류를 LLM에게 돌려줘 다시 고르게 한다. 트렌드당 2번까지, 3번째 위반에서 실패. 막힌 호출도 `search.steps`에 `error`(`reason`, `message`)와 함께 남고, 다시 고른 경우 limitations에 적는다
- 후보가 0개면 **트렌드 쪽 조건만** 완화(트렌드 색·트렌드 카테고리·색 규칙). 사용자 조건(`target_category`, `target_colors`)은 완화하지 않음. 착샷 소재·핏은 단계로 코드가 완화
- LLM이 규칙을 어기거나 실패하면 그 트렌드는 오류(`product: null`). 원인 `reason`: `no_llm`, `llm_unavailable`(키·권한·모델 이름·네트워크), `timeout`, `invalid_response`(도구를 안 부름·없는 도구·인자 오류·착샷 분석 답 형식 오류), `rule_violation`(사용자 조건 변경, 제외 카테고리 다시 넣기, 21색 밖 값, 영역·색 좁히기, 도구 순서), `step_limit`(도구 12번 초과), `invalid_final`(순위 결과에 없는 상품, 후보가 있는데 안 고름, 1위가 아닌데 `why_not_higher` 없음, 이유에 근거 없는 숫자)
- 응답 `search`에 `mode: "llm"`, `steps`(부른 도구·조건·후보 수·단계), `relaxed`(완화한 트렌드 조건, 무엇을·무엇에서·무엇으로·왜), `outfit_reference`(쓴 착샷: `status` used/skipped/failed, `skip_reason`, `product_id`, `image_url`, `score`, `garments` 영역별 소재·핏, `areas_removed`), `attribute_tier`(`tier` A/B/C, `counts` 단계별 수, `relaxed` 푼 조건 fit/materials). `reason`에 `attributes`(상품 소재·핏과 착샷 같은 영역 옷의 `outfit_materials`·`outfit_fit`, `materials_match`·`fit_match`: true/false/null=미확인, `tier`), `decided_by: "llm"`, `llm_reason`, `llm_why_not_higher`

### 응답

`stock_agent/multi.py`가 트렌드마다 LLM agent(`mode="rule"`이면 `agent.run`)를 돌려 순서대로 이어 붙인다.
Producer result 1.0에는 `findings`와 최상위 `limitations`가 없다. 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 담는다. 새 HTTP route는 이 producer payload 전체를 v0.1 envelope의 `result`에 보존한다.

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

HTTP route와 producer contract의 역할은 `contracts/README.md`를 따른다.

## 구조

```
stock_agent/   season.py  attributes.py  synthetic.py  overstock.py  trend_input.py  trend_request.py  agent.py
               llm.py  llm_openai.py  llm_agent.py  outfit.py  multi.py  errors.py  api.py  schemas.py
contracts/   producer payload JSON Schema, 예시 및 HTTP envelope 설명
scripts/     데이터 생성·실행·검증 스크립트
tests/       단위·SQL·API·계약 테스트 (duckdb·fastapi·jsonschema가 없으면 해당 테스트는 건너뜀)
examples/    예시 요청 (02 트렌드 결과 형식)
.env.example LLM 환경변수 예시 (값 없음. 복사해 .env를 만들고, .env는 커밋하지 않음)
docs/        이슈 초안(범위·계약·결정 사항)
```

### Runtime manifest와 데이터 경계

Stock 포트·route·health 기본값의 단일 소스는 `stock_agent/service-manifest.json`입니다. 로컬/Docker 시작과 Docker healthcheck는 `python -m stock_agent.server`를 사용합니다. 포트는 8002이며 기존 Orchestrator launcher의 8769 기본값은 해당 owner가 이 manifest를 소비하도록 변경해야 합니다. 임시 override는 `VOGUE_STOCK_AGENT_URL=http://127.0.0.1:8002`입니다.

입력은 명시적인 Trend `schema_version=1.1`을 요구합니다. unsupported/missing version, producer identity 불일치, request_id/status/nested shape 오류는 DB/LLM 조회 전에 거부합니다. JSON 중복 key·NaN/Infinity도 거부합니다. 원본 body는 내부 변환본과 분리하고 `input_provenance.source_payload_sha256`은 원본 **Trend result body**의 python-json-sort/1 hash입니다. envelope hash와 같다고 취급하지 않습니다. Stock이 사용하지 않는 전체 Evidence의 의미 검증과 wire bytes artifact 보존은 P08 retained consumer 책임입니다. 사용자 원문과 적용 필터를 연결하는 stock_call_context도 P08 소유입니다.

인증된 upstream no_results는 DB가 없어도 200/no_results·빈 products로 반환합니다. 실제 0, null product slot, 조회 실패503은 각각 다른 상태입니다. 이 adapter는 `as_of=2020-09-22`, `definition=virtual_stock_v1`, `data_nature=sample` snapshot만 받으며 현재 자사 재고를 검증하지 않습니다. 가격/재고는 합성이고 과거 거래 기반 지표와 구분합니다. 다른 snapshot은 상수를 현재값으로 바꿔 붙이지 않고 readiness에서 거부합니다.

`python scripts/diagnose_readiness.py`는 `.env` 파일 존재와 **현재 process env 주입 여부**를 분리하고, provision된 설정이 있으면 R2 HEAD·기존 cache·read-only schema를 진단합니다. DB 다운로드·재생성·업로드와 모델 호출은 하지 않습니다. 로컬 SHA sidecar는 cache 손상 검사이며 upstream checksum/원천 진위의 증명이 아닙니다.
