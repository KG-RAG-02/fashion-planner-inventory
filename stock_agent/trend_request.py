"""02 트렌드 결과(TrendAnalysisResult) → 03 내부 요청 (2026-11-08 결정)

02 쿼리 전처리·트렌드 에이전트가 자기 결과(trend_analysis_result_v1)를 그대로 03에 보낸다(push).
03 바깥 입구(API·multi.run_request)는 이 형식만 받는다. 입구에서 이 함수로 내부 형식으로 바꾸고,
안쪽 코드(변수 이름·타입)는 그대로 쓴다.

알아보는 방법: 요청에 applied_filters가 있으면 02 결과로 본다. 없으면 INVALID_REQUEST.

바꾸는 값
    request_id                          → request_id (그대로 돌려줌)
    applied_filters.season              → season ({year, season, raw, origin})
    applied_filters.categories.include  → target_category  (CategoryRef마다 minor, 없으면 major)
    applied_filters.categories.exclude  → exclude_categories (같은 방식)
    applied_filters.attributes(color)   → target_colors
    applied_filters.attributes(material·fit·pattern·detail) → 쓰지 않음 (사용자 소재·핏은 무시, 결정)
    applied_filters.gender              → gender (고객군은 agent.read_segments: 여 → Ladieswear+Divided,
                                          남 → Menswear+Divided, 공용 → 전체. 없으면 트렌드마다 Item gender)
    trends                              → trends (그대로)

02 status
    ok          위대로 바꿔서 처리
    no_results  DB를 조회하지 않고 no_results로 응답
    error       INVALID_REQUEST (reason=upstream_error)
"""

from __future__ import annotations

UPSTREAM_STATUSES = ("ok", "no_results", "error")


def is_trend_result(req: dict) -> bool:
    return isinstance(req, dict) and "applied_filters" in req


def _cat_names(refs) -> list[str]:
    out = []
    for r in refs or []:
        if isinstance(r, dict):
            name = r.get("minor") or r.get("major")
            if not name:          # 표준 값으로 못 바꾼 카테고리(raw만 있음)는 쓸 수 없다
                continue
        elif isinstance(r, str):
            name = r
        else:
            continue
        if name not in out:
            out.append(name)
    return out


def _unmapped(refs) -> list[str]:
    return [r.get("raw") for r in refs or [] if isinstance(r, dict) and not (r.get("minor") or r.get("major"))
            and r.get("raw")]


def from_trend_result(res: dict) -> tuple[dict, str, str | None]:
    """02 결과 → (내부 요청, 02 status, 메모).

    메모: 02 status가 ok가 아닐 때 응답 summary에 쓸 문장, 또는 None.
    02 status 값이 이상하면 ValueError.
    """
    status = res.get("status")
    if status not in UPSTREAM_STATUSES:
        raise ValueError(f"트렌드 결과의 status는 {', '.join(UPSTREAM_STATUSES)} 중 하나여야 합니다: {status!r}")
    af = res.get("applied_filters") or {}
    if not isinstance(af, dict):
        raise ValueError("applied_filters는 객체여야 합니다.")
    cats = af.get("categories") or {}
    include, exclude = cats.get("include") or [], cats.get("exclude") or []
    colors = []
    for a in af.get("attributes") or []:
        if isinstance(a, dict) and a.get("type") == "color" and a.get("value") and a["value"] not in colors:
            colors.append(a["value"])

    # schema_version belongs to TrendAnalysisResult, not Stock's internal request.
    # The complete source object remains in Project_08's preceding Trend stage trace.
    req = {"request_id": res.get("request_id"), "trends": res.get("trends") or []}
    if af.get("season") is not None:
        req["season"] = af["season"]
    if _cat_names(include):
        req["target_category"] = _cat_names(include)
    if _cat_names(exclude):
        req["exclude_categories"] = _cat_names(exclude)
    if colors:
        req["target_colors"] = colors
    if af.get("gender"):
        req["gender"] = af["gender"]
    unmapped = _unmapped(include) + _unmapped(exclude)
    if unmapped:
        req["_input_notes"] = [f"표준 카테고리로 바뀌지 않은 요청 카테고리는 쓰지 않았습니다: {', '.join(unmapped)}"]

    note = None
    lim = [x for x in (res.get("limitations") or []) if isinstance(x, str)]
    if status == "no_results":
        note = "트렌드 분석 결과가 없어(no_results) 재고를 조회하지 않았습니다."
    elif status == "error":
        note = "트렌드 분석이 오류(error)를 돌려줘 재고를 조회하지 않았습니다."
    if note and lim:
        note += " 트렌드 분석 한계: " + " / ".join(lim)
    return req, status, note


def build_trend_result(request_id: str, trends: list[dict], *, year: int = 2027, season: str | None = "SS",
                       origin: str = "user_requested", include: list[str] | None = None,
                       exclude: list[str] | None = None, colors: list[str] | None = None,
                       gender: str | None = None, status: str = "ok") -> dict:
    """02 결과 모양의 요청을 만든다 (스크립트·테스트용). 카테고리 이름은 영역이면 major, 세부면 minor로 넣는다."""
    from stock_agent.overstock import CATEGORY_TO_AREA, MATCHING_AREAS

    def ref(name):
        if name in MATCHING_AREAS:
            return {"raw": name, "major": name, "minor": None}
        return {"raw": name, "major": CATEGORY_TO_AREA.get(name), "minor": name if name in CATEGORY_TO_AREA else None}

    af = {"season": {"year": year, "season": season, "raw": None, "origin": origin} if season else None,
          "categories": {"include": [ref(x) for x in include or []], "exclude": [ref(x) for x in exclude or []]},
          "gender": gender,
          "attributes": [{"type": "color", "value": c} for c in colors or []]}
    return {"request_id": request_id, "status": status, "applied_filters": af, "trends": trends,
            "evidence": [], "limitations": []}
