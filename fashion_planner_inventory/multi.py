"""트렌드 n개 → agent.run()을 트렌드마다 돌려 결과 n개를 이어 붙인다 (2026-11-08 결정)

trend 서비스는 트렌드를 목록(trends)으로 넘긴다. 1개여도 목록이다.
트렌드 1개를 처리하는 규칙은 모두 agent.py에 있고, 여기서는 돌리고 이어 붙이기만 한다.

입구 (2026-11-08 결정: 바깥에서는 trend 형식만 받는다)
    run_request(trend 결과)   API·스크립트의 입구. trend 결과(TrendAnalysisResult) 그대로만 받는다.
                          trend_request.py가 내부 형식으로 바꾼 뒤 run_internal을 부른다.
    run_internal(내부 형식) 안쪽 함수. trend 결과를 바꾼 모양:
        {"request_id": "req_123",
         "season": {"year": 2027, "season": "SS", "origin": "user_requested"},
         "target_category": ["상의"], "exclude_categories": [...], "target_colors": [...], "gender": "여",
         "trends": [{"style": {...}, "items": [...]}, ...]}
        (segment는 안쪽 테스트·규칙 방식 검증용으로 고객군을 직접 줄 때만)

응답
    products        [상품 or null, ...]   트렌드 순서대로 n개. 상품을 못 고른 트렌드는 null
    trend_results   [trend_result, ...]   n개. trend_results[i] ↔ products[i] (product_index = i)
                    trend_results[i].evidence_ids = 그 트렌드의 근거 ID (evidence에서 찾음)
    evidence        트렌드마다 E-001~E-005(CLIP을 걸었으면 E-006). 이어 붙이면 ID가 겹쳐서 앞에 트렌드 ID를 붙임
                    (inventory:style_007:E-001), 항목마다 style_id
    status          모두 ok → ok / 일부만 상품 있음 → partial / 모두 오류 → error / 그 밖(상품 0개) → no_results
    summary         전체 한 줄 (트렌드별 문장은 trend_results[].summary)
    errors          공통 입력 오류, 또는 모든 트렌드가 오류일 때 한 줄 (트렌드별 오류는 trend_results[].errors)
                    code는 팀 공통 코드, inventory 안쪽 코드는 reason (errors.py)

공통 envelope의 findings·최상위 limitations는 쓰지 않는다 (2026-11-08 결정). 상품은 products, 트렌드별 결과는 trend_results.

같은 상품을 여러 트렌드가 고르면 products에 그대로 여러 번 들어간다(중복 허용).

clip_filter: CLIP 유사도 필터 함수(agent.run에 그대로 넘김). 기본은 clip_matcher.clip_filter, 테스트에서 바꾼다.
"""

from __future__ import annotations

from datetime import datetime

from fashion_planner_inventory.agent import (
    AGENT_ID,
    NS,
    SCHEMA_VERSION,
    STATUSES,
    check_common,
    check_product,
    check_trend_result,
    run,
)
from fashion_planner_inventory.errors import pick, to_team
from fashion_planner_inventory.trend_request import from_trend_result, input_provenance, is_trend_result


def error_response(req: dict, code: str, message: str, **extra) -> dict:
    return {"schema_version": SCHEMA_VERSION, "request_id": req.get("request_id"), "agent_id": AGENT_ID,
            "status": "error", "summary": message, "products": [], "trend_results": [], "season_basis": None,
            "evidence": [], "errors": [to_team({"code": code, "message": message, **extra})]}


def no_results_response(req: dict, message: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "request_id": req.get("request_id"), "agent_id": AGENT_ID,
            "status": "no_results", "summary": message, "products": [], "trend_results": [],
            "season_basis": None, "evidence": [], "errors": []}


def _record_input_provenance(result: dict, source: dict) -> dict:
    version = source.get("schema_version")
    if isinstance(version, str) and version:
        result["input_provenance"] = input_provenance(source)
    return result


def run_request(request: dict, con, image_dir: str | None = None, now: datetime | None = None,
                clip_filter=None, manifest_id: str | None = None) -> dict:
    """API·스크립트의 입구. trend 결과(TrendAnalysisResult) 그대로만 받는다.

    manifest_id: 근거 E-001~E-005의 manifest_id에 넣을 DB 버전(API는 R2 객체 버전 또는 로컬 파일 sha256). 없으면 null.
    """
    res = request if isinstance(request, dict) else {}
    if not is_trend_result(res):
        return error_response(res, "invalid_input",
                              "trend 결과(TrendAnalysisResult) 형식이 아닙니다: applied_filters가 없습니다. "
                              f"trend 결과(request_id, status, applied_filters, trends)를 그대로 보내 주세요. "
                              f"(받은 필드: {', '.join(sorted(res)) or '(없음)'})")
    try:
        req, upstream, note = from_trend_result(res)
    except ValueError as e:
        return error_response(res, "invalid_input", str(e))
    if upstream == "no_results":
        return _record_input_provenance(no_results_response(req, note), res)
    if upstream == "error":
        return _record_input_provenance(error_response(req, "upstream_error", note), res)
    input_notes = req.pop("_input_notes", [])
    if not req.get("season"):
        result = error_response(req, "missing_fields",
                                "필수 입력이 없습니다: applied_filters.season. trend 결과의 applied_filters.season에 시즌이 있어야 합니다 "
                                "(쿼리 전처리에서 사용자가 말하지 않았으면 다음 패션 시즌, origin=default_next). "
                                f"받은 applied_filters: {res.get('applied_filters')!r}")
        return _record_input_provenance(result, res)
    result = run_internal(req, con, image_dir=image_dir, now=now, clip_filter=clip_filter, input_notes=input_notes,
                          manifest_id=manifest_id)
    return _record_input_provenance(result, res)


def run_internal(req: dict, con, image_dir: str | None = None, now: datetime | None = None,
                 clip_filter=None, input_notes: list[str] | None = None, manifest_id: str | None = None) -> dict:
    """내부 형식 요청 → 응답. 트렌드가 1개여도 같은 형식으로 돌려준다."""
    req = req if isinstance(req, dict) else {}
    input_notes = input_notes or []
    trends = req.get("trends")
    if not isinstance(trends, list) or not trends:
        msg = ("필수 입력이 없습니다: trends가 비어 있습니다. trend 결과의 trends 목록을 그대로 담아 주세요 "
               "(각 항목: {style: {id, name}, items: [{category_major, category_minor, colors}]}).")
        return error_response(req, "missing_fields", msg)
    if not all(isinstance(t, dict) for t in trends):
        return error_response(req, "invalid_input", "trends의 각 항목은 {style, items} 객체여야 합니다.")

    # 공통 값(request_id, season, 사용자 카테고리)은 트렌드마다 같으니 한 번만 검사
    err, _ = check_common(req)
    if err:
        return error_response(req, err["errors"][0]["code"], err["errors"][0]["message"])

    common = {k: v for k, v in req.items() if k != "trends"}
    products, trend_results, evidence, used = [], [], [], set()
    season_basis = None
    for i, t in enumerate(trends):
        one = {**common, "trend": t}
        out = run(one, con, image_dir=image_dir, now=now, clip_filter=clip_filter, manifest_id=manifest_id)
        tr = out["trend_result"]
        tr["product_index"] = i
        tr["limitations"] = [*tr["limitations"], *input_notes]
        tr["errors"] = [to_team(e) for e in tr["errors"]]
        products.append(out["product"])
        trend_results.append(tr)
        season_basis = season_basis or out["season_basis"]
        tid = tr["style"]["id"] or f"t{i + 1}"
        if tid in used:                         # 같은 style.id가 두 번 오면 근거 ID가 겹치지 않게
            tid = f"{tid}#{i + 1}"
        used.add(tid)
        ev = [{**e, "evidence_id": e["evidence_id"].replace(f"{NS}:", f"{NS}:{tid}:", 1), "style_id": tid}
              for e in out["evidence"]]
        tr["evidence_ids"] = [e["evidence_id"] for e in ev]
        evidence += ev

    statuses = [tr["status"] for tr in trend_results]
    if all(s == "ok" for s in statuses):
        status = "ok"
    elif any(p is not None for p in products):
        status = "partial"
    elif all(s == "error" for s in statuses):
        status = "error"
    else:
        status = "no_results"

    errors = []
    if status == "error":
        errs = [e for tr in trend_results for e in tr["errors"]]
        detail = "; ".join(f"{_label(tr, i)}: {tr['errors'][0]['message']}" for i, tr in enumerate(trend_results))
        code = pick(e["code"] for e in errs)
        reasons = {e.get("reason") for e in errs if e["code"] == code}
        errors.append({"code": code,
                       "message": f"모든 트렌드를 처리하지 못했습니다. {detail}",
                       "reason": reasons.pop() if len(reasons) == 1 else "mixed",
                       "style_ids": [tr["style"]["id"] for tr in trend_results]})

    resp = {"schema_version": SCHEMA_VERSION, "request_id": req.get("request_id"), "agent_id": AGENT_ID,
            "status": status, "summary": _summary(products, trend_results, season_basis),
            "products": products, "trend_results": trend_results, "season_basis": season_basis,
            "evidence": evidence, "errors": errors}
    problems = validate_response(resp)
    if problems:
        return error_response(req, "invalid_response", "응답 형식 검사 실패: " + "; ".join(problems))
    return resp


def _label(tr: dict, i: int) -> str:
    sid, name = tr["style"]["id"] or f"t{i + 1}", tr["style"]["name"]
    return f"{sid}({name})" if name else sid


def _summary(products, trend_results, season_basis) -> str:
    """트렌드별 1위를 한 문장으로. 숫자는 각 트렌드 결과에서만 가져온다."""
    picked, failed = [], []
    for i, (p, tr) in enumerate(zip(products, trend_results)):
        label = _label(tr, i)
        if p is not None:
            picked.append(f"{label} → {p['category']}({p['attributes'].get('color')}) 재고 "
                          f"{p['inventory']['quantity']:,}개")
        else:
            code = tr["errors"][0]["code"] if tr["errors"] else "INTERNAL_ERROR"
            why = ("조건에 맞는 재고 없음" if tr["status"] == "no_results" else
                   "입력 오류" if code == "INVALID_REQUEST" else f"처리 오류({code})")
            failed.append(f"{label} {why}")
    head = f"{season_basis['season']} " if season_basis else ""
    text = f"{head}트렌드 {len(trend_results)}개에 대해 재고 상품 {len(picked)}개를 골랐습니다(가상 재고)"
    text += (": " + ", ".join(picked)) if picked else ""
    return text + "." + (f" 고르지 못한 트렌드: {', '.join(failed)}." if failed else "")


def validate_response(resp: dict) -> list[str]:
    """최종 응답의 최소 형식 검사 (팀 JSON Schema가 확정되기 전까지)."""
    p = []
    for k, t in {"schema_version": str, "request_id": str, "agent_id": str, "status": str, "summary": str,
                 "products": list, "trend_results": list, "evidence": list, "errors": list}.items():
        if not isinstance(resp.get(k), t):
            p.append(f"{k} 형식 오류")
    if resp.get("status") not in STATUSES:
        p.append("status 값 오류")
    prods, trs = resp.get("products") or [], resp.get("trend_results") or []
    if len(prods) != len(trs):
        p.append("products와 trend_results 개수가 다름")
    for i, (prod, tr) in enumerate(zip(prods, trs)):
        p += check_trend_result(tr, f"trend_results[{i}]")
        if tr.get("product_index") != i:
            p.append(f"trend_results[{i}].product_index가 {i}가 아님")
        if not isinstance(tr.get("evidence_ids"), list):
            p.append(f"trend_results[{i}].evidence_ids 없음")
        if prod is not None:
            p += check_product(prod, f"products[{i}]")
        if (prod is None) != (tr.get("status") in ("no_results", "error")):
            p.append(f"products[{i}] 유무와 trend_results[{i}].status가 맞지 않음")
    ids = [e.get("evidence_id") for e in resp.get("evidence", [])]
    if len(ids) != len(set(ids)):
        p.append("evidence_id 중복")
    dangling = [x for tr in trs for x in tr.get("evidence_ids") or [] if x not in set(ids)]
    if dangling:
        p.append(f"evidence에 없는 evidence_ids: {', '.join(dangling)}")
    return p
