"""stock-agent (#F) — 트렌드 1개 → 과잉재고 상품 1개 (규칙 기반)

트렌드 1개(02 결과의 trends[] 한 항목)와 02의 사용자 조건(시즌·카테고리·색·성별)을 받아
분석 함수를 정해진 순서로 실행하고, 상품 1개(product)와 그 트렌드의 결과(trend_result)를 돌려준다.
트렌드가 여러 개면 multi.py가 이 run()을 트렌드 수만큼 돌려 이어 붙인다.

처리 순서 (run_trend)
    1. 짝 영역: 사용자 카테고리의 짝(여러 개면 교집합, 없으면 합집합) → 없으면 트렌드 카테고리의 짝 → 없으면 전체 영역.
       제외 카테고리는 영역·세부 카테고리 단위로 뺀다
    2. 색: 기준 색(사용자 색 또는 트렌드 색)과 어울리는 색 중 가장 많이 겹치는 색 + 기준 색 자체(톤온톤)
    3. 재고 조회 (짝 영역 · 색 · 시즌 · 고객군 · 재고 > 0 · 제외 카테고리)
       후보가 0개면 트렌드 색 조건만 완화해 다시 조회: 하나라도 어울리는 색 → 트렌드 색 조건 풀기(21색 전체).
       사용자 색·사용자 카테고리는 완화하지 않는다
    4. 최근 판매 추세 (같은 영역 후보 대비)
    5. 기준 이미지(무신사 스냅 착샷, 없으면 무신사 상품 이미지)가 있으면 소재·질감·디테일 CLIP 유사도 필터
       (흑백 이미지 + 소재·디테일 텍스트, 색 제외. snap.py, clip_matcher.py)
    6. 과잉재고 순위 → H&M 사진이 있는 최상위 1개
    7. 추천 이유(reason), 템플릿 summary, 근거(evidence)

요청
    {"request_id": "req_123", "season": {"year": 2027, "season": "SS", "origin": "user_requested"},
     "target_category": "상의", "exclude_categories": ["스커트"], "segment": "Ladieswear",
     "trend": {"style": {"id": "style_007", "name": "정제된 맥시멀리스트"},
               "items": [{"category_major": "상의", "category_minor": "셔츠", "colors": ["화이트"]}, ...]}}

응답
    product        상품 정보만 (style_id, sku_ids, category, season, attributes, inventory, image). 못 고르면 null
    trend_result   style, status, product_index, rank, reason, summary, search, limitations, errors
    season_basis   대신 쓴 과거 시즌 기간과 시즌 출처
    evidence       E-001~E-006 데이터 출처
    errors         공통 입력 오류만 (트렌드별 오류는 trend_result.errors)

summary는 Summarizer가 쓴다. 기본은 TemplateSummarizer(정해진 문장 틀).
어떤 Summarizer든 summary 속 숫자는 facts에 있는 숫자여야 하며, 아니면 템플릿으로 되돌린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pandas as pd

from stock_agent.attributes import to_vocab
from stock_agent.images import DEFAULT_IMAGE_DIR, has_image, load_image, take_notes
from stock_agent.overstock import (
    COLOR_PAIRS,
    KF_COLORS,
    MIN_TREND_SOLD,
    get_inventory_status,
    areas_of,
    any_matching_colors,
    get_matching_colors,
    get_sales_trend,
    normalize_color,
    peer_relative_trend,
    rank_overstock,
    resolve_pair_areas,
    split_exclude,
)
from stock_agent.season import DATA_END, SEASON_LABELS_KO, map_season
from stock_agent.snap import pick_reference
from stock_agent.snap import reference as snap_reference
from stock_agent.synthetic import VERSION as STOCK_VERSION
from stock_agent.trend_input import USER_COLOR_BASES, TrendInput, read_trend

SCHEMA_VERSION = "1.0"
AGENT_ID = "stock-agent"      # 2026-11-06 변경 (기존 기본값 own-product-inventory-agent)
NS = "stock-agent"            # evidence_id 앞부분: stock-agent:E-001
# 시즌은 02 쿼리 전처리가 정해서 준다(미입력이면 02가 다음 시즌으로 정함, origin=default_next).
# 03은 계산하지 않고 받은 값과 출처를 그대로 쓴다.
REQUIRED = ("request_id", "season")
SEASON_SOURCES = ("user", "inferred", "unknown")   # 응답 season_basis.source 값
SEASON_ORIGINS = {"user_requested": "user", "default_next": "inferred"}   # 02 SeasonRequest.origin → source
SAMPLE_WARNING = "재고·판매가는 가상 데이터(sample)입니다. 상대 비교로만 해석하세요."
LICENSE = "Kaggle H&M 대회 규정(비상업적 용도)"
COLOR_RULE = "조합표 v1, 트렌드 색과 어울리는 색 중 가장 많이 겹치는 색 + 트렌드 색 자체(톤온톤)"
USER_COLOR_RULE = "조합표 v1, 사용자 색과 어울리는 색 중 가장 많이 겹치는 색 + 사용자 색 자체(톤온톤)"


# ---------------------------------------------------------------- summary

class Summarizer(Protocol):
    def summarize(self, facts: dict) -> str: ...


class TemplateSummarizer:
    """정해진 문장 틀로 summary를 만든다."""

    def summarize(self, f: dict) -> str:
        areas = ", ".join(f["areas"])
        basis = f.get("area_basis", "user")
        if basis == "all":
            where = f"카테고리 미지정으로 전체 영역({areas})"
        elif basis == "trend":
            where = f"트렌드 카테고리({', '.join(f['given_areas'])})의 짝 카테고리({areas})"
        else:
            where = f"{f['target']} 기획용 짝 카테고리({areas})"
        if f["n_items"] == 0:
            return f"{f['season_label']} {where}에서 조건에 맞는 과잉재고를 찾지 못했습니다."
        label = f.get("color_label", "트렌드 색")
        rule = ("하나라도 어울리는 색·같은 색" if f.get("color_mode") == "any"
                else "어울리는 색 중 가장 많이 겹치는 색·같은 색")
        color_part = (f"{label}({', '.join(f['trend_colors'])})과 {rule}의" if f["trend_colors"]
                      else "트렌드 색 조건을 풀어 색 조건 없이" if f.get("colors_relaxed")
                      else "트렌드 색이 없어 색 조건 없이")
        text = (f"{f['season_label']} {where} 중 {color_part} 재고 후보 {f['n_candidates']:,}개를 비교해, "
                f"{f['top1_rank']}위 {f['top1_category']}({f['top1_color']}) 재고 {f['top1_stock_qty']:,}개"
                f"(가상 재고)를 골랐습니다.")
        return text + (f" {f['reason_detail']}" if f.get("reason_detail") else "")


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


# ---------------------------------------------------------------- 공통 입력 (02)

def read_season(value) -> tuple[str | None, str]:
    """요청의 season → (시즌 문자열, 출처 user/inferred/unknown).

    - 02 SeasonRequest: {"year": 2027, "season": "SS", "origin": "user_requested"|"default_next"}
      → ("2027-SS", user|inferred)
    - {"value": "2026 겨울", "source": "inferred"} → 그 출처 그대로
    - "2027-SS" 같은 문자열 → 출처 unknown
    """
    if isinstance(value, dict):
        if "year" in value or "season" in value:
            y, s = value.get("year"), value.get("season")
            return (f"{y}-{s}" if y and s else None), SEASON_ORIGINS.get(value.get("origin"), "unknown")
        source = value.get("source") if value.get("source") in SEASON_SOURCES else "unknown"
        return value.get("value"), source
    return value, "unknown"


# 시즌이 없을 때 원인을 단정하지 않고 확인할 곳을 알려 준다
# (02가 안 만듦 / 필드 이름·위치가 달라 03이 못 알아봄)
SEASON_HINT = ("시즌은 02 쿼리 전처리가 정해 applied_filters.season에 담고, 입구가 내부 형식의 season으로 옮깁니다. "
               "02 결과에 시즌이 있는지, 내부 형식이면 필드 이름이 season인지 확인하세요.")


def _missing_detail(req: dict, fields: list[str]) -> str:
    """빠진 필드가 아예 없는지, 값만 비었는지, 그리고 03이 실제로 받은 필드 이름."""
    absent = [k for k in fields if k not in req]
    empty = [k for k in fields if k in req and not req.get(k)]
    parts = []
    if absent:
        parts.append(f"요청에 없는 필드: {', '.join(absent)}")
    if empty:
        parts.append(f"값이 비어 있는 필드: {', '.join(f'{k}={req.get(k)!r}' for k in empty)}")
    parts.append(f"받은 필드: {', '.join(sorted(req)) or '(없음)'}")
    return "(" + "; ".join(parts) + ")."


# 고객군 (H&M index_group_name). 성별 → 고객군 (2026-11-08 결정)
SEGMENTS = ("Ladieswear", "Menswear", "Divided", "Baby/Children", "Sport")
GENDER_SEGMENTS = {"여": ("Ladieswear", "Divided"), "남": ("Menswear", "Divided"), "공용": ()}   # () = 전체
SEGMENT_BASES = ("segment", "gender", "item_gender", "all")


@dataclass(frozen=True)
class Common:
    """모든 트렌드에 같은 값 (요청). 고객군은 trend_common()이 트렌드마다 정한다."""
    period: object
    season_source: str
    targets: tuple[str, ...]              # 02 target_category (1개 또는 여러 개, 받은 순서)
    target_areas: tuple[str, ...]         # 그 영역 (상의·하의·아우터·원피스)
    segments: tuple[str, ...] = ()        # 찾을 고객군. () = 전체
    user_colors: tuple[str, ...] = ()     # 02 target_colors (21색 이름)
    exclude_areas: tuple[str, ...] = ()        # 02 exclude_categories 중 영역
    exclude_categories: tuple[str, ...] = ()   # 02 exclude_categories 중 세부 카테고리
    gender: str | None = None             # 요청 성별 (남/여/공용)
    segment_basis: str = "all"            # SEGMENT_BASES: 고객군을 무엇으로 정했나
    item_genders: tuple[str, ...] = ()    # segment_basis=item_gender일 때 쓴 Item 성별

    @property
    def target(self) -> str | None:
        """문장용 사용자 카테고리 (여러 개면 ·로 이음)."""
        return "·".join(self.targets) or None

    @property
    def excluded(self) -> list[str]:
        return [*self.exclude_areas, *self.exclude_categories]


def read_categories(value) -> list[str]:
    """02 target_category / exclude_categories → 목록. 문자열 하나도 받는다. 빈 값·중복은 뺀다."""
    if value is None:
        return []
    vals = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else [value]
    out = []
    for v in vals:
        v = v.strip() if isinstance(v, str) else v
        if v not in (None, "") and v not in out:
            out.append(v)
    return out


def read_user_colors(value) -> tuple[list[str], list[str]]:
    """02 target_colors → (사용자 색, 21색이 아닌 값). 문자열 하나도 받는다."""
    if value is None:
        return [], []
    vals = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else [value]
    colors, bad = [], []
    for v in vals:
        if isinstance(v, str) and v.strip() in KF_COLORS:
            if v.strip() not in colors:
                colors.append(v.strip())
        elif v not in (None, ""):
            bad.append(v)
    return colors, bad


def check_common(req: dict) -> tuple[dict | None, Common | None]:
    """공통 입력 검사. (오류 응답, None) 또는 (None, Common). multi.py도 트렌드를 돌리기 전에 한 번 부른다."""
    missing = [k for k in REQUIRED if not req.get(k)]
    if missing:
        msg = f"필수 입력이 없습니다: {', '.join(missing)}. {_missing_detail(req, missing)}"
        if "season" in missing:
            msg += " " + SEASON_HINT
        return error_output(req, "missing_fields", msg), None
    if req.get("schema_version") not in (None, SCHEMA_VERSION):
        return error_output(req, "schema_version_mismatch",
                            f"지원하지 않는 schema_version: {req.get('schema_version')}"), None
    season_text, source = read_season(req["season"])
    if not season_text:
        return error_output(req, "missing_fields",
                            f"필수 입력이 없습니다: season. season은 있지만 시즌 값(year·season 또는 value)이 비어 있습니다"
                            f"(받은 값: {req['season']!r}). " + SEASON_HINT), None
    targets = read_categories(req.get("target_category"))
    exclude = read_categories(req.get("exclude_categories"))
    user_colors, bad = read_user_colors(req.get("target_colors"))
    if bad:
        return error_output(req, "invalid_input",
                            f"target_colors에 21색이 아닌 값이 있습니다: {', '.join(map(repr, bad))}. "
                            f"02에서 21색 이름으로 맞춰 보내 주세요 ({', '.join(KF_COLORS)})."), None
    if any(not isinstance(v, str) for v in targets + exclude):
        return error_output(req, "invalid_input", "target_category·exclude_categories는 카테고리 이름(문자열)이어야 합니다."), None
    both = [v for v in targets if v in exclude]
    if both:
        return error_output(req, "invalid_input",
                            f"같은 카테고리가 target_category와 exclude_categories에 모두 있습니다: {', '.join(both)}"), None
    try:
        period = map_season(str(season_text))
        target_areas = areas_of(targets)
        ex_areas, ex_cats = split_exclude(exclude)
        segments, gender, basis = read_segments(req.get("segment"), req.get("gender"))
    except ValueError as e:
        return error_output(req, "invalid_input", str(e)), None
    return None, Common(period, source, tuple(targets), tuple(target_areas), segments,
                        tuple(user_colors), tuple(ex_areas), tuple(ex_cats), gender, basis)


def read_segments(segment, gender) -> tuple[tuple[str, ...], str | None, str]:
    """요청의 segment(고객군 1개 또는 목록) / gender(남·여·공용) → (고객군, 성별, 근거).

    segment가 있으면 그것을 쓴다. 없고 gender가 있으면 GENDER_SEGMENTS. 둘 다 없으면 ((), None, "all")이고,
    트렌드마다 Item 성별로 다시 정한다(trend_common).
    """
    segs = read_categories(segment)
    bad = [s for s in segs if s not in SEGMENTS]
    if bad:
        raise ValueError(f"segment는 {', '.join(SEGMENTS)} 중에서 골라야 합니다: {', '.join(map(repr, bad))}")
    if gender in ("", None):
        gender = None
    elif gender not in GENDER_SEGMENTS:
        raise ValueError(f"gender는 {', '.join(GENDER_SEGMENTS)} 중 하나여야 합니다: {gender!r}")
    if segs:
        return tuple(segs), gender, "segment"
    if gender is not None:
        return GENDER_SEGMENTS[gender], gender, "gender"
    return (), None, "all"


def trend_common(c: Common, trend: dict) -> Common:
    """요청에 고객군·성별이 없으면 이 트렌드 Item들의 gender로 고객군을 정한다 (2026-11-08 결정).

    나온 성별의 고객군을 합친다. 공용이 하나라도 있거나 성별이 모두 비면 전체.
    """
    if c.segment_basis != "all" or not isinstance(trend, dict):
        return c
    items = trend.get("items") if isinstance(trend.get("items"), list) else []
    genders = []
    for it in items:
        g = it.get("gender") if isinstance(it, dict) else None
        if g in GENDER_SEGMENTS and g not in genders:
            genders.append(g)
    if not genders or "공용" in genders:
        return replace(c, item_genders=tuple(genders))
    segs = list(dict.fromkeys(s for g in genders for s in GENDER_SEGMENTS[g]))
    return replace(c, segments=tuple(segs), segment_basis="item_gender", item_genders=tuple(genders))


def user_color_notes(ti) -> list[str]:
    """사용자 색을 어떻게 썼는지 (limitations).

    트렌드 아이템 색이 비어 있는 것(02가 색을 확인하지 못함)과 색은 있는데 안 겹치는 것을 나눠 적는다.
    """
    if ti.color_basis == "user_colors_in_trend" and ti.user_colors_excluded:
        return [f"사용자 색 중 {', '.join(ti.user_colors_excluded)}은(는) 이 트렌드 아이템 색에 없어 빼고, "
                f"트렌드 아이템에도 있는 사용자 색({', '.join(ti.colors)})과 어울리는 색으로 찾았습니다."]
    if ti.color_basis == "user_colors":
        if not ti.item_colors:
            return [f"트렌드 아이템 색이 확인되지 않아(미확인) 사용자 색({', '.join(ti.colors)})과 어울리는 색으로 찾았습니다."]
        return [f"사용자 색({', '.join(ti.colors)})이 이 트렌드 아이템 색에 없습니다"
                f"(트렌드 아이템 색: {', '.join(ti.item_colors)}). 사용자 색과 어울리는 색으로 찾았습니다."]
    return []


def segment_note(c: Common) -> str:
    """고객군을 어떻게 정했는지 (limitations 한 줄)."""
    segs = ", ".join(c.segments)
    if c.segment_basis == "segment":
        return f"요청 고객군({segs})에서 찾았습니다."
    if c.segment_basis == "gender":
        return (f"요청 성별({c.gender})이 공용이라 전체 고객군에서 찾았습니다." if not c.segments
                else f"요청 성별({c.gender})에 맞는 고객군({segs})에서 찾았습니다.")
    if c.segment_basis == "item_gender":
        return f"요청 성별이 없어 트렌드 아이템 성별({', '.join(c.item_genders)})에 맞는 고객군({segs})에서 찾았습니다."
    if "공용" in c.item_genders:
        return "요청 성별이 없고 트렌드 아이템에 공용이 있어 전체 고객군에서 찾았습니다."
    return "성별 정보가 없어 전체 고객군에서 찾았습니다."


# ---------------------------------------------------------------- 응답 조립

def _output(req: dict, status: str, product, trend_result, season_basis=None, evidence=None, errors=None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": req.get("request_id"),
        "agent_id": AGENT_ID,
        "status": status,
        "product": product,
        "trend_result": trend_result,
        "season_basis": season_basis,
        "evidence": evidence or [],
        "errors": errors or [],
    }


def error_output(req: dict, code: str, message: str) -> dict:
    """공통 입력 오류 (트렌드를 돌릴 수 없음): product·trend_result 없음."""
    return {**_output(req, "error", None, None, errors=[{"code": code, "message": message}]), "summary": message}


def _season_label(period) -> str:
    return f"{period.target_year} {SEASON_LABELS_KO[period.season]}"


def _season_code(period) -> str:
    return f"{period.target_year}{period.season}" if period.season in ("SS", "FW") else _season_label(period)


def _season_basis(c: Common) -> dict:
    p = c.period
    return {"season": _season_label(p), "source": c.season_source,   # 02가 준 출처: user / inferred / unknown
            **p.to_period(), "n_periods": p.n_periods, "basis": p.basis}


def matched_trend_colors(product_color: str, trend_colors: list[str]) -> list[str]:
    """상품 색과 어울리는 트렌드 색 전부 (조합표)."""
    return [t for t in trend_colors
            if (k := normalize_color(t)) and product_color in COLOR_PAIRS[k]]


def same_trend_colors(product_color: str, trend_colors: list[str]) -> list[str]:
    """상품 색과 같은 기준 색 (톤온톤)."""
    return [t for t in trend_colors if normalize_color(t) == product_color]


def _evidence(now: str, period, areas, colors, trend_period, with_images: bool = False,
              outfit_ref: dict | None = None) -> list[dict]:
    """데이터 출처. outfit_ref: CLIP 유사도 필터의 기준 이미지(무신사 착샷·상품 이미지) (E-006, 쓴 경우만)."""
    common = {"published_at": None, "observed_at": DATA_END.isoformat(), "fetched_at": now,
              "permission_status": LICENSE, "manifest_id": "TBD"}
    image_ev = [{"evidence_id": f"{NS}:E-005", "source_id": "hm_images", "source_type": "internal_product_image",
                 "source_name": "H&M 상품 이미지", "data_nature": "fact",
                 "locator": "images/<article_id 앞 3자리>/<article_id>.jpg", **common}] if with_images else []
    if outfit_ref:
        catalog = outfit_ref.get("kind") == "catalog_item"
        image_ev.append({
            "evidence_id": f"{NS}:E-006",
            "source_id": outfit_ref.get("source") or ("musinsa_catalog" if catalog else "musinsa_snap"),
            "source_type": "external_product_image" if catalog else "external_outfit_image",
            "source_name": ("무신사 상품 이미지" if catalog else "무신사 스냅 착샷")
                           + " (02 similar_products, CLIP 유사도 필터의 기준 이미지)",
            "data_nature": "fact", "locator": outfit_ref.get("image_url"),
            **{**common, "observed_at": None, "permission_status": "02 트렌드 결과의 무신사 이미지"}})
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


def _round(v, nd=3):
    x = _num(v)
    return None if x is None else round(x, nd)


def reason_detail(rs: dict) -> str:
    """추천 이유의 숫자 부분 한 문장 (summary 뒤에 붙음). 숫자는 모두 rs 안의 값."""
    t = rs["recent_trend"]
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
    matched, same = rs["color"]["matched_trend_colors"], rs["color"].get("tone_on_tone") or []
    pc = rs["color"]["product_color"]
    head = (f"{'·'.join(matched)}와 어울리는 {pc}. " if matched and not same
            else f"{'·'.join(same)}와 같은 색(톤온톤) {pc}. " if same and not matched
            else f"{'·'.join(matched)}와 어울리고 {'·'.join(same)}와 같은 색(톤온톤)인 {pc}. " if same
            else "")
    return head + ", ".join(parts) + "."


def build_reason(r, am, target, ti: TrendInput, season_name: str, weeks: int = 4) -> dict:
    """1위 상품을 고른 근거 숫자 (추천 이유 문장은 summary에)."""
    g = lambda k: getattr(r, k, None)  # noqa: E731  (추세 실패 등으로 컬럼이 없을 수 있음)
    return {
        "pairing": {"basis": am.basis, "target_categories": read_categories(target),
                    "trend_categories": ti.categories if am.basis == "trend" else [],
                    "product_area": g("kf_area")},
        "color": {"product_color": r.kf_color,
                  "matched_trend_colors": matched_trend_colors(r.kf_color, ti.colors),
                  "tone_on_tone": same_trend_colors(r.kf_color, ti.colors),
                  "trend_color_basis": ti.color_basis,
                  "rule": (USER_COLOR_RULE if ti.color_basis in USER_COLOR_BASES else COLOR_RULE)
                          if ti.colors else "색 조건 없음"},
        "stock_value_krw": _num(g("stock_value_krw"), int),
        "sell_through": _round(g("sell_through")),
        "recent_trend": {"weeks": weeks, "recent_sold": _num(g("recent_sold"), int),
                         "prev_sold": _num(g("prev_sold"), int),
                         "basis": g("trend_basis"),       # peer_relative / too_few_sales
                         "value": _round(g("trend")),
                         "peer_value": _round(g("peer_trend")),
                         "raw_value": _round(g("trend_raw"))},
        "season_share": _round(g("season_share")),
        "season_name": season_name,
        "score": _round(g("score")),
        "data_nature": {"stock_value_krw": "sample", "sell_through": "sample",
                        "recent_trend": "fact", "season_share": "fact"},
    }


def build_product(r, period, image: dict | None = None) -> dict:
    """상품 정보만 (순위·이유는 trend_result에)."""
    return {
        "style_id": r.article_id[:7],
        "sku_ids": [r.article_id],
        "category": to_vocab(r.kf_category),
        "season": _season_code(period),
        "attributes": {
            "color": r.kf_color,
            "material": [to_vocab(m) for m in r.materials] if r.materials is not None else [],
            "fit": r.fit if isinstance(r.fit, str) else None,
        },
        "inventory": {
            "quantity": int(r.stock_qty),
            "unit": "pieces",
            "as_of": DATA_END.isoformat(),
            "definition": STOCK_VERSION,
            "data_nature": "sample",
        },
        "image": image,
    }


def _trend_result(ti: TrendInput, status: str, summary: str, search: dict, rank=None, reason=None,
                  limitations=None, errors=None) -> dict:
    return {
        "style": {"id": ti.style_id, "name": ti.name},
        "status": status,
        "product_index": 0,          # products 안의 위치. 트렌드 여러 개면 multi.py가 순서대로 붙임
        "rank": rank,
        "reason": reason,
        "summary": summary,
        "search": search,
        "limitations": limitations or [],
        "errors": errors or [],
    }


def _pick_with_images(ranked: pd.DataFrame, image_dir, limitations: list[str]):
    """순위 위에서부터 H&M 이미지가 있는 상품 1개를 고른다.

    1위에 이미지가 없으면 이미지가 있는 다음 순위 상품을 고르고(rank는 실제 순위 그대로) limitations에 적는다.
    후보 전체에 이미지가 하나도 없으면 1위를 이미지 없이(image: null) 돌려준다.
    """
    if ranked.empty:
        return None, None
    row = lambda i: SimpleNamespace(**ranked.iloc[i].to_dict())  # noqa: E731  (열 이름으로 읽기: row.rank 등)
    remote_ready = getattr(image_dir, "images_available", None)
    local_ready = Path(image_dir).is_dir() if isinstance(image_dir, (str, Path)) else False
    if not (bool(remote_ready) if remote_ready is not None else local_ready):
        limitations.append("이미지 폴더가 없어 상품 사진 없이 순위 1위를 반환합니다.")
        return row(0), None
    for i, aid in enumerate(ranked["article_id"]):
        if has_image(image_dir, aid):
            if i > 0:
                limitations.append(f"순위 1~{i}위 상품은 H&M 이미지가 없어, 이미지가 있는 최상위 "
                                   f"{i + 1}위 상품을 골랐습니다.")
            image = load_image(image_dir, aid)
            limitations.extend(take_notes(image_dir))
            return row(i), image
    limitations.append("후보 상품 중 H&M 이미지가 있는 상품이 없어 순위 1위를 사진 없이 반환합니다.")
    return row(0), None


def _to_vocab_frame(cand: pd.DataFrame) -> pd.DataFrame:
    """예전 이름으로 만든 DB도 vocabulary_v1 이름으로 (티셔츠, 울/캐시미어 등)."""
    if cand.empty:
        return cand
    cand = cand.copy()
    cand["kf_category"] = cand["kf_category"].map(to_vocab)
    cand["materials"] = cand["materials"].map(lambda ms: [to_vocab(m) for m in ms] if ms is not None else ms)
    return cand


# ---------------------------------------------------------------- 제외 카테고리·여러 사용자 카테고리

def exclude_areas(areas: list[str], c: Common) -> list[str]:
    return [a for a in areas if a not in c.exclude_areas]


def drop_excluded(cand: pd.DataFrame, c: Common) -> pd.DataFrame:
    """제외 세부 카테고리 상품 빼기 (영역 제외는 조회 전에 areas에서 뺀다)."""
    if cand.empty or not c.exclude_categories:
        return cand
    ex = {to_vocab(x) for x in c.exclude_categories}
    return cand[~cand["kf_category"].isin(ex)].reset_index(drop=True)


def pairing_notes(am, areas: list[str], c: Common) -> list[str]:
    """사용자 카테고리 여러 개·제외 카테고리 설명 (limitations)."""
    out = []
    if am.basis == "user" and len(am.given) > 1:
        if am.fallback:
            out.append(f"사용자 카테고리({', '.join(am.given)}) 모두와 어울리는 공통 짝이 없어, 각 짝을 합친 "
                       f"영역({', '.join(am.areas) or '없음'})에서 찾았습니다.")
        else:
            out.append(f"사용자 카테고리({', '.join(am.given)})의 공통 짝({', '.join(am.areas) or '없음'})에서 찾았습니다.")
    gone = [a for a in am.areas if a in c.exclude_areas]
    if gone:
        out.append(f"제외 카테고리라 짝 영역에서 {', '.join(gone)}을(를) 뺐습니다.")
    if c.exclude_categories:
        out.append(f"제외 카테고리({', '.join(c.exclude_categories)}) 상품은 후보에서 뺐습니다.")
    return out


# ---------------------------------------------------------------- 실행

def run(request: dict, con, summarizer: Summarizer | None = None,
        now: datetime | None = None, image_dir: str | None = None, clip_filter=None) -> dict:
    """요청 1건(트렌드 1개) → 응답 dict. con은 read_only DuckDB 연결.

    - season 필수(02가 정해서 줌). 03은 시즌을 계산하지 않는다
    - trend 필수: 02 결과의 트렌드 1개 {style, items}. 트렌드 색·카테고리는 trend_input.read_trend 규칙
    - 짝 카테고리: target_category(02) → 없으면 트렌드 카테고리 짝 교집합 → 둘 다 없으면 전체 영역
    - 사용자 색(target_colors, 02)이 있으면: 트렌드 아이템 색과 겹치는 사용자 색만, 하나도 안 겹치면 사용자 색 전부를 기준 색으로
      (안 겹치는 색은 limitations에 적음). 없으면 트렌드 색이 기준 색
    - 기준 색이 있으면 짝 카테고리 안에서 "기준 색과 어울리는 색 중 가장 많이 겹치는 색"으로 매치. 없으면 색 조건 없이
    - 카테고리·색 모두 없음 → 그 트렌드는 error
    - 응답 상품은 H&M 이미지가 있는 최상위 1개
    """
    req = request or {}
    err, c = check_common(req)
    if err:
        return err
    trend = req.get("trend")
    if not isinstance(trend, dict):
        return error_output(req, "missing_fields",
                            "필수 입력이 없습니다: trend. 02 트렌드 분석 결과의 트렌드 1개({style, items})를 "
                            f"trend 필드에 담아 주세요. {_missing_detail(req, ['trend'])}")
    c = trend_common(c, trend)
    ti = read_trend(trend, list(c.target_areas), list(c.user_colors))
    return run_trend(req, c, ti, con, summarizer, now, image_dir, ref=pick_reference(trend, list(c.target_areas)),
                     clip_filter=clip_filter)


COLOR_MODES = ("max_overlap", "any", "none")
COLOR_MODE_NAMES = {"max_overlap": "가장 많이 겹치는 색", "any": "하나라도 어울리는 색", "none": "색 조건 없음(21색 전체)"}


def color_attempts(ti: TrendInput, cm) -> list[tuple[str, list[str]]]:
    """재고 조회에 쓸 색을 시도 순서대로 [(mode, colors)].

    max_overlap  기준 색과 어울리는 색 중 가장 많이 겹치는 색 + 기준 색 자체(톤온톤)
    any          후보 0개일 때: 기준 색 하나라도와 어울리는 색 전부 + 톤온톤 (max_overlap과 같으면 건너뜀)
    none         그래도 0개일 때: 트렌드 색 조건을 풀고 21색 전체
    사용자 색(USER_COLOR_BASES)은 완화하지 않는다. 기준 색이 없으면 처음부터 none.
    """
    if not ti.colors:
        return [("none", list(KF_COLORS))]
    out = [("max_overlap", list(cm.colors))]
    if ti.color_basis in USER_COLOR_BASES:
        return out
    wider = any_matching_colors(cm)
    if set(wider) != set(cm.colors):
        out.append(("any", wider))
    if set(KF_COLORS) != set(out[-1][1]):
        out.append(("none", list(KF_COLORS)))
    return out


def relaxed_conditions(ti: TrendInput, mode: str) -> list[dict]:
    """최종 조회에서 완화한 트렌드 색 조건 (search.relaxed)."""
    if not ti.colors or mode == "max_overlap":
        return []
    out = [{"condition": "color_rule", "from": "max_overlap", "to": "any"}]
    if mode == "none":
        out = [{"condition": "trend_colors", "from": list(ti.colors), "to": []}]
    return out


def relaxed_notes(ti: TrendInput, tried: list[dict]) -> list[str]:
    """완화 설명 (limitations)."""
    if len(tried) < 2:
        return []
    steps = " → ".join(COLOR_MODE_NAMES[t["mode"]] for t in tried)
    last = tried[-1]
    head = f"재고 후보가 없어 트렌드 색({', '.join(ti.colors)}) 조건을 완화했습니다: {steps}."
    if last["n_candidates"] == 0:
        return [head + " 완화한 뒤에도 후보가 없습니다."]
    return [head]


def _ref_label(ref: dict) -> str:
    """기준 이미지 설명: 무신사 스냅 착샷 / 무신사 상품 이미지(블라우스)."""
    if ref.get("kind") == "catalog_item":
        return f"무신사 상품 이미지(상품 {ref.get('product_id')}, 트렌드 아이템 {ref.get('item_category')})"
    return f"무신사 스냅 착샷(상품 {ref.get('product_id')}, 트렌드 아이템 {ref.get('item_category')}와 일치도가 가장 높은 스냅)"


def clip_notes(ref: dict | None, clip: dict | None) -> list[str]:
    """기준 이미지·CLIP 설명 (limitations)."""
    if ref is None:
        return ["트렌드 아이템의 유사 상품에 무신사 착샷·상품 이미지가 없어 소재·디테일 유사도(CLIP) 필터 없이 찾았습니다."]
    if clip is None or clip.get("skip_reason") == "no_candidates":
        return []
    label = _ref_label(ref)
    wa = "와" if ref.get("kind") == "catalog_item" else "과"       # "…블라우스)와" / "…스냅)과"
    if not clip["applied"]:
        why = {"no_index": "CLIP 인덱스가 없어", "no_match": "재고 후보가 CLIP 인덱스에 없어",
               "no_query": "기준 이미지를 열 수 없고 소재·디테일 정보도 없어"}[clip["skip_reason"]]
        return [f"{why} {label}{wa}의 유사도(CLIP) 필터 없이 찾았습니다."]
    out = [f"{label}{wa} 소재·질감·디테일 유사도(CLIP, 흑백 이미지 + 소재·디테일, 색 제외)로 재고 색마다 유사도 상위 "
           f"{clip['keep_ratio']:.0%}를 남겨 후보 {clip['n_before']:,}개 중 {clip['n_after']:,}개를 남겼습니다."]
    if ref.get("kind") == "catalog_item":
        out.append("무신사 스냅 착샷이 없어 트렌드 아이템의 무신사 상품 이미지를 기준으로 썼습니다.")
    if not clip["image_used"]:
        out.append("기준 이미지를 열 수 없어 소재·디테일 텍스트만으로 유사도를 계산했습니다.")
    elif not clip.get("text_used", True):
        out.append("기준 이미지에 소재·디테일 정보가 없어 이미지만으로 유사도를 계산했습니다.")
    if clip["n_indexed"] < clip["n_before"]:
        out.append(f"CLIP 인덱스에 없는 후보 {clip['n_before'] - clip['n_indexed']:,}개는 유사도 0으로 보았습니다.")
    return out


def run_trend(req: dict, c: Common, ti: TrendInput, con, summarizer: Summarizer | None = None,
              now: datetime | None = None, image_dir: str | None = None, ref: dict | None = None,
              clip_filter=None) -> dict:
    """공통 값 검사가 끝난 뒤 트렌드 1개를 처리한다.

    ref: pick_reference로 고른 CLIP 기준 이미지(착샷, 없으면 상품 이미지). None이면 CLIP 필터 없음
    clip_filter: (후보, 기준 이미지) → (남은 후보, ClipInfo). 기본은 clip_matcher.clip_filter (테스트에서 바꿈)
    """
    now = now or datetime.now(timezone.utc)
    summarizer = summarizer or TemplateSummarizer()
    period, target = c.period, c.target
    season_basis = _season_basis(c)
    search = {"trend_colors": ti.colors, "trend_color_basis": ti.color_basis, "trend_color_items": ti.color_items,
              "user_colors": ti.user_colors, "user_colors_excluded": ti.user_colors_excluded,
              "trend_categories": ti.categories, "excluded_categories": c.excluded, "areas": None,
              "colors": None, "color_mode": None, "color_overlap": None, "color_attempts": [], "relaxed": [],
              "outfit_reference": snap_reference(ref, None), "clip": None,
              "segments": list(c.segments), "segment_basis": c.segment_basis}

    def trend_error(code: str, message: str) -> dict:
        tr = _trend_result(ti, "error", message, search, errors=[{"code": code, "message": message}])
        return _output(req, "error", None, tr, season_basis)

    if not ti.colors and not target and not ti.categories:
        return trend_error("missing_fields",
                           "카테고리와 트렌드 색이 모두 없습니다 (사용자 카테고리, 트렌드 아이템의 "
                           "category_major·category_minor·colors 중 하나는 있어야 합니다). "
                           "02 결과의 items가 담겼는지 확인하세요.")
    try:
        am = resolve_pair_areas(list(c.targets), ti.categories)
    except ValueError as e:
        return trend_error("invalid_input", str(e))
    areas = exclude_areas(am.areas, c)
    search["areas"] = areas

    limitations = [SAMPLE_WARNING, *period.warnings, segment_note(c)]
    limitations += pairing_notes(am, areas, c)
    if c.season_source == "inferred":
        limitations.append(f"시즌 {_season_label(period)}은 사용자 입력이 아니라 쿼리 전처리 단계가 정한 값입니다.")
    if am.basis == "all":
        limitations.append("카테고리 입력이 없어 상의·하의·아우터·원피스 전체에서 찾았습니다.")
    elif am.basis == "trend":
        limitations.append(f"사용자 카테고리가 없어 트렌드 카테고리({', '.join(am.given)})의 짝으로 찾았습니다.")
        if am.fallback:
            limitations.append(
                f"트렌드 카테고리 모두와 어울리는 공통 짝이 없어, 트렌드 카테고리와 그 짝 전체({', '.join(areas)})에서 "
                f"순위로 골랐습니다.")
    if not areas:
        msg = f"제외 카테고리({', '.join(c.excluded)})를 빼니 찾을 짝 영역이 없습니다."
        tr = _trend_result(ti, "no_results", msg, search, limitations=limitations + [msg])
        return _output(req, "no_results", None, tr, season_basis)

    limitations += user_color_notes(ti)

    cm = None
    color_label = "사용자 색" if ti.color_basis in USER_COLOR_BASES else "트렌드 색"
    if ti.colors:
        if ti.color_basis == "all_items" and target:
            limitations.append(f"사용자 카테고리({target}) 아이템에 색이 없어 트렌드 아이템 전체의 색을 썼습니다.")
        cm = get_matching_colors(ti.colors)
        if cm.unknown:
            limitations.append(f"K-Fashion 색으로 바꿀 수 없는 트렌드 색은 제외했습니다: {', '.join(cm.unknown)}")
        if not cm.colors:
            return trend_error("invalid_input", f"알 수 있는 트렌드 색이 없습니다: {', '.join(ti.colors)}")
        search["color_overlap"] = cm.overlap
    else:
        limitations.append("트렌드에 색이 없어 색 조건 없이 21색 전체에서 찾았습니다.")

    # 재고 조회: 후보가 0개면 트렌드 색 조건만 완화해 다시 조회 (사용자 색·카테고리는 풀지 않음)
    tried = []
    for mode, colors in color_attempts(ti, cm):
        cand = drop_excluded(_to_vocab_frame(get_inventory_status(con, areas, colors, period, segment=c.segments)), c)
        tried.append({"mode": mode, "colors": colors, "n_candidates": int(len(cand))})
        if not cand.empty:
            break
    search.update({"colors": colors, "color_mode": mode, "color_attempts": tried,
                   "relaxed": relaxed_conditions(ti, mode)})
    if mode == "max_overlap" and cm.n_trend > 1 and cm.overlap < cm.n_trend:
        limitations.append(f"{color_label} {cm.n_trend}개 모두와 어울리는 색이 없어, 가장 많은 {cm.overlap}개와 "
                           f"어울리는 색({', '.join(cm.matching)})으로 찾았습니다.")
    limitations += relaxed_notes(ti, tried)

    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds") if now.tzinfo else now.isoformat(timespec="seconds")
    errors: list[dict] = []
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

    # 기준 이미지(착샷, 없으면 상품 이미지)가 있으면 CLIP 유사도 필터를 항상 건다 (후보를 0개로 만들지 않음)
    clip = None
    if ref is not None:
        if clip_filter is None:
            from stock_agent.clip_matcher import clip_filter
        cand, info = clip_filter(cand, ref)
        clip = info.to_dict()
    search.update({"outfit_reference": snap_reference(ref, clip), "clip": clip})
    limitations += clip_notes(ref, clip)

    ranked = rank_overstock(cand)                        # 후보 전체 순위
    row, image = _pick_with_images(ranked, image_dir or DEFAULT_IMAGE_DIR, limitations)

    ti_used = replace(ti, colors=[]) if mode == "none" else ti
    reason = None
    if row is not None:
        reason = build_reason(row, am, target, ti_used, SEASON_LABELS_KO[period.season])
        if mode == "any":
            reason["color"]["rule"] = reason["color"]["rule"].replace("어울리는 색 중 가장 많이 겹치는 색", "하나라도 어울리는 색")
    facts = {
        "season_label": _season_label(period),
        "target": target or "카테고리 미지정",
        "area_basis": am.basis,
        "given_areas": am.given,
        "areas": areas,
        "trend_colors": ti_used.colors,
        "colors_relaxed": bool(ti.colors) and mode == "none",
        "color_mode": mode,
        "color_label": color_label,
        "n_candidates": int(len(cand)),
        "n_items": 0 if row is None else 1,
        "top1_rank": None if row is None else int(row.rank),
        "top1_category": None if row is None else to_vocab(row.kf_category),
        "top1_color": None if row is None else row.kf_color,
        "top1_stock_qty": None if row is None else int(row.stock_qty),
        "reason_detail": None if reason is None else reason_detail(reason),
    }
    summary, warn = safe_summary(summarizer, facts)
    limitations += warn

    status = "no_results" if row is None else ("partial" if errors else "ok")
    tr = _trend_result(ti, status, summary, search,
                       rank=None if row is None else int(row.rank), reason=reason,
                       limitations=limitations, errors=errors)
    product = None if row is None else build_product(row, period, image)
    resp = _output(req, status, product, tr, season_basis,
                   evidence=_evidence(fetched_at, period, areas, colors, trend_period, with_images=image is not None,
                                      outfit_ref=ref if clip and clip["applied"] else None))
    problems = validate_output(resp)
    if problems:
        return error_output(req, "invalid_response", "응답 형식 검사 실패: " + "; ".join(problems))
    return resp


# ---------------------------------------------------------------- 형식 검사

STATUSES = ("ok", "partial", "no_results", "error")
PRODUCT_KEYS = ("style_id", "sku_ids", "category", "season", "attributes", "inventory", "image")
TREND_RESULT_KEYS = ("style", "status", "product_index", "rank", "reason", "summary", "search",
                     "limitations", "errors")


def check_product(prod: dict, where: str) -> list[str]:
    p = [f"{where}.{k} 없음" for k in PRODUCT_KEYS if k not in prod]
    if prod.get("inventory", {}).get("quantity", -1) < 0:
        p.append(f"{where} 재고 음수")
    return p


def check_trend_result(tr: dict, where: str) -> list[str]:
    p = [f"{where}.{k} 없음" for k in TREND_RESULT_KEYS if k not in tr]
    if tr.get("status") not in STATUSES:
        p.append(f"{where}.status 값 오류")
    return p


def validate_output(resp: dict) -> list[str]:
    """트렌드 1개 응답의 최소 형식 검사 (팀 JSON Schema가 확정되기 전까지)."""
    p = []
    for k, t in {"schema_version": str, "request_id": str, "agent_id": str, "status": str,
                 "evidence": list, "errors": list}.items():
        if not isinstance(resp.get(k), t):
            p.append(f"{k} 형식 오류")
    if resp.get("status") not in STATUSES:
        p.append("status 값 오류")
    if resp.get("product") is not None:
        p += check_product(resp["product"], "product")
    if resp.get("trend_result") is not None:
        tr = resp["trend_result"]
        p += check_trend_result(tr, "trend_result")
        if (resp.get("product") is None) != (tr.get("status") in ("no_results", "error")):
            p.append("product 유무와 trend_result.status가 맞지 않음")
    return p
