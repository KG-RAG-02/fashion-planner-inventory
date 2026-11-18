# 변경 기록

이 파일은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 따르고, 버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

## [1.0.0] - 2026-11-18

첫 공개 버전입니다. 비공개로 개발하던 재고 서비스를 fashion-planner의 inventory 서비스로 정리해 공개했습니다.

### 추가

- trend 결과를 받아 트렌드마다 함께 제안할 과잉재고 상품 1개를 고르는 HTTP API(`POST /v1/agents/inventory/runs`, `inventory-handoff` envelope, bearer 인증).
- 짝 카테고리, 조합 색상표와 톤온톤, 트렌드 색만 정해진 순서로 완화, 또래 대비 판매 추세, 4지표 백분위 순위.
- 선택적 CLIP 필터(OpenCLIP ViT-B-32-quickgelu, 흑백 이미지 + 소재·디테일 텍스트, 색별 상위 10%)와 미리 만든 인덱스.
- 읽기 전용 DuckDB 스냅샷: local-file 모드, 또는 체크섬으로 검증한 객체 저장소 캐시와 `--prepare-cache` 데이터 작업.
- 합성 fixture DB와 오프라인 stub 요청·기대 응답, 계약 예시를 다시 만드는 `scripts/build_contract_examples.py`.
- Kaggle 원본 CSV를 바로 읽는 데이터 빌드 스크립트.
- 문서: 데이터 카드, 랭킹 규칙, 아키텍처와 다이어그램(근거표 포함), 트러블슈팅, 설계 결정 기록, 계약 안내.
- `pyproject.toml`(PEP 639), 오프라인 단위 테스트 CI, 공통 커뮤니티 파일.

### 비공개 개발본과 달라진 점

- 이름: 패키지 `fashion_planner_inventory`, agent_id `inventory`, 환경변수 `INVENTORY_*`와 공용 `FASHION_PLANNER_SERVICE_TOKEN`, 가상 재고 테이블 `virtual_stock`. 식별자 이름에서 순번을 빼고 버전은 `schema_version`(`1.0.0`) 한 곳에만 둡니다.
- 삭제: 레거시 경로 `/v1/inventory-products`, 토큰 별칭, 일회성 검증 스크립트, 내부 문서, 실제 데이터에서 만든 예시.
- 비공개 개발본의 DB·캐시·CLIP 인덱스와는 테이블·규칙 이름이 달라 호환되지 않습니다. 공개본은 데이터 카드의 절차로 새로 만듭니다.

[1.0.0]: https://github.com/KG-RAG-02/fashion-planner-inventory/releases/tag/v1.0.0
