"""stock-agent (#F) — 과잉재고 에이전트 실행

구조화된 요청을 받아 분석 함수 6개를 정해진 순서로 실행하고,
팀 공통 envelope + InventoryProductResult 형식의 응답을 만든다.

summary는 Summarizer가 쓴다. 지금은 TemplateSummarizer(LLM 없음).
나중에 LLM을 쓸 때는 summarize(facts) -> str 를 가진 클래스를 만들어 run()에 넘기면 된다.
어떤 Summarizer든 summary 속 숫자는 facts에 있는 숫자여야 하며, 아니면 템플릿으로 되돌린다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol

import pandas as pd

from stock_agent.overstock import (
    COLOR_PAIRS,
    KF_COLORS,
    TOP_N,
    get_inventory_status,
    MIN_TREND_SOLD,
    get_matching_colors,
    peer_relative_trend,
    resolve_pair_areas,
    get_sales_trend,
    normalize_color,
    rank_overstock,
    summarize_attributes,
)
from stock_agent.images import DEFAULT_IMAGE_DIR, has_image, load_image
from stock_agent.season import DATA_END, SEASON_LABELS_KO, map_season
from stock_agent.synthetic import VERSION as STOCK_VERSION

SCHEMA_VERSION = "1.0"
AGENT_ID = "stock-agent"      # 2026-11-06 변경 (기존 기본값 own-product-inventory-agent)
NS = "stock-agent"            # evidence_id 앞부분: stock-agent:E-001
# 시즌은 02 쿼리 전처리가 정해서 준다(미입력이면 02가 SS/FW로 추정, source=inferred).
# 04는 계산하지 않고 받은 값과 출처를 그대로 쓴다.
# 트렌드 색(03, K-Fashion 21색으로 정제)은 있으면 반드시 쓴다(짝 카테고리 안에서 조합색 매치).
# 트렌드에 색이 없을 때만 비어서 오고, 그때는 색 조건 없이 찾는다.
# 카테고리(사용자·트렌드)와 색이 모두 없으면 error.
REQUIRED = ("request_id", "season")
SEASON_SOURCES = ("user", "inferred", "unknown")   # 팀 계약의 값 출처
OUTPUT_N = 1        # 2026-11-06 팀 합의: 04의 응답 상품은 1위 1개. attribute_summary는 상위 TOP_N개 기준
SAMPLE_WARNING = "재고·판매가는 가상 데이터(sample)입니다. 상대 비교로만 해석하세요."
LICENSE = "Kaggle H&M 대회 규정(비상업적 용도)"


# ---------------------------------------------------------------- summary

class Summarizer(Protocol):
    def summarize(self, facts: dict) -> str: ...


class TemplateSummarizer:
    """LLM 없이 정해진 문장 틀로 summary를 만든다."""

    def summarize(self, f: dict) -> str:
        areas = ", ".join(f["areas"])
        basis = f.get("area_basis", "all" if f.get("all_areas") else "user")
        if basis == "all":
            where = f"카테고리 미지정으로 전체 영역({areas})"
        elif basis == "trend":
            where = f"트렌드 카테고리({', '.join(f['given_areas'])})의 짝 카테고리({areas})"
        else:
            where = f"{f['target']} 기획용 짝 카테고리({areas})"
        if f["n_items"] == 0:
            return f"{f['season_label']} {where}에서 조건에 맞는 과잉재고를 찾지 못했습니다."
        color_part = (f"트렌드 색({', '.join(f['trend_colors'])})과 어울리는 색의" if f["trend_colors"]
                      else "트렌드 색이 없어 색 조건 없이")
        return (
            f"{f['season_label']} {where} 중 {color_part} 재고 후보 {f['n_candidates']:,}개를 비교해, "
            f"{f['top1_rank']}위 {f['top1_category']}({f['top1_color']}) 재고 {f['top1_stock_qty']:,}개(가상 재고)를 골랐습니다."
        )


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM.findall(text)}


def allowed_numbers(facts: dict) -> set[str]:
    out: set[str] = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(str(int(v)) if float(v).is_integer() else str(v))
        elif isinstance(v, str):
            out.update(numbers_in(v))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    walk(facts)
    return out


def safe_summary(summarizer: Summarizer, facts: dict) -> tuple[str, list[str]]:
    """summary 속 숫자를 facts와 대조. 맞지 않으면 템플릿으로 되돌리고 경고."""
    text = summarizer.summarize(facts)
    extra = numbers_in(text) - allowed_numbers(facts)
    if extra:
        return TemplateSummarizer().summarize(facts), [f"summary에 근거 없는 숫자 {sorted(extra)}가 있어 템플릿 문장으로 바꿨습니다."]
    return text, []


# ---------------------------------------------------------------- 시즌 입력

def read_season(value) -> tuple[str, str]:
    """요청의 season → (시즌 문자열, 출처).

    "2027-SS" 같은 문자열이면 출처 unknown,
    {"value": "2026 겨울", "source": "inferred"} 같은 객체면 그 출처를 그대로 쓴다.
    """
    if isinstance(value, dict):
        text = value.get("value")
        source = value.get("source") if value.get("source") in SEASON_SOURCES else "unknown"
        return text, source
    return value, "unknown"


# ---------------------------------------------------------------- 응답 조립

def _envelope(req: dict, status: str, summary: str, **extra) -> dict:
    base = {
        "schema_version": SCHEMA_VERSION,
        "request_id": req.get("request_id"),
        "agent_id": AGENT_ID,
        "status": status,
        "summary": summary,
        "products": [],
        "attribute_summary": None,
        "season_basis": None,
        "findings": [],
        "evidence": [],
        "limitations": [],
        "errors": [],
    }
    base.update(extra)
    return base


def _error(req: dict, code: str, message: str) -> dict:
    return _envelope(req, "error", message, errors=[{"code": code, "message": message}])


def _season_label(period) -> str:
    return f"{period.target_year} {SEASON_LABELS_KO[period.season]}"


def _season_code(period) -> str:
    return f"{period.target_year}{period.season}" if period.season in ("SS", "FW") else _season_label(period)


def _matched_trend_color(product_color: str, trend_colors: list[str]) -> str | None:
    for t in trend_colors:
        k = normalize_color(t)
        if k and product_color in COLOR_PAIRS[k]:
            return t
    return None


def _evidence(now: str, period, areas, colors, trend_period, with_images: bool = False) -> list[dict]:
    common = {"published_at": None, "observed_at": DATA_END.isoformat(), "fetched_at": now,
              "permission_status": LICENSE, "manifest_id": "TBD"}
    image_ev = [{"evidence_id": f"{NS}:E-005", "source_id": "hm_images", "source_type": "internal_product_image",
                 "source_name": "H&M 상품 이미지", "data_nature": "fact",
                 "locator": "images/<article_id 앞 3자리>/<article_id>.jpg", **common}] if with_images else []
    return [
        {"evidence_id": f"{NS}:E-001", "source_id": "stock_v1", "source_type": "synthetic_inventory",
         "source_name": f"가상 재고 ({STOCK_VERSION})", "data_nature": "sample",
         "locator": f"stock_v1 JOIN article_attrs WHERE kf_area IN {areas} AND kf_color IN {colors}",
         **common},
        {"evidence_id": f"{NS}:E-002", "source_id": "hm_transactions_train", "source_type": "internal_sales_db",
         "source_name": "H&M transactions (시즌 판매 비중)", "data_nature": "fact",
         "locator": f"transactions, 기간 {period.to_period()['ranges']}", **common},
        {"evidence_id": f"{NS}:E-003", "source_id": "hm_transactions_train", "source_type": "internal_sales_db",
         "source_name": "H&M transactions (최근 판매 추세, 같은 영역 후보 대비)", "data_nature": "fact",
         "locator": f"transactions, 최근 {trend_period['recent'][0]}~{trend_period['recent'][1]} "
                    f"vs 직전 {trend_period['prev'][0]}~{trend_period['prev'][1]}. "
                    f"최근+직전 {MIN_TREND_SOLD}개 미만은 중립", **common},
        {"evidence_id": f"{NS}:E-004", "source_id": "hm_articles", "source_type": "internal_product_db",
         "source_name": "H&M articles + K-Fashion 속성 보강", "data_nature": "fact",
         "locator": "article_attrs (stock_agent/attributes.py 규칙)", **common},
    ] + image_ev


def _num(v, cast=float):
    """NaN·None → None, 아니면 숫자."""
    return None if v is None or pd.isna(v) else cast(v)


def reason_text(rs: dict) -> str:
    """상품별 추천 이유 한 줄 (LLM 없음). 숫자는 모두 rs 안의 값."""
    p, t = rs["pairing"], rs["recent_trend"]
    if p["basis"] == "user":
        where = f"{p['target_category']} 기획용 짝 카테고리({p['product_area']})"
    elif p["basis"] == "trend":
        where = f"트렌드 카테고리({', '.join(p['trend_categories'])})의 짝 카테고리({p['product_area']})"
    else:
        where = f"전체 영역 검색({p['product_area']})"
    color = (f"{rs['color']['matched_trend_color']}와 어울리는 {rs['color']['product_color']}"
             if rs["color"]["matched_trend_color"] else rs["color"]["product_color"])
    parts = []
    if rs["stock_value_krw"] is not None:
        parts.append(f"재고 금액 {rs['stock_value_krw'] / 10_000:,.0f}만 원(가상)")
    if rs["sell_through"] is not None:
        parts.append(f"판매율 {rs['sell_through']:.0%}(가상)")
    if t["recent_sold"] is not None:           # None = 추세 계산 실패
        if t["recent_sold"] == 0 and t["prev_sold"] == 0:
            sold = f"최근 {t['weeks'] * 2}주 판매 없음"
        else:
            sold = f"최근 {t['weeks']}주 {t['recent_sold']}개 / 직전 {t['weeks']}주 {t['prev_sold']}개 판매"
        basis, v = t.get("basis"), t.get("value")
        if basis == "too_few_sales":
            sold += "(판매가 적어 추세는 순위에 반영 안 함)"
        elif basis == "peer_relative" and v is not None:
            sold += (", 같은 영역 평균보다 빠르게 감소" if v <= -0.1
                     else ", 같은 영역 평균보다 판매 흐름 좋음" if v >= 0.1
                     else ", 같은 영역 평균과 비슷한 흐름")
        parts.append(sold)
    if rs["season_share"] is None:
        parts.append("판매 이력 없음")
    else:
        parts.append(f"판매의 {rs['season_share']:.0%}가 {rs['season_name']} 기간")
    return f"{where}, {color}. " + ", ".join(parts) + "."


def _reason(r, am, target: str | None, matched: str | None, season_name: str, weeks: int) -> dict:
    g = lambda k: getattr(r, k, None)  # noqa: E731  (추세 실패 등으로 컬럼이 없을 수 있음)
    rs = {
        "pairing": {"basis": am.basis, "target_category": target,
                    "trend_categories": am.given if am.basis == "trend" else [],
                    "product_area": g("kf_area")},
        "color": {"product_color": r.kf_color, "matched_trend_color": matched, "rule": "조합표 v1"},
        "stock_value_krw": _num(g("stock_value_krw"), int),
        "sell_through": None if _num(g("sell_through")) is None else round(float(g("sell_through")), 3),
        "recent_trend": {"weeks": weeks, "recent_sold": _num(g("recent_sold"), int),
                         "prev_sold": _num(g("prev_sold"), int),
                         "basis": g("trend_basis"),       # peer_relative / too_few_sales
                         "value": None if _num(g("trend")) is None else round(float(g("trend")), 3),
                         "peer_value": None if _num(g("peer_trend")) is None else round(float(g("peer_trend")), 3),
                         "raw_value": None if _num(g("trend_raw")) is None else round(float(g("trend_raw")), 3)},
        "season_share": None if _num(g("season_share")) is None else round(float(g("season_share")), 3),
        "season_name": season_name,
        "score": None if _num(g("score")) is None else round(float(g("score")), 3),
        "data_nature": {"stock_value_krw": "sample", "sell_through": "sample",
                        "recent_trend": "fact", "season_share": "fact"},
    }
    rs["text"] = reason_text(rs)
    return rs


def build_products(top: pd.DataFrame, period, trend_colors: list[str], target: str | None = None,
                   weeks: int = 4, area_match=None, images: dict | None = None) -> list[dict]:
    am = area_match or resolve_pair_areas(target, None)
    products = []
    season_name = SEASON_LABELS_KO[period.season]
    for r in top.itertuples(index=False):
        matched = _matched_trend_color(r.kf_color, trend_colors)
        products.append({
            "style_id": r.article_id[:7],
            "sku_ids": [r.article_id],
            "category": r.kf_category,
            "season": _season_code(period),
            "attributes": {
                "color": r.kf_color,
                "material": list(r.materials) if r.materials is not None else [],
                "fit": r.fit if isinstance(r.fit, str) else None,
            },
            "inventory": {
                "quantity": int(r.stock_qty),
                "unit": "pieces",
                "as_of": DATA_END.isoformat(),
                "definition": STOCK_VERSION,
                "data_nature": "sample",
            },
            "rank": int(r.rank),
            "matched_trend_color": matched,
            "reason": _reason(r, am, target, matched, season_name, weeks),
            "image": (images or {}).get(r.article_id),
            "evidence_ids": [f"{NS}:E-001", f"{NS}:E-002", f"{NS}:E-003", f"{NS}:E-004"]
                            + ([f"{NS}:E-005"] if (images or {}).get(r.article_id) else []),
        })
    return products


# ---------------------------------------------------------------- 실행

# 시즌이 없을 때 원인을 단정하지 않고 확인할 곳을 알려 준다
# (02가 안 만듦 / 요청을 만드는 쪽이 빠뜨림 / 필드 이름·위치가 달라 04가 못 알아봄)
SEASON_HINT = ("시즌은 02 쿼리 전처리가 정해 요청 최상위의 season 필드로 담아야 합니다. "
               "02 결과에 시즌이 있는지, 요청을 만드는 쪽에서 옮겨 담았는지, 필드 이름이 season인지 확인하세요.")


def _missing_detail(req: dict, fields: list[str]) -> str:
    """빠진 필드가 아예 없는지, 값만 비었는지, 그리고 04가 실제로 받은 필드 이름."""
    absent = [k for k in fields if k not in req]
    empty = [k for k in fields if k in req and not req.get(k)]
    parts = []
    if absent:
        parts.append(f"요청에 없는 필드: {', '.join(absent)}")
    if empty:
        parts.append(f"값이 비어 있는 필드: {', '.join(f'{k}={req.get(k)!r}' for k in empty)}")
    parts.append(f"받은 필드: {', '.join(sorted(req)) or '(없음)'}")
    return "(" + "; ".join(parts) + ")."


def _pick_with_images(ranked: pd.DataFrame, n: int, image_dir, limitations: list[str]):
    """순위 위에서부터 H&M 이미지가 있는 상품 n개를 고른다.

    1위에 이미지가 없으면 이미지가 있는 다음 순위 상품을 고르고(rank는 실제 순위 그대로) limitations에 적는다.
    후보 전체에 이미지가 하나도 없으면 1위를 이미지 없이(image: null) 돌려준다.
    """
    if ranked.empty:
        return ranked.head(0), {}
    from pathlib import Path
    if not Path(image_dir).is_dir():
        limitations.append(f"이미지 폴더({image_dir})가 없어 상품 사진 없이 순위 1위를 반환합니다.")
        return ranked.head(n).reset_index(drop=True), {}
    picked = []
    for i, aid in enumerate(ranked["article_id"]):
        if has_image(image_dir, aid):
            picked.append(i)
            if len(picked) == n:
                break
    if not picked:
        limitations.append("후보 상품 중 H&M 이미지가 있는 상품이 없어 순위 1위를 사진 없이 반환합니다.")
        return ranked.head(n).reset_index(drop=True), {}
    if picked[0] > 0:
        limitations.append(f"순위 1~{picked[0]}위 상품은 H&M 이미지가 없어, 이미지가 있는 최상위 "
                           f"{picked[0] + 1}위 상품을 골랐습니다.")
    top = ranked.iloc[picked].reset_index(drop=True)
    return top, {aid: load_image(image_dir, aid) for aid in top["article_id"]}


def run(request: dict, con, summarizer: Summarizer | None = None, n: int = OUTPUT_N,
        now: datetime | None = None, image_dir: str | None = None) -> dict:
    """요청 1건 → 응답 dict. con은 read_only DuckDB 연결.

    - season 필수(02 쿼리 전처리가 정해서 줌). 없으면 error. 04는 시즌을 계산하지 않는다
    - 짝 카테고리: target_category(02) → 없으면 trend_categories(03) 짝 교집합 → 둘 다 없으면 전체 영역
    - trend_colors(03, 21색): 있으면 짝 카테고리 안에서 조합색으로 매치. 없으면 색 조건 없이
    - 카테고리·색 모두 없음 → error (카테고리 없이 색만 있으면 전체 영역에서 조합색 매치)
    - 값이 있는데 잘못됐으면(예: '신발', '2027') error
    - 응답 상품은 H&M 이미지가 있는 최상위 1개. 사진은 products[].image에 base64로 넣는다
    """
    now = now or datetime.now(timezone.utc)
    summarizer = summarizer or TemplateSummarizer()
    req = request or {}

    # 입력 검증: 빠진 값은 추정하지 않는다
    as_list = lambda v: [v] if isinstance(v, str) else list(v or [])  # noqa: E731
    trend_colors = as_list(req.get("trend_colors"))
    trend_categories = as_list(req.get("trend_categories"))
    target = req.get("target_category")

    missing = [k for k in REQUIRED if not req.get(k)]
    if missing:
        msg = f"필수 입력이 없습니다: {', '.join(missing)}. {_missing_detail(req, missing)}"
        if "season" in missing:
            msg += " " + SEASON_HINT
        return _error(req, "missing_fields", msg)
    if not trend_colors and not target and not trend_categories:
        return _error(req, "missing_fields",
                      "필수 입력이 없습니다: 카테고리와 trend_colors가 모두 없습니다 "
                      "(사용자 카테고리·트렌드 카테고리·트렌드 색 중 하나는 있어야 합니다). "
                      f"{_missing_detail(req, ['target_category', 'trend_categories', 'trend_colors'])} "
                      "요청을 만드는 쪽에서 02·03 결과가 담겼는지, 필드 이름이 맞는지 확인하세요.")
    if req.get("schema_version") not in (None, SCHEMA_VERSION):
        return _error(req, "schema_version_mismatch", f"지원하지 않는 schema_version: {req.get('schema_version')}")

    season_text, season_source = read_season(req["season"])
    if not season_text:
        return _error(req, "missing_fields",
                      f"필수 입력이 없습니다: season. season은 있지만 value가 비어 있습니다(받은 값: {req['season']!r}). "
                      + SEASON_HINT)
    try:
        period = map_season(season_text)
        am = resolve_pair_areas(target, trend_categories)
    except ValueError as e:
        return _error(req, "invalid_input", str(e))
    areas = am.areas

    limitations = [SAMPLE_WARNING, *period.warnings]
    if season_source == "inferred":
        limitations.append(f"시즌 {_season_label(period)}은 사용자 입력이 아니라 쿼리 전처리 단계가 추정한 값입니다.")
    if am.basis == "all":
        limitations.append("카테고리 입력이 없어 상의·하의·아우터·원피스 전체에서 찾았습니다.")
    elif am.basis == "trend":
        limitations.append(f"사용자 카테고리가 없어 트렌드 카테고리({', '.join(am.given)})의 짝으로 찾았습니다.")
        if am.fallback:
            limitations.append(
                f"트렌드 카테고리 모두와 어울리는 공통 짝이 없어, 트렌드 카테고리와 그 짝 전체({', '.join(areas)})에서 "
                f"순위로 골랐습니다."
            )

    if trend_colors:
        cm = get_matching_colors(trend_colors)
        colors, unknown = cm.colors, cm.unknown
        if cm.fallback:
            limitations.append(
                f"트렌드 색 {cm.threshold}개 이상과 함께 어울리는 색이 없어, 하나라도 어울리는 색으로 넓혀 찾았습니다."
            )
        if unknown:
            limitations.append(f"K-Fashion 색으로 바꿀 수 없는 트렌드 색은 제외했습니다: {', '.join(unknown)}")
        if not colors:
            return _error(req, "invalid_input", f"알 수 있는 트렌드 색이 없습니다: {', '.join(trend_colors)}")
    else:
        colors = list(KF_COLORS)
        limitations.append("트렌드 색이 없어 색 조건 없이 21색 전체에서 찾았습니다.")

    segment = req.get("segment")
    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds") if now.tzinfo else now.isoformat(timespec="seconds")
    errors: list[dict] = []

    cand = get_inventory_status(con, areas, colors, period, segment=segment)
    trend_period = {"recent": ("", ""), "prev": ("", "")}
    if not cand.empty:
        try:
            trend = get_sales_trend(con, cand["article_id"].tolist())
            trend_period = {k: tuple(d.isoformat() for d in v) for k, v in trend.attrs["period"].items()}
            cand = cand.merge(trend[["article_id", "recent_sold", "prev_sold"]], on="article_id", how="left")
            cand = peer_relative_trend(cand)     # 또래(같은 영역 후보) 대비 추세, 판매 적으면 중립
        except Exception as e:  # 추세만 실패하면 나머지 지표로 순위
            errors.append({"code": "trend_failed", "message": f"최근 추세 계산 실패: {e}"})
            cand["trend"] = 0.0
            limitations.append("최근 판매 추세를 계산하지 못해 순위에서 추세를 중립값으로 두었습니다.")

    image_dir = image_dir or DEFAULT_IMAGE_DIR
    ranked = rank_overstock(cand, n=len(cand))           # 후보 전체 순위
    shortlist = ranked.head(max(n, TOP_N))               # 상위 50개: 속성 요약용
    top, images = _pick_with_images(ranked, n, image_dir, limitations)   # 응답 상품: 이미지 있는 최상위 1개
    summary_attr = {"scope": f"상위 {len(shortlist)}개 후보", **summarize_attributes(shortlist)}
    facts = {
        "season_label": _season_label(period),
        "target": target or "카테고리 미지정",
        "all_areas": am.basis == "all",
        "area_basis": am.basis,
        "given_areas": am.given,
        "areas": areas,
        "trend_colors": trend_colors,
        "n_candidates": int(len(cand)),
        "n_items": int(len(top)),
        "n_shortlist": summary_attr["n_items"],
        "total_stock_qty": summary_attr["total_stock_qty"],
        "top_colors": list(summary_attr["color"])[:3],
        "top1_rank": int(top.iloc[0]["rank"]) if len(top) else 1,
        "top1_category": top.iloc[0]["kf_category"] if len(top) else None,
        "top1_color": top.iloc[0]["kf_color"] if len(top) else None,
        "top1_stock_qty": int(top.iloc[0]["stock_qty"]) if len(top) else None,
    }
    summary, warn = safe_summary(summarizer, facts)
    limitations += warn

    status = "no_results" if top.empty else ("partial" if errors else "ok")
    season_basis = {
        "season": _season_label(period),
        "source": season_source,              # 02가 준 출처: user / inferred / unknown
        **period.to_period(),
        "n_periods": period.n_periods,
        "basis": period.basis,
    }

    resp = _envelope(
        req, status, summary,
        products=build_products(top, period, trend_colors, target, area_match=am, images=images),
        attribute_summary=summary_attr if len(top) else None,
        season_basis=season_basis,
        findings=[] if top.empty else [{
            "type": "attribute_summary",
            "statement": f"상위 {summary_attr['n_items']}개 중 많은 색: {', '.join(facts['top_colors'])}",
            "kind": "observation",
            "evidence_ids": [f"{NS}:E-001", f"{NS}:E-004"],
        }],
        evidence=_evidence(fetched_at, period, areas, colors, trend_period, with_images=any(images.values())),
        limitations=limitations,
        errors=errors,
    )
    problems = validate_response(resp)
    if problems:
        return _error(req, "invalid_response", "응답 형식 검사 실패: " + "; ".join(problems))
    return resp


# ---------------------------------------------------------------- 형식 검사

_TOP_KEYS = {"schema_version": str, "request_id": str, "agent_id": str, "status": str, "summary": str,
             "products": list, "findings": list, "evidence": list, "limitations": list, "errors": list}
_PRODUCT_KEYS = ("style_id", "sku_ids", "category", "season", "attributes", "inventory", "rank",
                 "matched_trend_color", "reason", "image", "evidence_ids")


def validate_response(resp: dict) -> list[str]:
    """팀 JSON Schema가 확정되기 전까지 쓰는 최소 검사."""
    p = []
    for k, t in _TOP_KEYS.items():
        if not isinstance(resp.get(k), t):
            p.append(f"{k} 형식 오류")
    if resp.get("status") not in ("ok", "partial", "no_results", "error"):
        p.append("status 값 오류")
    ev_ids = {e.get("evidence_id") for e in resp.get("evidence", [])}
    for i, prod in enumerate(resp.get("products", [])):
        for k in _PRODUCT_KEYS:
            if k not in prod:
                p.append(f"products[{i}].{k} 없음")
        if prod.get("inventory", {}).get("quantity", -1) < 0:
            p.append(f"products[{i}] 재고 음수")
        for e in prod.get("evidence_ids", []):
            if e not in ev_ids:
                p.append(f"products[{i}] evidence {e} 없음")
    return p
