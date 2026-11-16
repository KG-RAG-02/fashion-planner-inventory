"""설정 읽기: 현재 폴더의 .env (KEY=VALUE)

API·스크립트가 시작할 때 부른다. 이미 있는 환경변수는 덮어쓰지 않는다(프로세스 환경변수가 우선).
.env는 커밋하지 않는다 (.env.example 참고).
"""

from __future__ import annotations

import os
from pathlib import Path


def read_dotenv(path: str | Path = ".env") -> None:
    """KEY=VALUE 줄만 읽어 환경변수에 넣는다. 이미 있는 값은 덮어쓰지 않는다. 파일이 없으면 아무것도 안 함."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:      # 빈 값은 넣지 않음 (기본값을 빈 문자열로 덮지 않게)
            os.environ[k] = v
