# inventory (과잉재고 에이전트)

VOGUE 기획 파이프라인의 **03 상품 재고** 에이전트. 02 트렌드마다 짝 카테고리에서 트렌드 색과 어울리는 색의 과잉재고를 찾아,
무신사 착샷(없으면 무신사 상품 이미지)이 있으면 그 이미지와 소재·질감·디테일이 비슷한(CLIP, 색 제외) 후보로 좁힌 뒤 과잉재고 순위로 **트렌드마다 상품 1개(사진 포함)**를 반환한다.

```
사용자 쿼리 ──▶ 오케스트레이터 (Project_08): 쿼리 전처리 → ①②③④ 순차 blocking 호출 → 결과 통합 → 기획안 초안 UI
                 ① trend (02)       ◀ 전처리된 쿼리                                  ▶ TrendResult + Evidence
                 ② inventory (03, 이 레포)        ◀ 전처리된 쿼리 + TrendResult                     ▶ InventoryProductResult
                 ③ new-product-agent (05)          ◀ 전처리된 쿼리 + TrendResult + InventoryProductResult ▶ PlanningDraft
                 ④ sales-market-agent (04)         ◀ PlanningDraft                                  ▶ MarketData
```

오케스트레이터(Project_08)가 사용자 쿼리를 전처리하고 에이전트 4개를 순서대로 부른 뒤, 모든 결과를 모아 기획안 초안 UI로 조립한다.
에이전트끼리는 직접 부르지 않는다. inventory는 오케스트레이터에게서 trend 결과(`TrendAnalysisResult`, 전처리된 쿼리 조건 `applied_filters` 포함)를
`inventory-handoff` envelope의 `input`으로 받고, 결과(InventoryProductResult = producer payload 1.0)를 오케스트레이터에 돌려준다.
오케스트레이터가 이 결과를 new-product-agent에 넘긴다. Producer payload와 Project_08 공통 HTTP envelope의 역할은 [`contracts/`](contracts/README.md)에 구분해 기록한다.

- 처리는 정해진 규칙과 SQL·계산으로만 한다(외부 모델 API 호출 없음). 같은 입력·데이터면 같은 결과가 나온다. 단, 기준 이미지를 내려받지 못한 요청은 소재·디테일 텍스트만으로 CLIP 유사도를 계산하므로 결과가 다를 수 있다(`search.clip.image_used`)
- 재고 후보가 0개면 트렌드 색 조건만 단계적으로 완화한다. 사용자 색·사용자 카테고리는 완화하지 않는다
- CLIP 유사도 필터는 03 서버 안에서 CLIP 모델(open_clip `ViT-B-32-quickgelu`, OpenAI 가중치)과 미리 만든 재고 임베딩 인덱스로 계산한다

- producer agent_id: `inventory`; payload schema: `1.0`; Project_08 HTTP envelope: `inventory-handoff`
- 재고·판매가는 가상 데이터(`data_nature: sample`). 상세 규칙은 `docs/issues_draft.md`

## 1. 데이터 준비 및 R2 배치

원본 데이터와 DB는 Git·이미지에 넣지 않는다(Kaggle 대회 규정). `data/` 폴더에 직접 만든다.

```powershell
pip install -r requirements-dev.txt
python scripts/build_db.py          # H&M CSV → data/stock.duckdb
python scripts/build_attributes.py  # K-Fashion 속성 → article_attrs
python scripts/build_virtual_stock.py    # 가상 재고 → virtual_stock
python scripts/extract_images.py --zip data/raw/h-and-m-personalized-fashion-recommendations.zip
                                    # 의류 상품 이미지만 → data/images/<앞 3자리>/<article_id>.jpg
python scripts/build_clip_index.py  # 재고 상품 CLIP 인덱스 → data/clip_index.npz (DB·사진은 R2에서 읽음)
python -m pytest
```

- 이미지는 Kaggle 대회 전체 압축 파일(`kaggle competitions download -c h-and-m-personalized-fashion-recommendations -p data/raw`)에서
  의류 상품 것만 꺼낸다. `--max-side 512`를 주면 긴 변 512px로 줄여 저장해 응답이 가벼워진다(Pillow 필요).
- 생성된 DuckDB·이미지·CLIP 인덱스는 Git이나 Docker 이미지에 넣지 않는다. 배포 전 담당자가 Cloudflare R2의 private bucket에 업로드하고, DB object key·이미지 prefix·CLIP 인덱스 key·무신사 착샷 prefix를 환경 설정(`R2_DB_KEY`, `R2_IMAGE_PREFIX`, `R2_CLIP_INDEX_KEY`, `R2_SNAP_IMAGE_PREFIX`)에 등록한다. 무신사 착샷은 `scripts/extract_snap_images.py`로 HF parquet에서 꺼내 `scripts/upload_snap_images.py`(쓰기 권한 키)로 올린다.
- `build_clip_index.py`는 `.env`를 읽어 DB(`INVENTORY_DB_PATH`, 없으면 R2 DB 캐시)와 R2 상품 사진으로 의류 재고 79,178개 중 사진이나 소재·디테일이 있는 79,174개의 임베딩을 계산한다(`--image-dir`를 주면 로컬 사진). 사진은 내려받아 메모리에서만 쓰고 저장하지 않는다. 규칙은 아래 'API › 요청'의 CLIP 유사도 필터와 같고, 파일에 규칙 이름(`rule`)이 기록된다.
- 로컬 빌드·검증 스크립트는 `INVENTORY_DB_PATH`(기본 `data/stock.duckdb`)와 `INVENTORY_IMAGE_DIR`(기본 `data/images`)를 사용한다. API도 `INVENTORY_DB_PATH`가 설정되면 R2 대신 그 파일을 read-only로 연다(local-file 모드, 아래 2절). local-file 모드와 `scripts/run_agent.py`는 R2 착샷 저장소를 쓰지 않으므로 착샷 기준 이미지는 소재·디테일 텍스트만으로 CLIP을 건다(`search.clip.image_skip: no_snap_store`).

## 2. 환경 설정

`.env.example`을 복사해 `.env`를 만들고 값을 채운다. `.env`는 커밋하지 않는다(`.gitignore`).

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `R2_ACCOUNT_ID` | 없음 | Cloudflare 계정 ID. S3 endpoint는 `https://<account-id>.r2.cloudflarestorage.com`로 구성 |
| `R2_ENDPOINT` | 기본 account endpoint | 선택 사항. EU/US/FedRAMP 등 jurisdiction bucket은 Cloudflare가 안내한 jurisdiction endpoint 지정 |
| `R2_BUCKET` | 없음 | 비공개 R2 bucket 이름 |
| `R2_DB_KEY` | 없음 | bucket 안의 DuckDB object key |
| `R2_IMAGE_PREFIX` | 없음 | 이미지 object key prefix. 비우면 로컬 이미지 디렉터리를 사용 |
| `R2_ACCESS_KEY_ID` | 없음 | R2 읽기 전용 API token의 access key ID |
| `R2_SECRET_ACCESS_KEY` | 없음 | R2 읽기 전용 API token의 secret access key |
| `INVENTORY_CACHE_DIR` | OS 임시 폴더의 `inventory` | 선택 사항. 내려받은 DB와 요청된 이미지의 로컬 캐시 |
| `INVENTORY_IMAGE_DIR` | `data/images` | 로컬 개발/폴백 이미지 디렉터리 |
| `INVENTORY_IMAGE_CACHE_DIR` | `$INVENTORY_CACHE_DIR/images` | R2에서 받은 상품 이미지 캐시. 쓸 수 없으면(read-only mount 등) 캐시 없이 사진을 응답에 넣고 trend 결과 limitations에 적는다(500이 아님) |
| `INVENTORY_DB_PATH` | 없음 | 설정하면 local-file 모드: R2 DB 설정·HEAD 없이 이 DuckDB 파일을 read-only로 연다. 옆에 `<이름>.sha256` sidecar가 있으면 일치해야 한다. `/health/ready`는 `db: local-file`, `db_version: sha256:<파일 해시>`. 이미지는 `INVENTORY_IMAGE_DIR` |
| `FASHION_PLANNER_SERVICE_TOKEN` | 없음 | 두 inventory route의 공통 Bearer token. Project_08의 `FASHION_PLANNER_SERVICE_TOKEN`과 secret store에서 같은 값 사용 |
| `R2_CLIP_INDEX_KEY` | 없음 | 재고 CLIP 인덱스(`scripts/build_clip_index.py`로 만든 npz)의 R2 object key. `--prepare-cache`가 DB와 함께 `INVENTORY_CACHE_DIR/clip-index-<sha256>.npz`로 받는다 |
| `R2_SNAP_IMAGE_PREFIX` | 없음 | 무신사 스냅 착샷 JPEG의 R2 prefix(예: `musinsa-snap`). 02 착샷의 `image_ref`(`hf://datasets/disl/musinsa-snap-new@<revision>#row=<n>`)를 `<prefix>/<revision>/<n>.jpg`로 읽는다. 없거나 객체가 없으면(다른 revision 등) 착샷은 소재·디테일 텍스트만으로 CLIP을 건다 |
| `INVENTORY_CLIP_INDEX_PATH` | 없음 | 설정하면 R2 대신 이 인덱스 파일을 쓴다. 둘 다 없으면 `data/clip_index.npz`. 인덱스가 없거나 규칙(`rule`)이 다르면 서버는 뜨고 CLIP 필터만 건너뛴다(`search.clip.skip_reason: no_index`) |
| `INVENTORY_HANDOFF_TIMEOUT_SECONDS` | `60` | 두 inventory route의 전체 HTTP 처리 제한 시간(초, 0 초과 600 이하). 넘으면 504 `TIMEOUT` |

- R2 SDK region 값은 `auto`입니다. 접속은 S3 호환 API와 계정 endpoint를 사용합니다. jurisdiction bucket은 `R2_ENDPOINT`에 해당 공식 endpoint를 지정해야 합니다.
- R2 키는 해당 bucket의 `Object Read only` 권한만 부여하고, `R2_ACCESS_KEY_ID`·`R2_SECRET_ACCESS_KEY`는 배포 secret store에 둡니다. `.env`는 로컬 개발 전용이며 커밋하지 않습니다.
- API는 시작 시 R2 HEAD의 ETag/version과 기존 cache의 size/SHA-256 sidecar를 확인합니다. DB 다운로드는 자동 실행하지 않습니다. cache 준비는 별도 데이터 작업 `python -m fashion_planner_inventory.server --prepare-cache`입니다: 같은 R2 설정으로 객체 버전마다 한 번 내려받아 `INVENTORY_CACHE_DIR/stock-<sha256(bucket/key/version)>.duckdb`와 `.sha256` sidecar를 쓰고, read-only로 열어 readiness를 검사한 뒤 DuckDB 라이브러리 버전과 파일 storage version을 JSON 한 줄로 출력합니다(비밀·bucket·key는 출력하지 않음). 준비되지 않으면 exit 1.
- 확인된 이미지 key 구조는 `<R2_IMAGE_PREFIX>/<article_id 앞 3자리>/<article_id>.jpg`입니다. 예: `images/099/0999991001.jpg`. `/health`의 `images_configured`는 prefix/폴더 설정 여부를 나타내고 개별 object는 요청 중 확인합니다.
- `INVENTORY_DB_PATH`가 설정되면 API는 local-file 모드로 시작해 R2 DB 설정·HEAD를 쓰지 않습니다(stub 프로필·오프라인 점검용). schema·snapshot readiness 검사는 같습니다.
- 두 inventory route의 HTTP deadline은 기본 60초이고(`INVENTORY_HANDOFF_TIMEOUT_SECONDS`), 통합 배포는 180초로 둡니다. Project_08은 Stock 응답을 210초까지 기다립니다. 트렌드는 순서대로 처리하고, 트렌드마다 DB 조회와 기준 이미지가 있으면 그 이미지 읽기(상품 이미지 URL 5초, 착샷은 R2)·CLIP 임베딩 계산이 더해집니다. 초과 요청은 504로 응답하지만 이미 시작한 동기 DB 조회·이미지 내려받기의 강제 취소는 보장하지 않습니다.
- 서버는 시작할 때 CLIP 인덱스를 읽고(내려받지 않음: R2 인덱스는 `--prepare-cache`로 준비), 인덱스가 있으면 CLIP 모델도 미리 불러온다(첫 요청이 느리지 않게). `/health`의 `clip_index_loaded`·`clip_n_indexed`로 상태를 본다. CLIP 모델 가중치는 처음 불러올 때 내려받으므로 컨테이너가 인터넷에 나갈 수 있거나 `HOME`(`/tmp`) 아래 캐시가 준비되어 있어야 한다.

## 3. 로컬 실행

```powershell
python scripts/run_agent.py --request examples/request-example.json   # 02 결과 1건
python scripts/validate_requests.py                                   # 정확도 검증 10건 (성별 여·남·공용·없음, 무신사 착샷·상품 이미지 CLIP 8건)
python -m fashion_planner_inventory.server                                 # HTTP API
```

## 4. Docker 실행

```powershell
docker build -t inventory:v1 .
docker run --rm --env-file .env -v "$INVENTORY_CACHE_DIR:/cache" inventory:v1 python -m fashion_planner_inventory.server --prepare-cache   # DB·CLIP 인덱스 cache 준비 (데이터 작업)
docker run --rm -p 127.0.0.1:8002:8002 --env-file .env --read-only --tmpfs /tmp -v "$INVENTORY_CACHE_DIR:/cache:ro" -e INVENTORY_IMAGE_CACHE_DIR=/tmp/stock-images inventory:v1
```

- CLIP 인덱스는 `R2_CLIP_INDEX_KEY`가 있으면 `--prepare-cache`가 `/cache`에 받아 두고 서버가 그 파일을 읽는다(로컬 파일은 `INVENTORY_CLIP_INDEX_PATH`로 붙임). 없으면 서버는 뜨고 CLIP 필터만 건너뛴다(`/health`의 `clip_index_loaded: false`). 인덱스가 있으면 시작할 때 CLIP 모델 가중치를 `HOME`(`/tmp`) 아래로 내려받아 불러오므로 시간이 걸린다(healthcheck start-period 60초).

- 이미지는 uid/gid 10001, `HOME=/tmp`, `INVENTORY_CACHE_DIR=/cache`로 실행되고 `/app`에 쓰지 않는다. read-only root filesystem에서는 `/tmp` tmpfs와 `/cache`(volume 또는 tmpfs)만 쓰기 가능하게 둔다. `/cache`를 read-only로 붙이면 R2 이미지 캐시는 `INVENTORY_IMAGE_CACHE_DIR`(예: `/tmp/stock-images`)로 옮긴다. `--prepare-cache`로 host 폴더를 채울 때 Linux host에서는 그 폴더를 uid 10001이 쓸 수 있어야 한다.
- `HEALTHCHECK`는 `python -m fashion_planner_inventory.server --check-ready`(`GET /health/ready`, 외부 호출 없음)라서 DB가 준비되어야 healthy가 된다. liveness는 `GET /health/live`.

- 시작 시 R2 HEAD와 기존 cache의 size/SHA-256 sidecar만 검사하고 DuckDB read-only 연결을 엽니다. DB 다운로드는 자동 실행하지 않습니다. cache가 없거나 sidecar 검증·schema·sample snapshot 검사가 실패하면 `/health/ready`는 503입니다. 기존 cache 경로를 `INVENTORY_CACHE_DIR`로 지정하며 cache 파일은 같은 이미지로 `python -m fashion_planner_inventory.server --prepare-cache`를 실행해 준비합니다.
- `--env-file .env`로 런타임 설정을 전달합니다. 이미지 안에는 `.env`, DB, 이미지가 들어가지 않습니다.
- 확인: 브라우저에서 http://localhost:8002/health 또는 http://localhost:8002/docs
  (`/docs`에서 POST → Try it out → Examples에서 예시 요청을 골라 Execute)

요청 보내기 (다른 PowerShell 창):

```powershell
Invoke-RestMethod -Uri http://localhost:8002/v1/inventory-products -Method Post `
  -Headers @{ Authorization = "Bearer $env:FASHION_PLANNER_SERVICE_TOKEN" } `
  -ContentType "application/json; charset=utf-8" -InFile examples/request-example.json |
  ConvertTo-Json -Depth 10
```

## 5. Stub 프로필 (오프라인 결정적 실행)

통합 Docker 배포의 stub 프로필은 R2 없이 합성 fixture DB로 Stock을 실행한다.

```bash
python scripts/build_fixture_db.py /tmp/stock-fixture.duckdb   # 이미지 안에서는 python -m fashion_planner_inventory.fixture_db <out.duckdb>
INVENTORY_DB_PATH=/tmp/stock-fixture.duckdb FASHION_PLANNER_SERVICE_TOKEN=<token> \
  python -m fashion_planner_inventory.server
```

- fixture DB(`fashion_planner_inventory/fixture_db.py`): `tests/conftest.py`의 작은 표(a1~a6) + trend stub 결과(발레코어·상의·핑크 → 짝 하의·아우터, 봄·여름)에 맞춘 합성 상품 3개 + 표지 표 `stock_fixture`. H&M 원본이 아니고 내용은 매번 같다(`content_sha256`). storage version v1.0.0으로 써서 DuckDB 1.x·2.x 모두 읽는다. 있는 파일은 `--force` 없이 덮어쓰지 않는다.
- `/health/ready`: `db: local-file`, `synthetic_fixture: true`. 트렌드 결과마다 limitations에 합성 DB라는 문장이 붙는다.
- 요청: `examples/stub/inventory-request.json` — `inventory-handoff`, `input`은 trend stub 결과 그대로(`trends[].style.example_images` 포함).
- 기대 응답: `examples/stub/inventory-response-summary.json` — HTTP 200 `ok`, 상품 1개(`0910001001` 스커트·화이트, 사진 없음), bearer가 없으면 401. `tests/test_fixture_db.py`가 대조한다.

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/health/live` | API 생존만 확인 (DB 미준비에도 200) |
| GET | `/health/ready`, `/health` | DB readiness·snapshot, `clip_index_loaded`·`clip_n_indexed` |
| POST | `/v1/inventory-products` | 요청 1건 → 응답. `status`가 error일 때만 오류 번호(아래 오류 코드 표), 그 외 200 (`status`로 ok/partial/no_results 구분) |
| POST | `/v1/agents/inventory/runs` | Project_08 `inventory-handoff`; Bearer 인증; TrendAnalysisResult를 `input`으로 받고 producer payload 1.0을 `result`에 보존 |

두 inventory endpoint 모두 본문 파싱 전에 동일한 Bearer 인증을 요구합니다. `FASHION_PLANNER_SERVICE_TOKEN`과 `Authorization: Bearer <token>`이 필요하며 누락/불일치는 401입니다. token을 오류나 로그에 노출하지 않습니다. 운영은 HTTPS를 ingress/TLS termination에서 제공하고 authorization header를 backend까지 전달해야 합니다. loopback 개발 때 Project_08에 `FASHION_PLANNER_HTTP_DEV_MODE=1`을 설정합니다. 토큰 미설정도 401이며 malformed JSON도 인증보다 먼저 처리하지 않습니다.

Stock route는 기본 60초 전체 deadline을 적용합니다(`INVENTORY_HANDOFF_TIMEOUT_SECONDS`로 조정, 통합 배포는 180초이고 Project_08은 Stock 응답을 210초까지 기다립니다). 시간을 넘기면 HTTP는 504 envelope(`reason: timeout`)를 반환하지만, Python 동기 처리를 강제로 멈추지 못하므로 이미 시작한 DB 조회·기준 이미지 내려받기는 서버 쪽에서 끝까지 실행될 수 있습니다.

HTTP envelope `inventory-handoff`과 producer `schema_version="1.0"`은 별개입니다. 응답의 `result`는 원래 Stock payload 전체입니다. `/health/live`는 API 생존만 확인합니다. `/health/ready`와 호환 별칭 `/health`는 DB schema/column type·nonempty tables·sample/as_of/version·필수 수치 검사가 통과해야 200입니다. DB 미준비는 503입니다. 이미지 누락은 상태 필드로만 보고합니다. CLIP 인덱스 상태(`clip_index_loaded`)는 표시만 하고 200/503 판정에는 쓰지 않습니다. `/docs`와 `/openapi.json`에 두 endpoint를 모두 게시합니다.

### 요청

**요청은 02 트렌드 결과(`TrendAnalysisResult`) 그대로만 받는다**(예시: `examples/request-example.json`, 트렌드 여러 개: `examples/request-trends-example.json`).
`applied_filters`가 없으면 400 `INVALID_REQUEST`. 입구(`multi.run_request` → `fashion_planner_inventory/trend_request.py`)에서 내부 형식으로 바꾼다.

| 02 필드 | 03에서 |
|---|---|
| `request_id` | 그대로 돌려줌 |
| `status` | `ok` → 처리, `no_results` → DB 조회 없이 `no_results`, `error` → `INVALID_REQUEST`(`reason: upstream_error`) |
| `applied_filters.season` | `season` |
| `applied_filters.categories.include` / `exclude` | `target_category` / `exclude_categories` (`minor`, 없으면 `major`) |
| `applied_filters.attributes` 중 color | `target_colors` (소재·핏 등 다른 사용자 속성은 쓰지 않음) |
| `applied_filters.gender` | `gender` → 고객군 |
| `trends` | 그대로. `similar_products` 중 `snap_garment` 착샷(없으면 `catalog_item` 상품 이미지) 1장을 CLIP 필터 기준으로 씀(아래). `evidence_ids`·`image_urls` 등은 쓰지 않음 |

**성별 → 고객군**: `여` → Ladieswear + Divided, `남` → Menswear + Divided, `공용` → 전체.
요청 성별이 없으면 트렌드마다 Item `gender`로 정한다(여러 성별이면 합치고, `공용`이 있거나 모두 비면 전체).
결과는 `search.segments`·`segment_basis`와 limitations에 나온다.

**내부 형식** (02 결과를 바꾼 것. 바깥에서는 받지 않고, 안쪽 함수 `multi.run_internal`·`agent.run`과 그 테스트가 쓴다):

| 필드 | 설명 |
|---|---|
| `request_id` | 필수 |
| `season` | 필수. SeasonRequest `{"year": 2027, "season": "SS", "origin": "user_requested"}`. `origin`이 `default_next`면 출처 inferred |
| `target_category` | 사용자 기획 카테고리(영역 또는 세부 카테고리) 목록. 있으면 이것의 짝을 찾음 |
| `exclude_categories` | 제외 카테고리. 영역이면 짝 영역에서 빼고, 세부 카테고리면 그 상품을 후보에서 뺌. 모르는 값이거나 `target_category`와 겹치면 400 |
| `target_colors` | 사용자 색, 21색 이름. 21색이 아니면 400 |
| `gender` | 남/여/공용 |
| `segment` | 고객군을 직접 줄 때만(안쪽 테스트·검증용). 있으면 `gender`보다 먼저 |
| `trends` / `trend` | 트렌드 목록(`run_internal`) / 트렌드 1개(`agent.run`) |

트렌드 1개를 처리하는 규칙 (`fashion_planner_inventory/trend_input.py`, `fashion_planner_inventory/agent.py`):

- 트렌드 카테고리: 아이템의 `category_minor`, 없으면 `category_major`
- 트렌드 아이템 자신의 소재·핏은 재고를 거르는 데 쓰지 않는다 (새 상품의 소재로 짝 재고를 거르면 코디 논리에 맞지 않음). 무드 기준은 아래 CLIP 유사도 필터
- 기준 색 (사용자 쿼리가 가장 중요해서 사용자 색이 먼저)
  - 사용자 색(`target_colors`)이 있으면 트렌드 아이템 **전체**의 색과 비교: 겹치는 사용자 색이 있으면 그 색만(안 겹치는 사용자 색은 빼고 limitations에 기록), 하나도 안 겹치면 사용자 색 전부(limitations에 "트렌드 아이템 색에 없음" 기록)
  - 없으면 트렌드 색: 사용자 카테고리 아이템의 색 → 없으면 아이템 전체의 색 → 없으면 색 조건 없이 21색 전체. 개수 제한 없음
- 짝 카테고리: 사용자 카테고리의 짝 → 없으면 트렌드 카테고리 짝의 교집합(없으면 트렌드 카테고리와 그 짝 전체) → 둘 다 없으면 네 영역 전체
- 후보 색: 21색마다 기준 색 몇 개와 어울리는지(조합표 v1) 세어, 기준 색과 어울리는 색 중 **가장 많이 겹치는 색** + **기준 색 자체(톤온톤)**. 동점이면 모두. 상품 색이 기준 색과 같으면 `reason.color.tone_on_tone`에 적음
- 카테고리와 색이 모두 없는 트렌드는 그 트렌드만 오류
- 사용자 카테고리가 여러 개면 각 짝의 교집합(없으면 합집합). 제외 카테고리는 짝 영역·후보에서 뺀다

처리 순서 (`agent.run_trend`):

1. 짝 영역 → 2. 후보 색 → 3. 재고 조회(짝 영역 · 색 · 시즌 · 고객군 · 재고 > 0 · 제외 카테고리) → 4. 최근 판매 추세(같은 영역 후보 대비)
→ 5. CLIP 유사도 필터 → 6. 과잉재고 순위 → 사진이 있는 최상위 1개 → 7. 추천 이유·summary·근거

**후보가 0개일 때 완화**: 재고 조회 결과가 0개면 **트렌드 색 조건만** 넓혀 다시 조회한다.

| 단계 (`color_mode`) | 찾는 색 |
|---|---|
| `max_overlap` | 기준 색과 어울리는 색 중 가장 많이 겹치는 색 + 기준 색 자체(톤온톤). 처음엔 항상 이것 |
| `any` | 기준 색 하나라도와 어울리는 색 전부 + 톤온톤. `max_overlap`과 색이 같으면(예: 기준 색 1개) 건너뜀 |
| `none` | 트렌드 색 조건을 풀고 21색 전체 |

- 사용자 색(`target_colors`)이 기준 색이면 완화하지 않는다(사용자 조건). 사용자 카테고리·짝 영역도 넓히지 않는다
- 조회한 단계는 `search.color_attempts`(`mode`, `colors`, `n_candidates`), 최종 단계는 `search.color_mode`, 완화한 조건은 `search.relaxed`와 limitations에 적는다. `any`면 `reason.color.rule`이 "하나라도 어울리는 색", `none`이면 "색 조건 없음"
- 모든 단계 뒤에도 0개면 `no_results`

**CLIP 유사도 필터** (`fashion_planner_inventory/snap.py`, `fashion_planner_inventory/clip_matcher.py`, `fashion_planner_inventory/clip_vocab.py`): 새 상품(예: 시폰 블라우스)과
함께 팔 짝 재고를 트렌드 아이템의 무신사 이미지와 소재·질감·디테일 무드가 비슷한 것으로 좁힌다. 색은 조합 색상표가 정하므로 CLIP은 색을 보지 않는다.

- 기준 이미지 고르기: 사용자 카테고리 아이템(없으면 아이템 전체)의 `similar_products`에서
  1. `kind: snap_garment`(무신사 스냅 착샷) 중 `score`(트렌드 아이템과의 일치도)가 가장 높은 1장. 02 착샷은 `image_url` 없이 `image_ref`(HF parquet 행 참조)만 주므로, R2에 미리 올린 착샷 JPEG(`R2_SNAP_IMAGE_PREFIX`)로 읽는다. `multi_wearer_suspected: true`(여러 명이 찍혔을 수 있는 사진)는 뺀다
  2. 착샷이 없으면 `kind: catalog_item`(무신사 상품 이미지, `image_url` 또는 http `image_ref`) 중 `score`가 가장 높은 1장.
     상품 이미지는 트렌드 아이템 카테고리(예: 블라우스)라 짝 재고(하의)와 카테고리가 다르지만, 소재·질감·디테일만 비교하므로 그대로 쓴다
  3. 둘 다 없으면 CLIP 필터 없이 찾는다(`no_reference`)
- 기준 이미지가 있으면 **항상** 건다. 기준과 재고 인덱스는 같은 규칙으로 임베딩한다
  - 이미지: 흑백으로 바꿔 임베딩(색 제외, 명암·질감·형태만)
  - 텍스트: 소재 + 디테일을 영어 대응어로(CLIP은 영어 모델이라 한글 단어를 구분하지 못함). 기준은 02 `materials`·`details`(`DetailName`, 넥라인·칼라 제외), 재고는 `article_attrs.materials` + H&M 상품 설명에서 키워드로 뽑은 `DetailName`. 02 표준 어휘(vocabulary_v1) 소재 26개·디테일 40개를 쓴다
  - 합치기: 이미지 0.6 + 텍스트 0.4. 텍스트가 비면 이미지만, 이미지를 열지 못하면 텍스트만. 못 연 이유는 `search.clip.image_skip`(`no_image` 참조 없음, `host_not_allowed` 허용되지 않은 URL, `no_snap_store` 착샷 저장소 설정 없음, `not_found` 착샷이 저장소에 없음(다른 데이터셋 버전 등), `load_failed` 내려받기·열기 실패)과 limitations에 적는다
- 재고 후보 임베딩은 CLIP 인덱스에서 읽는다. 후보를 재고 색별로 나눠 각 색 안에서 유사도 상위 10%(올림, 색마다 최소 1개)를 남긴다. 유사도 수준이 기준 이미지마다 달라(H&M 사진 기준 중앙값 0.81~0.86) 고정 기준값은 쓰지 않고, 색 안에서만 비교해 흑백에 남은 밝기 차이로 밝은 색 재고가 쏠려 남지 않게 한다(조합 색상표가 고른 색 구성 유지). 후보를 0개로 만들지 않는다. 인덱스에 없는 후보는 유사도 0
- 걸지 못한 경우(인덱스 없음·규칙 다름 `no_index`, 후보가 인덱스에 하나도 없음 `no_match`, CLIP 모델을 불러오지 못함 `no_model`, 이미지를 열 수 없고 소재·디테일도 없음 `no_query`)는 후보를 그대로 두고 그 사실을 기록한다
- 응답 `search.outfit_reference`(`status` used/skipped, `skip_reason`, `kind` snap_garment/catalog_item, `product_id`, `image_url`, `image_ref`, `score`, `source`, `item_id`, `item_category`), `search.clip`(`applied`, `skip_reason`, `image_used`, `text_used`, `n_before`, `n_after`, `n_indexed`, `keep_ratio` 0.1, `keep_rule` per_color_top, `image_skip`; 기준 이미지가 없으면 `null`). CLIP 필터를 걸었으면 근거 `E-006`(기준 이미지 URL, 착샷은 `image_ref`, 착샷 `external_outfit_image` / 상품 이미지 `external_product_image`)이 붙는다
- 기준 이미지는 03 서버가 직접 읽는다. 상품 이미지 URL은 https이고 무신사 이미지 서버(`msscdn.net`, 예: `image.msscdn.net`)인 것만 5초·리다이렉트 없음·최대 10MB로 내려받고, 착샷은 R2에서 읽는다. 이미지는 임베딩 계산에만 쓰고 외부 서비스로 보내지 않는다
- 무신사 착샷·상품 이미지는 02 트렌드 결과로 받은 이미지이며 비상업적 연구용으로만 쓴다(착샷 원본: Hugging Face `disl/musinsa-snap-new`)

### 응답

`fashion_planner_inventory/multi.py`가 트렌드마다 `agent.run`을 돌려 순서대로 이어 붙인다.
Producer result 1.0에는 `findings`와 최상위 `limitations`가 없다. 상품은 `products`, 트렌드별 결과·한계는 `trend_results`에 담는다. canonical HTTP route는 이 producer payload 전체를 v0.1 envelope의 `result`에 보존한다.

| 필드 | 설명 |
|---|---|
| `products` | 트렌드 순서대로 n개. 상품 정보만(`style_id`, `sku_ids`, `category`, `season`, `attributes`, `inventory`, `image`). 상품을 못 고른 트렌드는 `null`. 같은 상품이 여러 번 나올 수 있음 |
| `trend_results` | n개. `trend_results[i]` ↔ `products[i]` (`product_index`). `style`, `status`, `rank`, `reason`(근거 숫자), `summary`(추천 이유 한 줄), `search`(찾은 조건), `limitations`, `errors`, `evidence_ids`(이 트렌드의 근거 ID) |
| `evidence` | 트렌드마다 데이터 출처 E-001~E-006(E-005 상품 사진, E-006 CLIP 기준 이미지는 쓴 경우만). `evidence_id` = `inventory:<style.id>:E-001`, `style_id`. `manifest_id`는 계산에 쓴 DB 버전(`/health`의 `db_version`), E-006은 `null`. 근거는 여기 한 곳에 모으고 `evidence_ids`로 가리킴 |
| `status` | 모두 ok → ok / 일부만 상품 있음 → partial / 모두 오류 → error / 상품 0개 → no_results |

### 오류 코드

`errors[].code`는 팀 공통 코드, 03 안의 자세한 원인은 `reason` (`fashion_planner_inventory/errors.py`).

| reason | code | HTTP |
|---|---|---|
| `missing_fields`, `invalid_input`, `schema_version_mismatch`, `upstream_error` | `INVALID_REQUEST` | 400 |
| `db_unavailable`, `image_source_unavailable` | `SOURCE_UNAVAILABLE` | 503 |
| `timeout` (HTTP 처리 제한 시간 초과) | `TIMEOUT` | 504 |
| `invalid_response`, `internal` | `INTERNAL_ERROR` | 500 |
| `trend_failed` (최근 추세만 실패, 나머지 지표로 순위) | `INTERNAL_ERROR` | 200 (`status: partial`) |

일부 트렌드만 실패하면 200이고 오류는 `trend_results[i].errors`에 있다. 안쪽 함수 `agent.run`(트렌드 1개)은 안쪽 코드를 그대로 쓴다.

- 상품은 H&M 이미지가 있는 최상위 1개(1위에 이미지가 없으면 다음 순위, `rank`는 실제 순위). 사진은 `image`에 base64 JPEG. 이미지가 하나도 없으면 1위를 `image: null`로.
- 카테고리·소재 이름은 트렌드 팀 vocabulary_v1을 따른다(예: `티셔츠`, `울/캐시미어`). DB 값이 다른 표기(`티셔츠·캐주얼상의`, `울·캐시미어` 등)여도 응답은 vocabulary_v1 이름으로 바꿔 낸다(`attributes.to_vocab`).

HTTP route와 producer contract의 역할은 `contracts/README.md`를 따른다.

## 구조

```
fashion_planner_inventory/   season.py  attributes.py  synthetic.py  overstock.py  trend_input.py  trend_request.py  agent.py
               snap.py  snap_image.py  clip_vocab.py  clip_matcher.py  images.py  storage.py  readiness.py
               multi.py  errors.py  api.py  schemas.py  env.py  server.py  fixture_db.py
contracts/   producer payload JSON Schema, 예시 및 HTTP envelope 설명
scripts/     데이터 생성·실행·검증 스크립트
tests/       단위·SQL·API·계약 테스트 (duckdb·fastapi·jsonschema가 없으면 해당 테스트는 건너뜀)
examples/    예시 요청 (02 트렌드 결과 형식)
.env.example 환경변수 예시 (값 없음. 복사해 .env를 만들고, .env는 커밋하지 않음)
docs/        이슈 초안(범위·계약·결정 사항), Cloudflare R2 설정(cloudflare-r2.md)
```

### Runtime manifest와 데이터 경계

Stock 포트·route·health 기본값의 단일 소스는 `fashion_planner_inventory/service-manifest.json`입니다. 로컬/Docker 시작과 Docker healthcheck는 `python -m fashion_planner_inventory.server`를 사용합니다. 포트는 8002입니다.

입력은 명시적인 Trend `schema_version=1.1`을 요구합니다. unsupported/missing version, producer identity 불일치, request_id/status/nested shape 오류는 DB 조회 전에 거부합니다. JSON 중복 key·NaN/Infinity도 거부합니다. 원본 body는 내부 변환본과 분리하고 `input_provenance.source_payload_sha256`은 원본 **Trend result body**의 python-json-sort/1 hash입니다. envelope hash와 같다고 취급하지 않습니다. Stock이 사용하지 않는 전체 Evidence의 의미 검증과 wire bytes artifact 보존은 P08 retained consumer 책임입니다. 사용자 원문과 적용 필터를 연결하는 stock_call_context도 P08 소유입니다.

인증된 upstream no_results는 DB가 없어도 200/no_results·빈 products로 반환합니다. 실제 0, null product slot, 조회 실패503은 각각 다른 상태입니다. 이 adapter는 `as_of=2020-09-22`, `definition=virtual_stock_from_hm_sales`, `data_nature=sample` snapshot만 받으며 현재 자사 재고를 검증하지 않습니다. 가격/재고는 합성이고 과거 거래 기반 지표와 구분합니다. 다른 snapshot은 상수를 현재값으로 바꿔 붙이지 않고 readiness에서 거부합니다.

`python scripts/diagnose_readiness.py`는 `.env` 파일 존재와 **현재 process env 주입 여부**를 분리하고, provision된 설정이 있으면 R2 HEAD·기존 cache·read-only schema를 진단합니다. DB 다운로드·재생성·업로드는 하지 않습니다. 로컬 SHA sidecar는 cache 손상 검사이며 upstream checksum/원천 진위의 증명이 아닙니다.
