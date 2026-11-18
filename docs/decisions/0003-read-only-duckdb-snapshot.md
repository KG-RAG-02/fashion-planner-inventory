# 0003 읽기 전용 DuckDB 스냅샷과 검증된 캐시

- 상태: 채택
- 날짜: 2026-11-13

## 배경

이 서비스는 다시 빌드할 때만 바뀌는 데이터에 분석형 조회(필터, 조인, 약 3,200만 건 거래의 상품별 집계)를 합니다. 컨테이너는 읽기 전용 루트 파일시스템으로 돌고, 데이터를 이미지나 레포에 넣을 수 없습니다.

## 결정

- 데이터베이스는 DuckDB 파일 하나이고 `read_only=True`로 엽니다.
- 제공 방법은 두 가지입니다. local-file 모드(파일 경로) 또는 비공개 S3 호환 버킷.
- 버킷을 쓰면 시작할 때 객체 메타데이터와 SHA-256 sidecar가 맞는 기존 캐시만 확인하고, 내려받지는 않습니다. 별도 데이터 작업(`--prepare-cache`)이 객체 버전마다 한 번 내려받아 크기를 확인하고, 파일을 원자적으로 교체하고, sidecar를 씁니다.
- 검증된 DB가 열리기 전까지 readiness는 실패(503)이고 liveness는 계속 응답합니다.

## 결과

- 조회는 로컬에서 돌고 프로세스마다 한 버전만 봅니다.
- 새 데이터 버전은 새 객체와 재시작으로 반영합니다. 객체를 제자리에서 바꾸지 않습니다.
- 새 머신의 첫 시작에는 데이터 작업이 필요합니다. 대신 콜드 스타트가 원격 범위 읽기에 기대지 않습니다.
- SHA-256 sidecar는 로컬 손상을 잡아낼 뿐, 파일의 출처를 증명하지 않습니다.

## 검토한 대안

- DuckDB `httpfs`로 원격 파일을 범위 요청으로 읽기: 전체 다운로드는 피하지만 확장 설치와 원격 읽기가 요청 경로에 들어오고, 버전 고정이 원격 메타데이터에 달립니다.
- PostgreSQL이나 SQLite: 측정하지 않았습니다. 여러 곳에서 쓰는 재고 원장이 필요하면 트랜잭션 DB가 맞지만, 이 스냅샷에는 필요하지 않습니다.

## 출처

[cloudflare-r2.md](../cloudflare-r2.md), 커밋 [`7490887`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/74908879b8124ac43421fa9f647a1d0c4f0ac543)(R2 DB 연결), [`9676203`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/96762037b3d05661e984302c484a5bf41964e0e6)(local-file 모드), [`4e2b26d`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/4e2b26d2bd84c61ffb6ccff8ff7a076661b72d2e)(`--prepare-cache`), [`4caa8da`](https://github.com/KG-RAG-02/fashion-planner-inventory/commit/4caa8dafb7fe609f111c52be441fe74e7b60d10c)(문서 정정). 코드: `fashion_planner_inventory/storage.py`, `api.py`, `server.py`.
