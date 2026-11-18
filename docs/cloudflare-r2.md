# 객체 저장소(Cloudflare R2) 사용

객체 저장소는 선택 사항입니다. 로컬 파일만으로 실행하려면 `INVENTORY_DB_PATH`(local-file 모드)를 쓰면 되고, 이 문서의 설정은 필요 없습니다. 아래는 비공개 S3 호환 버킷(Cloudflare R2에서 확인)에 DB·사진·CLIP 인덱스를 두는 경우입니다. 결정 배경은 [ADR 0003](decisions/0003-read-only-duckdb-snapshot.md)에 있습니다.

## DB

API는 비공개 버킷의 DuckDB 파일을 체크섬으로 검증한 로컬 캐시로 읽습니다.

- 시작할 때는 객체 메타데이터(HEAD)만 확인하고, 그 객체 버전과 SHA-256 sidecar가 맞는 캐시가 있으면 씁니다. **시작할 때 내려받지 않습니다.** 캐시가 없거나 낡았으면 `/health/ready`는 503입니다.
- 캐시는 별도 데이터 작업 `python -m fashion_planner_inventory.server --prepare-cache`가 채웁니다. 없는 버전이나 바뀐 버전을 내려받아 바이트 수를 확인하고, 임시 파일을 원자적으로 옮긴 뒤 sidecar를 씁니다.
- API는 캐시를 DuckDB `read_only=True`로 엽니다. 요청 처리 중에는 DB를 내려받지 않습니다.
- 재시작이 잦으면 `INVENTORY_CACHE_DIR`에 영구 볼륨을 붙이세요. 그래도 캐시를 쓰기 전에 원격 ETag/버전을 확인합니다.
- 새 데이터 버전은 새 객체 버전(또는 새 key)으로 올리고 API를 다시 시작하거나 다시 배포합니다. 실행 중에 객체를 제자리에서 바꾸지 마세요. 인스턴스마다 자기 로컬 캐시와 읽기 전용 연결을 갖습니다.

DuckDB `httpfs` 확장은 S3에 있는 DB를 범위 읽기로 직접 열 수 있고, 문서에 Cloudflare R2도 동작할 서비스로 적혀 있습니다([DuckDB S3 API 지원](https://duckdb.org/docs/current/core_extensions/httpfs/s3api)). 첫 시작의 전체 다운로드는 피하지만, 확장 설치와 원격 범위 읽기가 실제 조회 경로에 들어오고 버전 고정이 원격 메타데이터와 캐시 설정에 달립니다. 그래서 로컬 스냅샷을 택했습니다.

## 상품 사진

`R2_IMAGE_PREFIX`가 있으면 API가 필요할 때 상품 사진 객체를 확인하고, 고른 사진만 캐시합니다.

- key 모양은 `<prefix>/<article_id 앞 3자리>/<article_id>.jpg`입니다. 예: `images/099/0999991001.jpg`(합성 ID).
- 존재 확인 결과는 프로세스 안에서 30초 동안 캐시합니다.
- 받은 사진은 `INVENTORY_IMAGE_CACHE_DIR`(기본 `$INVENTORY_CACHE_DIR/images`)에 캐시합니다. DB 캐시를 읽기 전용으로 붙였을 때처럼 쓸 수 없으면, 요청을 실패시키지 않고 사진을 캐시 없이 돌려주며 트렌드 결과의 `limitations`에 적습니다.
- 사진이 없거나 받을 수 없어도 상품 결과는 나옵니다. prefix가 없으면 `INVENTORY_IMAGE_DIR`을 로컬 폴백으로 씁니다.

응답에는 고른 상품 사진이 base64로 들어갑니다. 원본 DB와 사진 모음은 Git이나 도커 이미지에 넣지 않습니다.

## CLIP 인덱스

`R2_CLIP_INDEX_KEY`는 CLIP 인덱스 객체(`scripts/build_clip_index.py`로 만든 npz, 규칙 `vit-b-32-quickgelu-openai/gray-image+material-detail-en`)입니다.

- `--prepare-cache`가 객체 버전마다 한 번 DB 옆 `INVENTORY_CACHE_DIR/clip-index-<sha256>.npz`로 내려받습니다.
- API는 시작할 때 이 캐시를 읽고, 내려받지 않습니다.
- 인덱스가 없거나 규칙이 다르면 서버는 뜨고 CLIP 필터만 건너뜁니다(`search.clip.skip_reason: no_index`).
- 로컬 파일을 쓰려면 `INVENTORY_CLIP_INDEX_PATH`를 설정합니다.

## 무신사 착샷

trend 결과의 착샷(`kind: snap_garment`)에는 `hf://datasets/disl/musinsa-snap-new@<revision>#row=<n>` 형태의 `image_ref`만 있습니다.

- 그 revision의 착샷 JPEG를 `<R2_SNAP_IMAGE_PREFIX>/<revision>/<n>.jpg`로 둡니다. 만들기는 `scripts/extract_snap_images.py`, 올리기는 `scripts/upload_snap_images.py`이고, 올릴 때만 쓰기 권한 키를 따로 줍니다([데이터 카드](data.md#선택-착샷-기준-이미지)).
- API는 요청마다 착샷 1장을 필요할 때 읽고 캐시하지 않습니다.
- 다른 revision을 가리키면 객체가 없으므로 텍스트만으로 CLIP을 겁니다(`search.clip.image_skip: not_found`). prefix가 없으면 `no_snap_store`입니다.

## 설정과 보안

| 변수 | 용도 |
|---|---|
| `R2_ACCOUNT_ID`, `R2_ENDPOINT`(선택) | S3 호환 endpoint. 관할 구역(jurisdiction) 버킷은 `R2_ENDPOINT`가 필요합니다. |
| `R2_BUCKET`, `R2_DB_KEY` | DB 객체 |
| `R2_IMAGE_PREFIX` | 상품 사진(선택) |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | S3 호환 API 자격 증명(읽기 전용) |
| `R2_CLIP_INDEX_KEY` | CLIP 인덱스 객체(선택) |
| `R2_SNAP_IMAGE_PREFIX` | 무신사 착샷(선택) |
| `INVENTORY_CACHE_DIR` | 로컬 영구 캐시 폴더 |
| `INVENTORY_IMAGE_CACHE_DIR` | 받은 상품 사진 캐시(기본 `$INVENTORY_CACHE_DIR/images`) |

- SDK region은 `auto`입니다.
- 토큰은 필요한 버킷에 `Object Read only` 권한만 줍니다. 권한과 관할 구역 endpoint는 Cloudflare [R2 인증 문서](https://developers.cloudflare.com/r2/api/tokens/)에 있습니다.
- 키 값은 배포 비밀 저장소에 둡니다. 자격 증명, 서명된 URL, 제공자 예외 메시지를 로그에 남기지 않습니다.
- `/health`는 제공자와 비밀이 아닌 버전 식별자만 보여 주고, 버킷·key 경로나 자격 증명은 보여 주지 않습니다.
- 계정 ID, 버킷 이름, 객체 key, 자격 증명 값은 로컬 `.env`나 배포 비밀 저장소에 두고, 이 문서나 `.env.example`에는 적지 않습니다.

`INVENTORY_DB_PATH`를 설정하면 local-file 모드입니다. R2 설정과 HEAD를 건너뛰고 그 DuckDB 파일을 읽기 전용으로 열며, `<이름>.sha256` sidecar가 있으면 확인합니다. 이때 `/health`는 `db: local-file`, `db_version: sha256:<파일 해시>`를 보여 주고, 사진은 `INVENTORY_IMAGE_DIR`에서 읽습니다. 로컬 빌드 스크립트도 같은 변수를 씁니다.

## 확인

- `python scripts/diagnose_readiness.py`: 값을 출력하지 않고 설정이 있는지, R2 HEAD, 기존 캐시, 읽기 전용 스키마를 점검합니다. 내려받거나 만들거나 올리지 않습니다.
- `python -m fashion_planner_inventory.server --prepare-cache`: 객체 버전마다 한 번 내려받아(`prepare_database(allow_download=True)`) SHA-256 sidecar를 쓰고, 읽기 전용으로 열어 readiness를 검사한 뒤, DuckDB 라이브러리 버전과 파일 storage version을 JSON 한 줄로 출력합니다(자격 증명·endpoint·버킷·key 없음). DB가 준비되지 않으면 0이 아닌 코드로 끝납니다.
- 파일의 storage version보다 오래된 DuckDB는 그 파일을 열지 못합니다.
- `/health/live`는 생존, `/health/ready`는 스키마·스냅샷 readiness입니다. SHA sidecar는 로컬 손상을 잡을 뿐 원본의 진위를 증명하지 않습니다.
