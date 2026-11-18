# 트러블슈팅과 교훈

inventory 서비스를 만들며 겪은 문제, 원인을 찾은 방법, 바꾼 내용입니다.
항목마다 증상, 원인(근거), 해결, 검증, 교훈, 커밋 순서로 적습니다. 커밋 링크는 공개 히스토리를 가리킵니다. 환경변수·모듈 이름은 지금 이름으로 적었습니다.

| ID | 영역 | 한 줄 요약 |
|---|---|---|
| [T-CLIP-01](#t-clip-01) | CLIP, Docker | torch와 torchvision을 다른 인덱스에서 받아 CLIP이 꺼졌다 |
| [T-CLIP-02](#t-clip-02) | CLIP | OpenAI 가중치는 QuickGELU 모델 설정으로 불러야 한다 |
| [T-CLIP-03](#t-clip-03) | CLIP | `.npz`의 object 배열은 pickle이 필요하다. 읽는 쪽이 아니라 쓰는 쪽을 고친다 |
| [T-CLIP-04](#t-clip-04) | CLIP, 보안 | 실패하면 필터만 건너뛰고, 기준 이미지 URL은 허용 목록으로 제한한다 |
| [T-CACHE-01](#t-cache-01) | 저장소 | 읽기 전용 이미지 캐시가 처리되지 않은 `OSError`를 냈다 |
| [T-CACHE-02](#t-cache-02) | 저장소 | 시작할 때 내려받지 않으니 캐시를 채우는 별도 작업이 필요했다 |
| [T-API-01](#t-api-01) | API | 고정 60초 제한 시간과 트렌드 순차 처리가 맞지 않았다 |
| [T-DOCKER-01](#t-docker-01) | Docker | uid 10001, `HOME=/tmp`, 쓰기 가능한 곳은 `/cache`뿐 |
| [T-CONTRACT-01](#t-contract-01) | 계약 | 소비자가 아직 필수로 보는 필드를 지웠다 |
| [T-DESIGN-01](#t-design-01) | 설계 | 프롬프트 규칙으로 근거 없는 설명을 막지 못해 LLM을 뺐다 |
| [T-RANK-01](#t-rank-01) | 순위 | 사용자 카테고리의 짝에는 교집합이 아니라 순서가 필요했다 |

---

<a id="t-clip-01"></a>
## T-CLIP-01 도커 이미지 안에서 CLIP 필터가 한 번도 돌지 않았다

- **증상.** 같은 코드가 도커 밖에서는 됐지만 이미지 안에서는 CLIP 필터가 전혀 돌지 않았다.
- **원인.** Dockerfile이 `torch`는 PyTorch CPU 인덱스에서 받았는데, `open-clip-torch`가 `torchvision`을 PyPI에서 따로 끌어왔다. 둘의 짝이 맞지 않아 `import open_clip`이 `operator torchvision::nms does not exist`로 실패했다. 모델을 못 불러오면 필터만 건너뛰도록 되어 있어서([`bc909c3`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/bc909c3b94fe76a96fd321784a93a4b8fdd6cc4e)) 요청은 성공했고, CLIP 관련 필드에만 드러났다.
- **해결.** `torch`와 `torchvision`을 같은 CPU 인덱스에서 함께, 다른 의존성보다 먼저 설치한다(`Dockerfile`). 읽기 전용 루트 파일시스템에서도 가중치를 볼륨에 둘 수 있게 서비스 사용자가 쓸 수 있는 `/models` 폴더도 만든다.
- **검증.** 버전을 맞춰 다시 빌드한 이미지에서 `import open_clip`이 성공한다(커밋 메시지 기록: torch 2.15.0+cpu, torchvision 0.29.2+cpu).
- **교훈.** 조용히 기능을 줄이는 구성 요소에는 그 사실을 알리는 신호가 있어야 한다. 이미지를 빌드할 때마다 HTTP 상태만 보지 말고 `/health`의 `clip_index_loaded`와 응답의 `search.clip.skip_reason`을 확인한다.
- **커밋.** [`1b56e63`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/1b56e639592847fc077d45ae63c42116c593217e)

<a id="t-clip-02"></a>
## T-CLIP-02 OpenAI 가중치를 다른 활성화 함수로 불러왔다

- **증상.** 흑백 사진으로 소재 8종을 맞히는 내부 점검에서 정확도가 기대보다 낮았다(72.5%).
- **원인.** OpenAI CLIP 가중치는 QuickGELU로 학습됐는데 `ViT-B-32`(GELU) 설정으로 불렀다.
- **해결.** `MODEL_NAME = "ViT-B-32-quickgelu"`, `pretrained="openai"`로 바꾸고, 옛 설정으로 만든 인덱스를 거부하도록 인덱스 규칙 이름도 바꿨다(`fashion_planner_inventory/clip_matcher.py`).
- **검증.** 바꾼 뒤 같은 점검에서 76.8%가 기록됐다. 다만 표본·정답·실행 로그가 남아 있지 않아 개선 폭은 미검증으로 본다. 방향은 OpenAI 가중치에 `-quickgelu` 정의를 써야 한다는 OpenCLIP 문서와 맞다. 규칙이 다른 인덱스는 거부된다(`no_index`). `tests/test_prepare_cache.py`, `tests/test_clip_matcher.py`가 확인한다.
- **교훈.** 사전 학습 가중치에는 모델 구조 조건이 따라온다. 모델 설정을 산출물(인덱스의 `rule`)에 기록해 어긋나면 바로 실패하게 하고, 숫자를 보고할 때는 평가 세트도 함께 남긴다.
- **커밋.** [`6a8ea5d`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/6a8ea5d9125e41af6f31716fbc8dfa30cbbc2fd2)

<a id="t-clip-03"></a>
## T-CLIP-03 CLIP 인덱스를 불러오지 못했다

- **증상.** `clip_index.npz`를 불러오다 실패했다.
- **원인.** 인덱스 빌더가 `article_ids`를 NumPy object 배열로 저장했고, `np.load(..., allow_pickle=False)`는 이를 거부한다.
- **해결.** 처음에는 `allow_pickle=True`로 읽고 빌더가 문자열 배열로 저장하게 고쳤다. 인덱스를 다시 만든 뒤 읽는 쪽은 `allow_pickle=False`로 되돌렸다([`7de9df4`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/7de9df4d67733526b7c8d0a547f83d69fa08e21a)).
- **검증.** `fashion_planner_inventory/clip_matcher.py`는 `allow_pickle=False`로 읽고, `scripts/build_clip_index.py`는 `np.array(ids)`(문자열 dtype)로 저장한다.
- **교훈.** pickle을 풀면 코드가 실행될 수 있다. 읽는 쪽이 pickle을 쓰지 않아도 되게 쓰는 쪽을 고치고, 느슨하게 만든 로더를 남겨 두지 않는다.
- **커밋.** [`1a4929d`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/1a4929d9ec3c2ea09e1c9449942863d37bab29c2)

<a id="t-clip-04"></a>
## T-CLIP-04 CLIP이나 이미지 실패가 요청 전체 실패가 됐다

- **증상.** 모델을 불러오지 못하거나 기준 이미지를 받지 못하면 요청 오류가 됐다. 기준 이미지 URL은 상위 데이터에서 오므로 어디든 가리킬 수 있었다.
- **원인.** 선택 단계인 CLIP 주변에 실패 처리가 없었고, 서버가 받아 올 URL에 제한이 없었다.
- **해결.** CLIP이 실패하면 필터만 건너뛰고 이유를 남긴다(`search.clip.skip_reason: no_model`, `search.clip.image_skip`). 상품 이미지 URL은 https이고 허용된 호스트일 때만, 리다이렉트 없이, 최대 10MB까지 받는다.
- **검증.** `tests/test_clip_matcher.py`가 `no_model`, `host_not_allowed`, 비슷하게 생긴 호스트, 링크 로컬 주소를 확인한다.
- **교훈.** 부가 기능이 핵심 응답을 막아서는 안 된다. 데이터에서 온 URL은 신뢰할 수 없는 입력이다.
- **커밋.** [`bc909c3`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/bc909c3b94fe76a96fd321784a93a4b8fdd6cc4e)

<a id="t-cache-01"></a>
## T-CACHE-01 읽기 전용 캐시가 HTTP 500이 됐다

- **증상.** DB 캐시를 읽기 전용으로 붙이자 요청이 `500 INTERNAL_ERROR`로 실패했다.
- **원인.** 받은 상품 사진을 항상 `INVENTORY_CACHE_DIR/images`에 썼고, `mkdir`·`mkstemp`가 낸 `OSError`를 처리하지 않았다.
- **해결.** 이미지 캐시 위치를 `INVENTORY_IMAGE_CACHE_DIR`(기본 `$INVENTORY_CACHE_DIR/images`)로 분리하고 필요할 때 만든다. 사진을 먼저 받고 캐시는 가능할 때만 원자적으로 쓴다. 캐시에 쓰지 못해도 사진은 응답에 넣고 `trend_results[].limitations`에 적는다.
- **검증.** `tests/test_storage.py::test_unwritable_image_cache_serves_uncached_bytes_with_a_note`, `tests/test_runtime_modes.py::test_unwritable_image_cache_is_a_limitation_not_a_500`.
- **교훈.** 캐시는 최적화다. 캐시에 쓰지 못하면 속도를 잃어야지 정확성을 잃어서는 안 된다.
- **커밋.** [`dd6127c`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/dd6127cc35123a0e50ad77c00955ebb34d1ec115)

<a id="t-cache-02"></a>
## T-CACHE-02 DB 캐시를 채우는 작업이 없었다

- **증상.** 시작할 때 DB를 내려받지 않도록 바꾼 뒤, 새 머신에서는 `/health/ready`가 계속 503이었다. 문서는 여전히 시작할 때 내려받는다고 적고 있었다.
- **원인.** 설계상 시작할 때는 객체 메타데이터와 이미 있는 검증된 캐시만 확인한다. 그런데 그 캐시를 채우는 작업이 레포에 없었고, 문서도 옛 동작을 설명했다.
- **해결.** `python -m fashion_planner_inventory.server --prepare-cache`를 추가했다. 객체 버전마다 한 번 내려받아 크기를 확인하고 SHA-256 sidecar를 쓰고 파일을 원자적으로 교체한다. 이어 읽기 전용으로 열어 readiness를 검사하고, 비밀값 없이 JSON 한 줄을 출력한다. R2 문서도 고쳤다.
- **검증.** `tests/test_prepare_cache.py`(가짜 객체 저장소만 사용): 한 번 내려받은 뒤 재사용, sidecar 내용, 빈 파일·DuckDB가 아닌 파일·설정 누락에서 0이 아닌 종료 코드, 출력에 비밀값 없음.
- **교훈.** 숨은 부수 효과를 없앨 때는 명시적인 대체 수단과 문서 수정을 같은 변경에 넣는다.
- **커밋.** [`4e2b26d`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/4e2b26d2bd84c61ffb6ccff8ff7a076661b72d2e), [`4caa8da`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/4caa8dafb7fe609f111c52be441fe74e7b60d10c)

<a id="t-api-01"></a>
## T-API-01 트렌드가 많은 요청이 제한 시간을 넘겼다

- **증상.** 트렌드 11개 요청이 `504 TIMEOUT`을 받았다.
- **원인.** 경로의 제한 시간이 60초로 고정됐는데, 트렌드는 하나씩 순서대로 처리했다(당시 LLM 에이전트로 트렌드당 약 10초).
- **해결.** `INVENTORY_HANDOFF_TIMEOUT_SECONDS`를 추가했다(비었거나 (0, 600] 밖이면 60초). 통합 배포는 180초, 호출하는 쪽은 210초를 기다리게 해 서버가 클라이언트보다 먼저 응답하게 했다.
- **검증.** `tests/test_api.py`가 설정값 해석과, 설정한 초를 메시지에 담은 504 envelope을 확인한다.
- **교훈.** 서버 제한 시간은 언제나 호출자의 읽기 제한 시간보다 짧아야 한다. 504를 돌려줘도 이미 시작한 동기 작업은 계속 돈다. 제한 시간은 응답을 묶을 뿐 작업을 멈추지 않는다.
- **커밋.** [`17a9c90`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/17a9c907583f8e57029175a8a3e5f6ab9ed9e791)

<a id="t-docker-01"></a>
## T-DOCKER-01 이미지가 읽기 전용·비root 배포와 맞지 않았다

- **증상.** 통합 배포는 모든 컨테이너를 uid/gid 10001, 읽기 전용 루트 파일시스템, `HOME=/tmp`로 실행하는데, 이미지는 다른 사용자였고 캐시 폴더가 없었다.
- **원인.** Dockerfile이 홈 폴더가 있는 `app` 사용자를 만들었고 `/app` 밖에 쓸 수 있는 곳이 없었다.
- **해결.** 시스템 사용자·그룹 10001(홈 `/tmp`), 10001 소유의 `/cache`, `INVENTORY_CACHE_DIR=/cache`, `/app`에는 쓰지 않음, 외부 호출 없이 readiness를 확인하는 exec 형식 `HEALTHCHECK`.
- **검증.** 커밋에 기록된 수동 실행: `--read-only`, tmpfs `/tmp`와 `/cache`, 읽기 전용으로 붙인 fixture DB → 컨테이너 healthy, `/health/ready` 200, stub 요청 200, bearer 없으면 401.
- **교훈.** 가장 엄격한 실행 환경에 맞춰 이미지를 만든다. 나중에 푸는 것은 쉽고 조이는 것은 어렵다.
- **커밋.** [`bb4c6e7`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/bb4c6e7ee96685fe6d3832ceb6ae50736bf8b6d3)

<a id="t-contract-01"></a>
## T-CONTRACT-01 모든 실행이 inventory 단계에서 멈췄다

- **증상.** orchestrator가 모든 워크플로를 inventory 단계에서 `502 CONTRACT_VIOLATION`으로 멈췄다.
- **원인.** [`5912fc4`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/5912fc47991fdfb15c7151f9b775a9341903308b)이 생산자 결과에서 `trend_results[].attribute_summary`를 지웠는데, orchestrator가 가져다 쓰는 결과 스키마 사본은 여전히 이 필드를 필수로 요구했다.
- **해결.** 상위 50개 후보 속성 요약, 스키마 항목, 예시를 되살렸다.
- **검증.** `tests/test_contracts.py`가 실제 응답을 결과 스키마로 검증한다.
- **교훈.** 소비자가 검증하는 순간 생산자 필드는 계약이 된다. 필드는 소비자 스키마와 함께 지우고, 머지 전에 소비자 쪽 계약 검사를 돌린다.
- **커밋.** [`cdf0472`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/cdf04720f3ed6b5ed0c067234c76ea2342f41122)

<a id="t-design-01"></a>
## T-DESIGN-01 LLM이 갖지 않은 사실로 순위를 설명했다

- **증상.** 이전 버전은 도구를 호출하는 LLM(`gpt-4o-mini`)이 도구를 고르고 상품을 정하고 이유를 썼다. `why_not_higher` 설명에 도구 결과에 없는 소재·핏·스타일 근거를 썼고, 1위 상품에도 이 필드를 채웠다.
- **원인.** 모델이 도구 결과의 빈 곳을 그럴듯한 문장으로 채웠다. 프롬프트 수정이 세 번 이어졌다. 착샷 단계를 건너뛰었을 때 착샷 근거 추론 금지, 영역·색을 스스로 좁히기 금지([`5d2b818`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/5d2b8188be61a017ebebdd179d55fb5488feab1c)), 데이터 없는 소재·핏·스타일 근거 금지([`10954fd`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/10954fd5629ef327e470275d3ccfc858e0f4603e)), 1위에는 `why_not_higher`를 쓰지 않기([`46cccc0`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/46cccc09bf790543b761626cd3a842698c376295)). 마지막 수정은 코드가 최종 출력에서 그 필드를 버렸는데도 모델이 중간 단계에 계속 써 넣었기 때문에 필요했다.
- **해결.** 프롬프트를 더 고치는 대신 LLM 루프, 착샷 vision 분석, OpenAI 의존성을 지웠다. 지금은 정해진 순서의 SQL과 규칙이고, summary는 계산값만 채우는 문장 틀이며, 후보가 없으면 트렌드 색만 정해진 순서로 완화한다.
- **검증.** `tests/test_agent.py`가 완화 순서, "사용자 색·카테고리는 완화하지 않음" 규칙, summary 문장을 확인한다. 비용·지연·추천 품질의 제거 전후 비교는 없으므로, 규칙 방식이 그 지표에서 더 낫다고 주장하지 않는다.
- **교훈.** 입력이 이미 구조화되어 있고 도구 순서·제약·목표가 정해져 있으면, 요청마다 언어 모델에게 같은 단계를 고르게 해도 새 정보는 없고 데이터와 맞지 않는 설명이라는 실패 종류만 늘어난다. 추적해야 하는 설명은 문장 틀로 만든다. 자유 문장이 다시 필요하면 짧은 후보 목록에 대해 따로 검사하는 단계로 둔다.
- **커밋.** [`e6fcad9`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/e6fcad9365687cbc7dac97218bc4d237d1911ec1)

<a id="t-rank-01"></a>
## T-RANK-01 짝 카테고리가 MD의 코디 방식과 맞지 않았다

- **증상.** 상의를 기획하면 아우터가 나올 수 있었고, 하의를 기획하면 상의를 건너뛸 수 있었다.
- **원인.** 사용자 카테고리 1개에도 트렌드 카테고리와 같은 교집합 규칙을 썼다.
- **해결.** `AREA_PRIORITY`: 상의 기획은 하의만, 하의 기획은 상의를 먼저 찾고 색 완화 뒤에도 상의 후보가 없을 때만 아우터. 조회마다 찾은 영역을 남기고, 다음 영역으로 넘어가면 limitations에 적는다.
- **검증.** `tests/test_agent.py`(영역 우선순위 사례).
- **교훈.** 도메인의 순서는 명시적으로 적는다. 집합 연산은 선호를 숨긴다.
- **커밋.** [`ae10a61`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/ae10a611c1e25910252d1bc8a0774c0e4adbef6c)
