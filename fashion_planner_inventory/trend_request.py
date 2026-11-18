"""trend 결과(TrendAnalysisResult) → inventory 내부 요청 (2026-11-08 결정)

orchestrator가 trend 결과(TrendAnalysisResult)를 그대로 inventory에 보낸다.
inventory 바깥 입구(API·multi.run_request)는 이 형식만 받는다. 입구에서 이 함수로 내부 형식으로 바꾸고,
안쪽 코드(변수 이름·타입)는 그대로 쓴다.

알아보는 방법: 요청에 applied_filters가 있으면 trend 결과로 본다. 없으면 INVALID_REQUEST.

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

trend status
    ok          위대로 바꿔서 처리
    no_results  DB를 조회하지 않고 no_results로 응답
    error       INVALID_REQUEST (reason=upstream_error)
"""

from __future__ import annotations

import copy
import hashlib
import json

UPSTREAM_STATUSES = ("ok", "no_results", "error")
UPSTREAM_SCHEMA_VERSION = "1.0.0"


def canonical_bytes(value) -> bytes:
    def check(obj):
        if isinstance(obj, dict):
            if any(not isinstance(key, str) for key in obj):
                raise ValueError("JSON object keys must be strings")
            for item in obj.values():
                check(item)
        elif isinstance(obj, list):
            for item in obj:
                check(item)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def input_provenance(source: dict) -> dict:
    return {"source_agent_id": "trend", "source_schema_version": source["schema_version"],
            "internal_schema_version": None, "adapter_version": "inventory-trend-input-adapter",
            "transformed": True, "source_payload_sha256": hashlib.sha256(canonical_bytes(source)).hexdigest()}


def validate_source(res: dict) -> None:
    """Validate consumed boundary fields, leaving the producer's full raw object intact.

    Full Trend Evidence semantics remain the upstream/retained consumer's responsibility.
    """
    canonical_bytes(res)
    if res.get("schema_version") != UPSTREAM_SCHEMA_VERSION:
        raise ValueError("지원하는 Trend schema_version은 1.1입니다.")
    if "agent_id" in res and res["agent_id"] != "trend":
        raise ValueError("Trend agent_id가 일치하지 않습니다.")
    if not isinstance(res.get("request_id"), str) or not res["request_id"].strip():
        raise ValueError("Trend request_id는 비어 있지 않은 문자열이어야 합니다.")
    if res.get("status") not in UPSTREAM_STATUSES:
        raise ValueError("지원하지 않는 Trend status입니다.")
    if not isinstance(res.get("trends"), list) or not all(isinstance(t, dict) for t in res["trends"]):
        raise ValueError("trends는 객체 목록이어야 합니다.")
    if res["status"] == "no_results" and res["trends"]:
        raise ValueError("no_results Trend에는 trends가 없어야 합니다.")
    af = res.get("applied_filters")
    if af is not None and not isinstance(af, dict):
        raise ValueError("applied_filters는 객체 또는 null이어야 합니다.")
    af = af or {}
    cats = af.get("categories")
    if cats is not None and not isinstance(cats, dict):
        raise ValueError("applied_filters.categories는 객체여야 합니다.")
    for name in ("include", "exclude"):
        refs = (cats or {}).get(name)
        if refs is not None and (not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs)):
            raise ValueError("categories include/exclude는 객체 목록이어야 합니다.")
        for ref in refs or []:
            if any(ref.get(k) is not None and not isinstance(ref[k], str) for k in ("raw", "major", "minor")):
                raise ValueError("category reference 값은 문자열 또는 null이어야 합니다.")
    if af.get("attributes") is not None and (not isinstance(af["attributes"], list)
                                             or not all(isinstance(a, dict) for a in af["attributes"])):
        raise ValueError("attributes는 객체 목록이어야 합니다.")
    for name in ("evidence", "limitations"):
        if name in res and not isinstance(res[name], list):
            raise ValueError(f"{name}는 목록이어야 합니다.")
    for trend in res["trends"]:
        if trend.get("style") is not None and not isinstance(trend["style"], dict):
            raise ValueError("trend.style은 객체 또는 null이어야 합니다.")
        items = trend.get("items")
        if items is not None and (not isinstance(items, list) or not all(isinstance(item, dict) for item in items)):
            raise ValueError("trend.items는 객체 목록이어야 합니다.")


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
    """trend 결과 → (내부 요청, trend status, 메모).

    메모: trend status가 ok가 아닐 때 응답 summary에 쓸 문장, 또는 None.
    trend status 값이 이상하면 ValueError.
    """
    validate_source(res)
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
    # The complete source object remains in the orchestrator's preceding trend stage trace.
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
    return copy.deepcopy(req), status, note


def build_trend_result(request_id: str, trends: list[dict], *, year: int = 2027, season: str | None = "SS",
                       origin: str = "user_requested", include: list[str] | None = None,
                       exclude: list[str] | None = None, colors: list[str] | None = None,
                       gender: str | None = None, status: str = "ok") -> dict:
    """trend 결과 모양의 요청을 만든다 (스크립트·테스트용). 카테고리 이름은 영역이면 major, 세부면 minor로 넣는다."""
    from fashion_planner_inventory.overstock import CATEGORY_TO_AREA, MATCHING_AREAS

    def ref(name):
        if name in MATCHING_AREAS:
            return {"raw": name, "major": name, "minor": None}
        return {"raw": name, "major": CATEGORY_TO_AREA.get(name), "minor": name if name in CATEGORY_TO_AREA else None}

    af = {"season": {"year": year, "season": season, "raw": None, "origin": origin} if season else None,
          "categories": {"include": [ref(x) for x in include or []], "exclude": [ref(x) for x in exclude or []]},
          "gender": gender,
          "attributes": [{"type": "color", "value": c} for c in colors or []]}
    return {"schema_version": UPSTREAM_SCHEMA_VERSION, "request_id": request_id, "status": status, "applied_filters": af, "trends": trends,
            "evidence": [], "limitations": []}
