"""시즌 매핑 규칙

MD가 요청한 시즌(예: "2026 봄", "2026-SS")을 H&M 데이터 안의 기준 기간으로 바꾼다.

규칙
- H&M 데이터 범위: 2018-09-20 ~ 2020-09-22 (분석 기준일)
- 데이터 안에 온전히 들어 있는 같은 시즌을 모두 기준 기간으로 쓴다.
  (시즌 중간에 데이터가 시작·끝나는 기간은 쓰지 않는다)
- 요청 시즌 자체가 데이터 안에 온전히 있으면(예: "2019 봄") 그 기간만 쓴다.

결과 예
- 2026 봄  → 2019-03-01~05-31, 2020-03-01~05-31
- 2026 여름 → 2019-06-01~08-31, 2020-06-01~08-31
- 2026 가을 → 2019-09-01~11-30            (2018 가을은 9/20 시작, 2020 가을은 9/22 끝이라 제외)
- 2026 겨울 → 2018-12-01~2019-02-28, 2019-12-01~2020-02-29
- 2026-SS  → 2019-03-01~08-31, 2020-03-01~08-31
- 2026-FW  → 2019-09-01~2020-02-29

근거(transactions 직접 집계): 2020년 봄은 매장 휴업으로 판매가 온라인으로 옮겨갔지만,
판매 건수 감소폭은 코로나 이전(1~2월)과 비슷했고 상품 구성도 대부분 ±1%p 안이었다.

여러 해를 합칠 때 주의
- 판매 '구성'(비중)은 기간 전체를 합쳐서 계산하면 된다.
- 판매 '양'을 다른 값과 비교할 때는 n_periods로 나눠 시즌 1회 평균으로 쓴다.

최근 판매 추세(최근 4주 등)는 이 규칙이 아니라 분석 기준일(DATA_END)에서 거꾸로 센다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

DATA_START = date(2018, 9, 20)
DATA_END = date(2020, 9, 22)  # 분석 기준일

# 시즌 → (시작 월, 끝 월). 끝 월 < 시작 월이면 해를 넘긴다.
SEASON_MONTHS: dict[str, tuple[int, int]] = {
    "spring": (3, 5),
    "summer": (6, 8),
    "fall": (9, 11),
    "winter": (12, 2),
    "SS": (3, 8),
    "FW": (9, 2),
}

SEASON_LABELS_KO = {
    "spring": "봄", "summer": "여름", "fall": "가을", "winter": "겨울",
    "SS": "SS(봄·여름)", "FW": "FW(가을·겨울)",
}

_ALIASES = {
    "봄": "spring", "spring": "spring",
    "여름": "summer", "summer": "summer",
    "가을": "fall", "fall": "fall", "autumn": "fall",
    "겨울": "winter", "winter": "winter",
    "ss": "SS", "s/s": "SS",
    "fw": "FW", "f/w": "FW", "aw": "FW", "a/w": "FW",
}

_PATTERN = re.compile(r"^\s*(\d{4})\s*[-_ ]?\s*([A-Za-z/가-힣]+)\s*$")


@dataclass(frozen=True)
class SeasonPeriod:
    target_year: int
    season: str                           # spring / summer / fall / winter / SS / FW
    ranges: tuple[tuple[date, date], ...]  # 기준 기간들 (시작, 끝) 포함, 오래된 순
    is_substitute: bool                   # True면 요청 시즌 대신 과거 같은 시즌을 사용
    warnings: list[str] = field(default_factory=list)

    @property
    def n_periods(self) -> int:
        """합친 시즌 수. 판매량을 시즌 1회 평균으로 바꿀 때 나눈다."""
        return len(self.ranges)

    @property
    def days(self) -> int:
        """전체 기간 일수."""
        return sum((e - s).days + 1 for s, e in self.ranges)

    @property
    def basis(self) -> str:
        """응답 metrics.basis에 넣을 설명 문장."""
        label = f"{self.target_year} {SEASON_LABELS_KO[self.season]}"
        spans = ", ".join(f"{s.isoformat()}~{e.isoformat()}" for s, e in self.ranges)
        if self.is_substitute:
            return f"{label} 요청 → 데이터 안의 같은 시즌 {self.n_periods}회({spans}) 판매 패턴으로 대체"
        return f"{label} 실제 판매 기간 {spans}"

    def to_period(self) -> dict:
        """metrics.period 형식(제안안). 기간이 여러 개라 ranges 목록으로 준다."""
        return {"ranges": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in self.ranges]}

    def sql_filter(self, column: str = "t_dat") -> tuple[str, list[date]]:
        """읽기 전용 SQL 템플릿용 조건절과 파라미터. 값은 문자열로 끼워 넣지 않는다."""
        if not column.isidentifier():
            raise ValueError(f"허용되지 않는 컬럼 이름: {column!r}")
        clause = " OR ".join(f"{column} BETWEEN ? AND ?" for _ in self.ranges)
        params = [d for r in self.ranges for d in r]
        return f"({clause})", params


def parse_season(text: str) -> tuple[int, str]:
    """'2026 봄', '2026-SS', '2026 S/S', '2026FW' 등을 (연도, 시즌 키)로 바꾼다."""
    m = _PATTERN.match(text)
    if not m:
        raise ValueError(f"시즌 형식을 알 수 없음: {text!r} (예: '2026 봄', '2026-SS')")
    year, raw = int(m.group(1)), m.group(2).lower()
    if raw not in _ALIASES:
        raise ValueError(f"알 수 없는 시즌: {m.group(2)!r} (봄/여름/가을/겨울/SS/FW)")
    return year, _ALIASES[raw]


def _span(year: int, season: str) -> tuple[date, date]:
    start_m, end_m = SEASON_MONTHS[season]
    end_year = year + 1 if end_m < start_m else year
    start = date(year, start_m, 1)
    end = date(end_year, end_m, calendar.monthrange(end_year, end_m)[1])
    return start, end


def _inside_data(start: date, end: date) -> bool:
    return start >= DATA_START and end <= DATA_END


def map_season(text: str) -> SeasonPeriod:
    """요청 시즌 → H&M 기준 기간(들)."""
    year, season = parse_season(text)

    start, end = _span(year, season)
    if _inside_data(start, end):
        return SeasonPeriod(year, season, ((start, end),), is_substitute=False)

    ranges = tuple(
        span
        for y in range(DATA_START.year, DATA_END.year + 1)
        if _inside_data(*(span := _span(y, season)))
    )
    if not ranges:
        raise ValueError(f"{text!r}에 쓸 수 있는 기준 기간이 데이터 안에 없음")

    spans = ", ".join(f"{s.isoformat()}~{e.isoformat()}" for s, e in ranges)
    warnings = [
        f"{year} {SEASON_LABELS_KO[season]} 실제 판매 데이터가 없어 "
        f"같은 시즌 {len(ranges)}회({spans}) 패턴으로 대체했습니다."
    ]
    return SeasonPeriod(year, season, ranges, is_substitute=True, warnings=warnings)
