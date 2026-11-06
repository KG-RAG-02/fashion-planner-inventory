"""트렌드 여러 개 → 트렌드마다 재고 1위 1개 (총 n개)

03 트렌드 분석은 한 시즌에 트렌드를 1개 이상 넘긴다. 트렌드마다 agent.run()을 한 번씩 돌리고,
결과를 팀 공통 envelope 하나로 합친다. 한 트렌드를 처리하는 규칙은 agent.run() 그대로다.

요청 (시즌·사용자 카테고리·고객군은 02가 주는 공통 값, 트렌드별 색·카테고리는 03이 줌)
    {
      "request_id": "req_123", "season": "2027-SS", "target_category": "상의", "segment": "Ladieswear",
      "trends": [
        {"trend_id": "t1", "name": "정제된 맥시멀리스트", "trend_colors": [], "trend_categories": ["재킷", "셔츠"]},
        {"trend_id": "t2", "name": "발레코어", "trend_colors": ["라이트핑크", "화이트"], "trend_categories": ["상의"]}
      ]
    }

응답
    products        트렌드마다 1개 (상품에 trend_id, trend_name). 같은 상품이 여러 트렌드에 나올 수 있음
    trend_results   트렌드별 status, summary, limitations, attribute_summary, errors
    evidence        트렌드별 근거. evidence_id = stock-agent:<trend_id>:E-001 (트렌드마다 조회 조건이 달라서)
    limitations     모든 트렌드에 공통인 것만. 트렌드별 경고는 trend_results에
    status          모두 ok → ok / 일부 실패·결과 없음 → partial / 모두 결과 없음 → no_results / 모두 오류 → error

요청에 trends가 없으면 run_request()는 agent.run()을 그대로 부른다(트렌드 1개 형식).
"""

from __future__ import annotations

from datetime import datetime

from stock_agent.agent import NS, REQUIRED, SEASON_HINT, _envelope, _error, _missing_detail, run, validate_response

TREND_FIELDS = ("trend_colors", "trend_categories")


def run_request(request: dict, con, image_dir: str | None = None, now: datetime | None = None) -> dict:
    """API·스크립트의 입구. trends가 있으면 트렌드별로, 없으면 트렌드 1개 형식으로."""
    req = request or {}
    if "trends" in req:
        return run_multi(req, con, image_dir=image_dir, now=now)
    return run(req, con, image_dir=image_dir, now=now)


def _trend_id(t: dict, i: int) -> str:
    return str(t.get("trend_id") or f"t{i + 1}")


def _rename_evidence(resp: dict, tid: str) -> dict:
    """E-001 → <tid>:E-001. 트렌드마다 조회 조건(색·영역)이 달라 근거를 따로 둔다."""
    old = f"{NS}:"
    new = f"{NS}:{tid}:"
    fix = lambda e: e.replace(old, new, 1) if e.startswith(old) else e  # noqa: E731
    for ev in resp.get("evidence", []):
        ev["evidence_id"] = fix(ev["evidence_id"])
        ev["trend_id"] = tid
    for p in resp.get("products", []):
        p["evidence_ids"] = [fix(e) for e in p.get("evidence_ids", [])]
    for f in resp.get("findings", []):
        f["evidence_ids"] = [fix(e) for e in f.get("evidence_ids", [])]
        f["trend_id"] = tid
    return resp


def run_multi(request: dict, con, image_dir: str | None = None, now: datetime | None = None) -> dict:
    req = request or {}
    trends = req.get("trends")
    if not isinstance(trends, list) or not trends:
        return _error(req, "missing_fields",
                      "필수 입력이 없습니다: trends가 비어 있습니다. 트렌드 분석(03) 결과의 트렌드 목록을 "
                      "trends 필드에 담아 주세요 (각 항목: trend_id, name, trend_colors, trend_categories).")
    if not all(isinstance(t, dict) for t in trends):
        return _error(req, "invalid_input", "trends의 각 항목은 {trend_id, name, trend_colors, trend_categories} 객체여야 합니다.")

    # 공통 필수 값(request_id, season)은 트렌드마다 같으니 한 번만 검사
    missing = [k for k in REQUIRED if not req.get(k)]
    if missing:
        msg = f"필수 입력이 없습니다: {', '.join(missing)}. {_missing_detail(req, missing)}"
        return _error(req, "missing_fields", msg + (" " + SEASON_HINT if "season" in missing else ""))

    # 위에 trend_colors·trend_categories가 있어도 쓰지 않음: 트렌드별 값만 씀
    common = {k: v for k, v in req.items() if k != "trends" and k not in TREND_FIELDS}
    results = []
    for i, t in enumerate(trends):
        tid, name = _trend_id(t, i), t.get("name")
        sub = {**common, **{k: t[k] for k in TREND_FIELDS if k in t}}
        resp = _rename_evidence(run(sub, con, image_dir=image_dir, now=now), tid)
        for p in resp["products"]:
            p["trend_id"], p["trend_name"] = tid, name
        results.append((tid, name, resp))

    statuses = [r["status"] for _, _, r in results]
    if all(s == "ok" for s in statuses):
        status = "ok"
    elif any(r["products"] for _, _, r in results):
        status = "partial"                     # 일부 트렌드만 성공
    elif all(s == "error" for s in statuses):
        status = "error"                       # 모두 오류 (예: 공통 입력인 시즌이 없음)
    else:
        status = "no_results"                  # 상품 0개, 오류만은 아님

    # 모든 트렌드에 공통인 경고만 위로 (가상 데이터, 시즌 대체 등)
    lim_sets = [r["limitations"] for _, _, r in results if r["status"] != "error"]
    common_lims = [x for x in lim_sets[0] if all(x in s for s in lim_sets[1:])] if lim_sets else []

    # 같은 오류가 여러 트렌드에서 나면(예: 시즌 없음) 한 번만, 어느 트렌드인지는 trend_ids로
    merged: dict[tuple, dict] = {}
    for tid, _, r in results:
        for e in r["errors"]:
            key = (e["code"], e["message"])
            merged.setdefault(key, {**e, "trend_ids": []})["trend_ids"].append(tid)
    errors = list(merged.values())

    first_ok = next((r for _, _, r in results if r.get("season_basis")), None)
    resp = _envelope(
        req, status, _summary(results),
        products=[p for _, _, r in results for p in r["products"]],
        season_basis=first_ok["season_basis"] if first_ok else None,
        findings=[f for _, _, r in results for f in r["findings"]],
        evidence=[e for _, _, r in results for e in r["evidence"]],
        limitations=common_lims,
        errors=errors,
    )
    resp["trend_results"] = [{
        "trend_id": tid, "trend_name": name, "status": r["status"], "summary": r["summary"],
        "n_products": len(r["products"]),
        "attribute_summary": r.get("attribute_summary"),
        "limitations": [x for x in r["limitations"] if x not in common_lims],
        "errors": r["errors"],
    } for tid, name, r in results]

    problems = validate_response(resp)
    if problems:
        return _error(req, "invalid_response", "응답 형식 검사 실패: " + "; ".join(problems))
    return resp


def _summary(results) -> str:
    """트렌드별 1위를 한 문장으로. 숫자는 각 트렌드 응답에서만 가져온다."""
    picked, failed = [], []
    for tid, name, r in results:
        label = f"{tid}({name})" if name else tid
        if r["products"]:
            p = r["products"][0]
            picked.append(f"{label} → {p['category']}({p['attributes'].get('color')}) 재고 "
                          f"{p['inventory']['quantity']:,}개")
        else:
            why = "조건에 맞는 재고 없음" if r["status"] == "no_results" else "입력 오류"
            failed.append(f"{label} {why}")
    season = next((r["season_basis"]["season"] for _, _, r in results if r.get("season_basis")), "")
    head = f"{season} " if season else ""
    text = f"{head}트렌드 {len(results)}개에 대해 재고 상품 {len(picked)}개를 골랐습니다(가상 재고)"
    text += (": " + ", ".join(picked)) if picked else ""
    text += "." + (f" 고르지 못한 트렌드: {', '.join(failed)}." if failed else "")
    return text
