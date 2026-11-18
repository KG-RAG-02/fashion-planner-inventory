# 다이어그램 근거표

`docs/diagrams/src/*.dot`의 모든 노드와 엣지, 그리고 그것을 뒷받침하는 코드입니다.
줄 번호는 다이어그램을 렌더한 트리 기준입니다. 코드를 고친 뒤에는 "확인 방법" 열의 테스트를 돌리고 줄 번호를 다시 확인합니다.

## inventory-pipeline (요청 처리 흐름)

| 요소 | 근거 file:line | 확인 방법 |
|---|---|---|
| orchestrator → `POST /v1/agents/inventory/runs`, `input` = trend 결과인 envelope | `fashion_planner_inventory/api.py:47-48`, `:353-375` | `tests/test_api.py` handoff 테스트 |
| 본문을 읽기 전에 Bearer 인증 | `fashion_planner_inventory/api.py:271-280` | `tests/test_api.py::test_auth_precedes_even_malformed_json` |
| 엄격한 JSON: 중복 key, NaN/Infinity 거부 | `fashion_planner_inventory/api.py:283-298` | `tests/test_api.py::test_ambiguous_json_rejected` |
| 요청 제한 시간 | `fashion_planner_inventory/api.py:53-68`, `:307-321` | `tests/test_api.py::test_handoff_deadline_returns_504_envelope` |
| trend 결과 검증, `applied_filters` → 내부 요청 | `fashion_planner_inventory/trend_request.py:56-100`, `:127-172` | `tests/test_trend_request.py` |
| 원본 payload SHA-256 (provenance) | `fashion_planner_inventory/trend_request.py:36-54`, `fashion_planner_inventory/multi.py:64-69` | `tests/test_api.py::test_handoff_accepts_trend_result_and_records_adapter_provenance` |
| upstream `no_results`/`error` → DB 조회 없음 | `fashion_planner_inventory/api.py:234-242` | `tests/test_api.py::test_db_unavailable_is_alive_and_upstream_no_results_skips_database` |
| 트렌드마다, 요청 순서대로 | `fashion_planner_inventory/multi.py:103-140` | `tests/test_multi.py` |
| 1 짝 카테고리: 사용자 상의 → 하의, 하의 → 상의 다음 아우터 | `fashion_planner_inventory/overstock.py:29-45`(`MATCHING_AREAS`, `AREA_PRIORITY`), `:108`; `fashion_planner_inventory/agent.py:731-741` | `tests/test_agent.py`(영역 우선순위 사례) |
| 2 색 매칭: 가장 많이 겹치는 색 + 톤온톤 | `fashion_planner_inventory/overstock.py:146-175`(`COLOR_PAIRS`), `:213-243` | `tests/test_overstock.py` |
| 3 과잉재고 조회: `virtual_stock JOIN article_attrs`, 영역·색·시즌·고객군·재고 > 0 | `fashion_planner_inventory/overstock.py:256-297` | `tests/test_overstock.py` |
| 3 완화: 트렌드 색만(max_overlap → any → none), 그다음 짝 영역 다음 단계 | `fashion_planner_inventory/agent.py:618-637`, `:776-795`; `fashion_planner_inventory/overstock.py:244` | `tests/test_agent.py` 완화 테스트 |
| 4 최근 판매 추세: 4주 vs 4주, 또래 대비 | `fashion_planner_inventory/overstock.py:302-370`, `fashion_planner_inventory/agent.py:807-816` | `tests/test_overstock.py` |
| 5 기준 이미지가 있을 때만 CLIP 필터 | `fashion_planner_inventory/agent.py:817-823` | `tests/test_agent.py` |
| 5 흑백 이미지 0.6 + 소재·디테일 텍스트 0.4(벡터 결합) | `fashion_planner_inventory/clip_matcher.py:54-55`, `:93-133` | `tests/test_clip_matcher.py` |
| 5 색마다 상위 10%, 최소 1개 | `fashion_planner_inventory/clip_matcher.py:56-57`, `:355` | `tests/test_clip_matcher.py` |
| 5 → 6 건너뛴 경우: 이유 기록, 후보 그대로 | `fashion_planner_inventory/clip_matcher.py:279-294`(`ClipInfo`), `:316-354`; `fashion_planner_inventory/agent.py:677-702` | `tests/test_clip_matcher.py`, `tests/test_agent.py` |
| 기준 이미지: 착샷 먼저, 없으면 상품 이미지 | `fashion_planner_inventory/snap.py:25-27`, `:48-78` | `tests/test_snap.py` |
| 착샷은 객체 저장소, 상품 이미지는 https·허용 목록만 | `fashion_planner_inventory/clip_matcher.py:135-215` | `tests/test_clip_matcher.py` 허용 목록 사례 |
| CLIP 인덱스(npz), 규칙 확인 | `fashion_planner_inventory/clip_matcher.py:53`, `:233-275`; `fashion_planner_inventory/api.py:137-165` | `tests/test_clip_matcher.py`, `tests/test_prepare_cache.py` |
| 6 순위: 4지표 백분위 각 0.25, 동점은 article_id 순 | `fashion_planner_inventory/overstock.py:372-401` | `tests/test_overstock.py` |
| 7 사진이 있는 최상위 상품 | `fashion_planner_inventory/agent.py:518-542` | `tests/test_agent.py` |
| 7 reason, 문장 틀 summary, 근거 E-001~E-006 | `fashion_planner_inventory/agent.py:87-108`, `:365-404`, `:450-477` | `tests/test_contracts.py` |
| 상품 사진: 로컬 폴더 또는 객체 저장소 | `fashion_planner_inventory/api.py:250-253`, `fashion_planner_inventory/images.py:36-62`, `fashion_planner_inventory/storage.py` | `tests/test_images.py`, `tests/test_storage.py` |
| DuckDB 스냅샷을 읽기 전용으로 엶 | `fashion_planner_inventory/api.py:99-103` | `tests/test_runtime_modes.py` |
| 트렌드 순서대로 이어 붙임, `products[i]` ↔ `trend_results[i]`, 공통 evidence, status | `fashion_planner_inventory/multi.py:121-172`, `:198-`(`validate_response`) | `tests/test_multi.py` |

## data-build (데이터 빌드 흐름)

| 요소 | 근거 file:line | 확인 방법 |
|---|---|---|
| Kaggle H&M CSV(또는 Parquet) → `build_db.py`: articles, transactions(2020-09-22까지) | `scripts/build_db.py:19-50` | 스크립트 읽기(합성 CSV·Parquet로 확인한 기록은 커밋 메시지) |
| Kaggle 이미지 zip → `extract_images.py`, 의류 article_id는 DB에서 | `scripts/extract_images.py:1-30` | 스크립트 읽기 |
| `build_attributes.py` → `article_attrs` | `scripts/build_attributes.py:19-35`, `fashion_planner_inventory/attributes.py` | `tests/test_attributes.py` |
| `build_virtual_stock.py` → `virtual_stock`(고정 시드) | `scripts/build_virtual_stock.py:20-37`, `fashion_planner_inventory/synthetic.py:28-83` | `tests/test_synthetic.py` |
| `build_clip_index.py`: DB + 사진(+ 가중치) → 규칙 이름이 든 `clip_index.npz` | `scripts/build_clip_index.py:1-15`, `:160-165`; `fashion_planner_inventory/clip_matcher.py:79-92` | 스크립트 읽기 |
| OpenCLIP 가중치는 실행 중 내려받음 | `fashion_planner_inventory/clip_matcher.py:79-92`, `Dockerfile:15-22` | — |
| Hugging Face parquet → `extract_snap_images.py` → `snap_images/<revision>/<row>.jpg` | `scripts/extract_snap_images.py:27-45` | 스크립트 읽기 |
| `upload_snap_images.py` → 자기 버킷(쓰기 키) | `scripts/upload_snap_images.py:28-79` | 스크립트 읽기 |
| 객체 저장소 → API: `--prepare-cache`(DB, CLIP 인덱스), 요청마다 착샷 | `fashion_planner_inventory/server.py:26-95`, `fashion_planner_inventory/api.py:79-92`, `:156-165` | `tests/test_prepare_cache.py` |
| local-file 모드 | `fashion_planner_inventory/api.py:74-88`, `fashion_planner_inventory/storage.py:141`(`open_local_database`) | `tests/test_runtime_modes.py` |
| 오프라인 stub용 합성 `fixture_db` | `fashion_planner_inventory/fixture_db.py:60-145` | `tests/test_fixture_db.py` |
