"""stock-agent LLM agent — 트렌드 1개 → 과잉재고 상품 1개 (2026-11-08 결정, 2026-11-09 착샷 기준으로 변경)

LLM이 도구를 골라 부르고, 결과를 보고 다음 행동을 정한다(자율 추론 → 도구 선택 → 반복).
도구는 agent.py·overstock.py의 분석 함수를 감싼 것이고, 최종 응답 모양(product, trend_result)은 규칙 방식과 같다.

착샷 분석 (도구 반복 전에 1번, outfit.py)
    트렌드 아이템의 무신사 스냅 착샷(score 최고 1장)을 LLM이 보고, 모델이 함께 입은 옷의 소재·핏을 영역별로 뽑는다.
    그 값으로 짝 영역(착샷에 없는 영역은 뺌)과 재고 단계(A 소재·핏 일치 → B 소재 일치 → C 전체)를 코드가 정한다.
    스냅이 없거나, 아이템만 잘라낸 사진이거나, 이미지를 열 수 없으면 착샷 반영 없이 진행한다.

LLM이 정하는 것
    - 카테고리 조합: 사용자 카테고리 여러 개의 짝을 교집합/합집합 중 무엇으로 볼지, 트렌드 카테고리 중 무엇을 쓸지
    - 색 조합: 기준 색과 "가장 많이 겹치는 색"만 볼지, "하나라도 어울리는 색"까지 볼지
    - 후보가 0개일 때 트렌드 쪽 조건(트렌드 색·트렌드 카테고리)을 완화할지
    - 순위 상위 후보 중 어떤 상품을 고를지 (1위가 아니면 why_not_higher 필수)

LLM이 못 하는 것 (어기면 llm_failed / rule_violation)
    - 사용자 조건 완화: target_category·target_colors는 바꿀 수 없다
    - 제외 카테고리(exclude_categories)를 다시 넣기
    - 21색이 아닌 색, 없는 카테고리 쓰기, 트렌드 아이템에 없는 색·카테고리를 트렌드 조건으로 쓰기
    - get_inventory에서 find_categories·find_colors 결과보다 영역·색을 좁히기 (2026-11-09 결정)
    - 착샷 소재·핏 단계 바꾸기 (코드가 정함)
    - 순서 어기기: find_categories·find_colors 전에 get_inventory 등

LLM이 실패하면 규칙 방식으로 대신 내지 않고 오류로 돌려준다 (평가할 수 있게, 2026-11-08 결정)
    code: llm_failed (안쪽 코드. API 응답에서는 errors.to_team이 no_llm·llm_unavailable → SOURCE_UNAVAILABLE,
          timeout → TIMEOUT, 그 밖 → INTERNAL_ERROR로 바꾸고 원인은 reason에 둔다), reason:
        no_llm            LLM이 연결되지 않음 (LLM_API_KEY 없음)
        llm_unavailable   LLM에 연결할 수 없음 (키 오류·권한·모델 이름·네트워크)
        timeout           LLM 응답 시간 초과
        invalid_response  도구를 안 부름, 없는 도구, 인자 형식 오류, LLM 오류, 착샷 분석 답 형식 오류
        rule_violation    위 "못 하는 것"을 어김 (오류를 LLM에게 돌려주고 MAX_RULE_RETRIES번까지 다시 시도, 그다음 위반에서 실패)
        step_limit        도구 호출이 MAX_STEPS를 넘음
        invalid_final     finish의 상품이 마지막 순위 결과에 없음, 후보가 있는데 고르지 않음, 이유에 근거 없는 숫자,
                          1위가 아닌 상품인데 why_not_higher(더 높은 순위 대신 고른 이유)가 없음
    이때 product는 null, trend_result.status는 error, search.steps에 실패 전까지의 기록이 남는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from stock_agent import agent as A
from stock_agent import outfit as O
from stock_agent.attributes import to_vocab
from stock_agent.images import DEFAULT_IMAGE_DIR, has_image, load_image, take_notes
from stock_agent.llm import LLM, ImageUnavailable, LLMReply, LLMUnavailable
from stock_agent.overstock import (
    COMBINES,
    KF_COLORS,
    TOP_N,
    get_matching_colors,
    peer_relative_trend,
    rank_overstock,
    resolve_pair_areas,
    summarize_attributes,
)
from stock_agent.season import SEASON_LABELS_KO
from stock_agent.trend_input import USER_COLOR_BASES, TrendInput, llm_trend

MAX_STEPS = 12            # 트렌드 1개에 부를 수 있는 도구 수
MAX_TOP_K = 20
COLOR_MODES = ("max_overlap", "any")
FAIL_REASONS = ("no_llm", "llm_unavailable", "timeout", "invalid_response", "rule_violation", "step_limit",
                "invalid_final")
# 규칙 위반은 바로 실패시키지 않고 LLM에게 오류를 돌려줘 다시 고르게 한다 (2026-11-09 결정).
# 막힌 호출은 실행되지 않으므로 규칙 밖 결과는 나오지 않는다. 트렌드당 MAX_RULE_RETRIES번까지, 그다음 위반에서 실패.
RETRYABLE = ("rule_violation", "invalid_final")
MAX_RULE_RETRIES = 2
TIER_NAMES = {"A": "착샷 소재·핏이 모두 맞는 재고(A)", "B": "착샷 소재가 맞는 재고(B)",
              "C": "카테고리·색만 맞는 재고 전체(C)"}

SYSTEM = """당신은 VOGUE 패션 MD 파이프라인의 03 재고 agent입니다.
목표: 트렌드 1개에 대해, 사용자가 기획하는 상품과 함께 입었을 때 트렌드 색·스타일에 어울리는 짝 카테고리의 과잉재고 상품 1개를 고릅니다.
      고르는 상품은 사용자 카테고리 자체가 아니라 그 짝 카테고리입니다(예: 사용자가 상의를 기획하면 함께 코디할 하의·아우터 재고를 고름).
      트렌드와의 색·스타일 적합성을 고려해 후보 중 가장 잘 어울리는 상품을 고르세요.

일하는 방법
1. find_categories로 찾을 짝 영역을 정합니다. 사용자 카테고리가 여러 개면 교집합/합집합 중 무엇이 맞을지 판단하세요.
   작업의 outfit은 트렌드 아이템과 비슷한 무신사 스냅 착샷에서 모델이 함께 입은 옷입니다.
   outfit.status가 "skipped"이면 착샷 정보가 없는 것입니다. 착샷을 근거로 영역을 조정하거나 추론하지 마세요.
   착샷에 없는 짝 영역 제거는 코드가 자동으로 처리합니다. 직접 영역을 빼려 하지 마세요.
2. find_colors로 찾을 색을 정합니다. base_colors는 작업의 base_colors에서 시작합니다.
   결과 colors는 기준 색과 어울리는 색과 기준 색 자체(톤온톤)입니다.
3. get_inventory로 후보를 조회합니다. areas·colors는 생략하면 앞 도구 결과 전부를 씁니다. 일부만 골라 좁힐 수 없습니다.
   영역을 바꾸고 싶으면 find_categories를 다시 부르세요. get_inventory에서 직접 영역을 좁히는 것은 규칙 위반입니다.
   색을 바꾸고 싶으면 find_colors를 다시 부르세요. get_inventory에서 직접 색을 좁히는 것도 규칙 위반입니다.
   착샷 소재·핏이 있으면 코드가 후보를 단계로 나눕니다: A 소재·핏 일치 → 없으면 B 소재 일치 → 없으면 C 전체.
   결과의 tier가 지금 후보의 단계입니다. 이 단계는 바꿀 수 없습니다.
4. 후보가 0개이면 트렌드 쪽 조건만 완화합니다(트렌드 색 줄이기·빼기, mode=any, 트렌드 카테고리 줄이기).
   완화할 때는 note에 이유를 쓰세요.
5. rank_candidates로 순위를 보고, finish로 상품 1개와 이유를 냅니다. 순위는 과잉재고 순서(많이 남았는데 안 팔리는 순)이므로
   기본은 1위입니다. 1위가 아닌 상품을 고르면 why_not_higher에 더 높은 순위 상품들 대신 고른 구체적 이유를 쓰세요.
   이유는 도구 결과에서 그 상품과 더 높은 순위 상품이 실제로 다른 점이어야 합니다 (예: 더 높은 순위 상품에 사진이 없음).
   모든 후보에 공통인 점은 이유가 될 수 없습니다. 소재·핏은 이미 단계로 걸렀으므로 이유가 될 수 없습니다.
   find_categories가 정한 짝 영역은 모두 코디 가능한 영역이므로, 영역 차이(예: 짝 카테고리 안에서 어느 영역인지)는
   이유가 될 수 없습니다. 이미지가 있는 상품을 우선하세요.
   모든 완화 뒤에도 후보가 0개일 때만 finish(article_id=null)로 끝냅니다.

지켜야 할 것
- 사용자 조건(target_categories, target_colors)은 절대 완화하지 않습니다. 제외 카테고리는 다시 넣지 않습니다.
- 색은 21색만 씁니다.
- finish의 reason·why_not_higher에 숫자를 쓸 때는 도구 결과에 나온 숫자만 씁니다.
- 항상 도구를 부르세요. 도구 없이 글로만 답하면 실패로 처리됩니다.
- 규칙에 맞지 않는 호출은 실행되지 않고 도구 결과로 오류(error, message)가 돌아옵니다. 메시지를 보고 규칙에 맞게 다시 부르세요.
  규칙 위반은 트렌드마다 몇 번까지만 허용됩니다."""

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_NOTE = {"type": "string", "description": "이 선택(특히 완화)의 이유"}
TOOLS = [
    {"name": "find_categories",
     "description": "찾을 짝 영역(상의·하의·아우터·원피스)을 정한다. 제외 영역과 착샷에 없는 영역은 자동으로 빠진다. "
                    "사용자 카테고리가 있으면 base_categories는 비우거나 사용자 카테고리 그대로여야 한다.",
     "parameters": {"type": "object", "properties": {
         "base_categories": {**_STR_LIST, "description": "짝을 정할 카테고리. 생략하면 사용자 카테고리, 없으면 트렌드 카테고리 전부. "
                                                         "트렌드 카테고리일 때만 일부를 고르거나 []로 전체 영역을 볼 수 있다"},
         "combine": {"type": "string", "enum": list(COMBINES),
                     "description": "여러 카테고리의 짝 합치기. auto=교집합, 없으면 합집합"},
         "note": _NOTE}}},
    {"name": "find_colors",
     "description": "기준 색과 어울리는 색(조합표 v1)과 기준 색 자체(톤온톤)를 찾는다. 사용자 색이 있으면 base_colors는 작업의 base_colors 그대로여야 한다. "
                    "트렌드 색이면 일부만 쓰거나 []로 색 조건을 풀 수 있다.",
     "parameters": {"type": "object", "required": ["base_colors"], "properties": {
         "base_colors": _STR_LIST,
         "mode": {"type": "string", "enum": list(COLOR_MODES),
                  "description": "max_overlap=기준 색과 가장 많이 겹치는 색만(기본), any=하나라도 어울리는 색 전부"},
         "note": _NOTE}}},
    {"name": "get_inventory",
     "description": "find_categories의 영역 전부와 find_colors의 색 전부로 재고 후보를 조회한다(시즌·고객군·재고>0·제외 카테고리는 자동 적용). "
                    "areas·colors는 생략하거나 앞 결과와 똑같이 준다(좁힐 수 없음). "
                    "착샷 소재·핏이 있으면 코드가 단계(A 소재·핏 일치 → B 소재 일치 → C 전체)를 정해 그 단계 후보만 남긴다. "
                    "후보 수, 단계(tier), 단계별 수, 속성 분포를 돌려준다.",
     "parameters": {"type": "object", "properties": {
         "areas": _STR_LIST, "colors": _STR_LIST, "note": _NOTE}}},
    {"name": "rank_candidates",
     "description": "지금 후보(정해진 단계 안)를 과잉재고 순위(재고 금액·낮은 판매율·판매 감소·시즌 판매 비중)로 매겨 상위 top_k개를 돌려준다.",
     "parameters": {"type": "object", "properties": {
         "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K}}}},
    {"name": "finish",
     "description": "최종 상품을 낸다. article_id는 마지막 rank_candidates 결과 안에 있어야 한다. "
                    "후보가 0개일 때만 article_id=null. 1위가 아닌 상품이면 why_not_higher가 필수다.",
     "parameters": {"type": "object", "required": ["reason"], "properties": {
         "article_id": {"type": ["string", "null"]},
         "reason": {"type": "string", "description": "고른 이유 (숫자는 도구 결과에 나온 것만)"},
         "why_not_higher": {"type": "string",
                            "description": "1위가 아닌 상품을 고를 때 필수: 더 높은 순위 상품들 대신 이 상품을 고른 이유. "
                                           "도구 결과에서 실제로 다른 점만 (모든 후보에 공통인 점, 영역 차이, 소재·핏은 안 됨)"}}}},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


class AgentFailed(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def _bad_args(msg):
    return AgentFailed("invalid_response", msg)


def _violation(msg):
    return AgentFailed("rule_violation", msg)


def _str_list(args: dict, key: str, required: bool = False) -> list[str] | None:
    if key not in args or args[key] is None:
        if required:
            raise _bad_args(f"{key}가 없습니다")
        return None
    v = args[key]
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _bad_args(f"{key}는 문자열 목록이어야 합니다: {v!r}")
    return list(dict.fromkeys(x.strip() for x in v if x.strip()))


def _counts(s: pd.Series, top: int = 10) -> dict:
    return {str(k): int(v) for k, v in s.dropna().value_counts().head(top).items()}


def _dist(cand: pd.DataFrame) -> dict:
    if cand.empty:
        return {}
    mats = cand["materials"].explode()
    return {"area": _counts(cand["kf_area"]), "category": _counts(cand["kf_category"]),
            "color": _counts(cand["kf_color"]), "material": _counts(mats),
            "fit": _counts(cand["fit"].fillna("정보 없음"))}


# ---------------------------------------------------------------- 착샷 분석

def analyze_outfit(llm, ref: dict | None) -> O.Outfit:
    """고른 착샷(pick_snap) → LLM 이미지 분석. LLM 자체 실패는 AgentFailed, 데이터 문제(스냅 없음·이미지 오류)는 생략."""
    if ref is None:
        return O.skipped("no_snap")
    try:
        raw = llm.see(O.OUTFIT_SYSTEM, O.outfit_prompt(), ref["image_url"])
    except NotImplementedError:
        return O.skipped("no_vision", ref)
    except ImageUnavailable:
        return O.skipped("image_error", ref)
    except TimeoutError as e:
        raise AgentFailed("timeout", f"착샷 분석 중 LLM 응답 시간 초과 ({e})") from e
    except LLMUnavailable as e:
        raise AgentFailed("llm_unavailable", f"착샷 분석 중 LLM 연결 실패 ({e})") from e
    except Exception as e:
        raise AgentFailed("invalid_response", f"착샷 분석 중 LLM 오류: {type(e).__name__}: {e}") from e
    try:
        return O.parse_outfit(raw, ref)
    except ValueError as e:
        raise AgentFailed("invalid_response", str(e)) from e


def _outfit_for_llm(o: O.Outfit) -> dict:
    if not o.used:
        return {"status": "skipped", "message": O.SKIP_NOTES[o.skip_reason]}
    return {"status": "used", "garments": o.garments,
            "message": "착샷 소재·핏으로 코드가 재고 단계(A/B/C)를 정합니다. 착샷에 없는 짝 영역은 빠집니다."}


class ToolBox:
    """도구 상태 (트렌드 1개). 각 도구는 LLM에게 보낼 결과 dict를 돌려주고, 기록을 steps에 남긴다."""

    def __init__(self, c: A.Common, ti: TrendInput, con, image_dir, outfit: O.Outfit | None = None):
        self.c, self.ti, self.con, self.image_dir = c, ti, con, image_dir
        self.outfit = outfit or O.skipped("no_snap")
        self.steps: list[dict] = []
        self.violations: list[dict] = []      # 다시 시도하게 한 규칙 위반
        self.am = None                 # 마지막 find_categories
        self.cat_call: dict = {}
        self.areas: list[str] | None = None
        self.outfit_dropped: list[str] = []   # 착샷에 없어 뺀 짝 영역
        self.color_call: dict | None = None
        self.allowed_colors: list[str] | None = None
        self.inv: dict | None = None   # 마지막 get_inventory {areas, colors, note}
        self.cand_all: pd.DataFrame | None = None
        self.cand: pd.DataFrame | None = None
        self.tier: dict | None = None  # {"tier", "counts", "relaxed"}
        self.ranked: pd.DataFrame | None = None
        self.shortlist: list[str] = []
        self.final: dict | None = None
        self.errors: list[dict] = []
        self.extra_limits: list[str] = []
        self.trend_period = {"recent": ("", ""), "prev": ("", "")}
        self.results: list[dict] = []   # LLM에게 보낸 결과 (finish 이유의 숫자 검사용)

    # ---------------------------------------------------------- 도구
    def find_categories(self, args):
        c, ti = self.c, self.ti
        base = _str_list(args, "base_categories")
        combine = args.get("combine") or "auto"
        if combine not in COMBINES:
            raise _bad_args(f"combine은 {', '.join(COMBINES)} 중 하나여야 합니다: {combine!r}")
        if c.targets:
            if base not in (None, []) and set(A.areas_of(base)) != set(c.target_areas):
                raise _violation(f"사용자 카테고리({', '.join(c.targets)})는 바꿀 수 없습니다: {base}")
            am = resolve_pair_areas(list(c.targets), None, combine)
            used = list(c.targets)
        else:
            if base is None:
                base = list(ti.categories)
            unknown = [x for x in base if x not in ti.categories]
            if unknown:
                raise _violation(f"트렌드 카테고리에 없는 카테고리는 쓸 수 없습니다: {', '.join(unknown)}")
            am = resolve_pair_areas(None, base, combine)
            used = base
        self.am, self.cat_call = am, {"base_categories": used, "combine": combine, "note": args.get("note")}
        self.areas, self.outfit_dropped = O.visible_areas(A.exclude_areas(am.areas, c), self.outfit)
        return {"areas": self.areas, "basis": am.basis, "given_areas": am.given, "fallback": am.fallback,
                "excluded_areas": [a for a in am.areas if a in c.exclude_areas],
                "excluded_categories": list(c.exclude_categories),
                "not_in_outfit": self.outfit_dropped}

    def find_colors(self, args):
        ti = self.ti
        base = _str_list(args, "base_colors", required=True)
        mode = args.get("mode") or "max_overlap"
        if mode not in COLOR_MODES:
            raise _bad_args(f"mode는 {', '.join(COLOR_MODES)} 중 하나여야 합니다: {mode!r}")
        if ti.color_basis in USER_COLOR_BASES:
            if set(base) != set(ti.colors):
                raise _violation(f"사용자 색 기준({', '.join(ti.colors)})은 바꿀 수 없습니다: {base}")
        else:
            unknown = [x for x in base if x not in ti.colors]
            if unknown:
                raise _violation(f"트렌드 색에 없는 색은 기준 색으로 쓸 수 없습니다: {', '.join(unknown)}")
        if base:
            cm = get_matching_colors(base)
            if not cm.colors:
                raise _violation(f"알 수 있는 색이 없습니다: {', '.join(base)}")
            colors = cm.colors if mode == "max_overlap" else list(cm.counts) + [x for x in cm.tone_on_tone
                                                                                if x not in cm.counts]
            out = {"colors": colors, "mode": mode, "overlap": cm.overlap, "n_base": cm.n_trend,
                   "tone_on_tone": cm.tone_on_tone,
                   "unknown": cm.unknown, "counts": cm.counts}
        else:
            cm, colors = None, list(KF_COLORS)
            out = {"colors": colors, "mode": "none", "message": "기준 색이 없어 21색 전체를 씁니다"}
        self.color_call = {"base_colors": base, "mode": mode if base else "none", "cm": cm, "note": args.get("note")}
        self.allowed_colors = colors
        return out

    def _same_as_before(self, key: str, got: list[str] | None, allowed: list[str]) -> list[str]:
        """get_inventory의 areas·colors: 생략하면 앞 결과 전부, 주면 앞 결과와 같아야 한다 (좁히기 금지)."""
        if got is None:
            return list(allowed)
        name = {"areas": "영역", "colors": "색"}[key]
        extra = [x for x in got if x not in allowed]
        if extra:
            if key == "areas":
                ex = [a for a in extra if a in self.c.exclude_areas]
                if ex:
                    raise _violation(f"제외 카테고리는 다시 넣을 수 없습니다: {', '.join(ex)}")
                raise _violation(f"find_categories 결과({', '.join(allowed)})에 없는 영역: {', '.join(extra)}")
            raise _violation(f"find_colors 결과에 없는 색(21색이 아니거나 어울리지 않는 색): {', '.join(extra)}. "
                             f"쓸 수 있는 색: {', '.join(allowed)}")
        missing = [x for x in allowed if x not in got]
        if missing:
            how = ("find_colors를 다시 부르세요(트렌드 색을 줄이거나 mode를 바꿈)" if key == "colors"
                   else "find_categories를 다시 부르세요")
            raise _violation(f"get_inventory에서 {name}을 좁힐 수 없습니다(빠진 {name}: {', '.join(missing)}). "
                             f"앞 결과 전부를 쓰거나 생략하세요. 조건을 바꾸려면 {how}.")
        return list(allowed)

    def get_inventory(self, args):
        if self.areas is None or self.allowed_colors is None:
            raise _violation("get_inventory 전에 find_categories와 find_colors를 불러야 합니다")
        areas = self._same_as_before("areas", _str_list(args, "areas"), self.areas)
        colors = self._same_as_before("colors", _str_list(args, "colors"), self.allowed_colors)
        c = self.c
        cand = pd.DataFrame()
        if areas and colors:
            cand = A.drop_excluded(A._to_vocab_frame(A.get_inventory_status(self.con, areas, colors, c.period,
                                                                             segment=c.segments)), c)
        if not cand.empty:
            try:
                tr = A.get_sales_trend(self.con, cand["article_id"].tolist())
                self.trend_period = {k: tuple(d.isoformat() for d in v) for k, v in tr.attrs["period"].items()}
                cand = cand.merge(tr[["article_id", "recent_sold", "prev_sold"]], on="article_id", how="left")
                cand = peer_relative_trend(cand)
            except Exception as e:  # 추세만 실패하면 나머지 지표로 순위 (규칙 방식과 같음)
                if not any(x["code"] == "trend_failed" for x in self.errors):
                    self.errors.append({"code": "trend_failed", "message": f"최근 추세 계산 실패: {e}"})
                    self.extra_limits.append("최근 판매 추세를 계산하지 못해 순위에서 추세를 중립값으로 두었습니다.")
                cand["trend"] = 0.0
        self.inv = {"areas": areas, "colors": colors, "note": args.get("note")}
        self.cand_all = cand.reset_index(drop=True)
        self.ranked, self.shortlist, self.tier = None, [], None
        out = {"n_candidates": 0, "distribution": {}}
        if cand.empty:
            self.cand = self.cand_all
            return out
        if self.outfit.used and any(self.outfit.spec(a) for a in areas):
            tiered = O.assign_tiers(self.cand_all, self.outfit)
            self.cand, tier, counts = O.pick_pool(tiered)
            self.tier = {"tier": tier, "counts": counts, "relaxed": O.relaxed_conditions(self.outfit, tier, areas)}
            out.update({"tier": tier, "tier_counts": counts, "n_total": int(len(cand))})
        else:
            self.cand = self.cand_all
        out.update({"n_candidates": int(len(self.cand)), "distribution": _dist(self.cand)})
        return out

    def rank_candidates(self, args):
        if self.cand is None:
            raise _violation("rank_candidates 전에 get_inventory를 불러야 합니다")
        k = args.get("top_k", 10)
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= MAX_TOP_K:
            raise _bad_args(f"top_k는 1~{MAX_TOP_K} 정수여야 합니다: {k!r}")
        self.ranked = rank_overstock(self.cand, n=len(self.cand))
        top = self.ranked.head(k)
        self.shortlist = top["article_id"].tolist()
        rows = []
        for r in top.itertuples(index=False):
            rows.append({"article_id": r.article_id, "rank": int(r.rank), "category": r.kf_category,
                         "area": r.kf_area, "color": r.kf_color,
                         "materials": list(r.materials) if r.materials is not None else [],
                         "fit": r.fit if isinstance(r.fit, str) else None, "stock_qty": int(r.stock_qty),
                         "sell_through": A._round(r.sell_through), "score": A._round(r.score),
                         "has_image": has_image(self.image_dir, r.article_id)})
        out = {"n_candidates": int(len(self.ranked)), "candidates": rows}
        if self.tier:
            out["tier"] = self.tier["tier"]
        return out

    def finish(self, args):
        aid, reason = args.get("article_id"), args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _bad_args("finish에 reason이 없습니다")
        if aid is not None and not isinstance(aid, str):
            raise _bad_args(f"article_id는 문자열 또는 null이어야 합니다: {aid!r}")
        extra = A.numbers_in(reason) - A.allowed_numbers(self.results)
        if extra:
            raise AgentFailed("invalid_final", f"finish 이유에 도구 결과에 없는 숫자가 있습니다: {sorted(extra)}")
        if aid is None:
            no_area = self.areas == []          # 제외 카테고리를 빼니 찾을 영역이 없음
            if not no_area and (self.cand is None or len(self.cand) > 0):
                raise AgentFailed("invalid_final", "후보를 조회하지 않았거나 후보가 있는데 상품을 고르지 않았습니다")
        elif aid not in self.shortlist:
            raise AgentFailed("invalid_final", f"마지막 rank_candidates 결과에 없는 상품입니다: {aid}")
        why = args.get("why_not_higher")
        if why is not None and not isinstance(why, str):
            raise _bad_args(f"why_not_higher는 문자열이어야 합니다: {why!r}")
        why = (why or "").strip() or None
        rank = None
        if aid is not None:
            rank = int(self.ranked.loc[self.ranked["article_id"] == aid, "rank"].iloc[0])
            if rank > 1 and not why:
                raise AgentFailed("invalid_final", f"1위가 아닌 {rank}위 상품을 골랐는데 why_not_higher(더 높은 순위 대신 고른 이유)가 없습니다")
            if why:
                extra = A.numbers_in(why) - A.allowed_numbers(self.results)
                if extra:
                    raise AgentFailed("invalid_final", f"why_not_higher에 도구 결과에 없는 숫자가 있습니다: {sorted(extra)}")
        self.final = {"article_id": aid, "reason": reason.strip(), "why_not_higher": why if rank and rank > 1 else None}
        return {"ok": True}

    def call(self, name: str, args) -> dict:
        if not isinstance(args, dict):
            raise _bad_args(f"{name} 인자는 객체여야 합니다")
        try:
            out = getattr(self, name)(args)
        except ValueError as e:     # 모르는 카테고리 등
            raise _violation(str(e)) from e
        log_args = {k: v for k, v in args.items()}
        self.steps.append({"step": len(self.steps) + 1, "tool": name, "args": log_args,
                           "n_candidates": out.get("n_candidates"),
                           **({"areas": out["areas"]} if name == "find_categories" else {}),
                           **({"colors": out["colors"]} if name == "find_colors" else {}),
                           **({"tier": out["tier"]} if name == "get_inventory" and "tier" in out else {})})
        self.results.append(out)
        return out

    # ---------------------------------------------------------- 결과 정리
    def relaxed(self) -> list[dict]:
        """트렌드 쪽 조건 중 최종 조회에서 완화한 것 (착샷 단계 완화는 search.attribute_tier에)."""
        ti, out = self.ti, []
        if not self.c.targets and self.cat_call and ti.categories:
            used = self.cat_call["base_categories"]
            if set(used) != set(ti.categories):
                out.append({"condition": "trend_categories", "from": list(ti.categories), "to": used,
                            "why": self.cat_call.get("note")})
        cc = self.color_call
        if cc:
            if ti.color_basis not in USER_COLOR_BASES and set(cc["base_colors"]) != set(ti.colors):
                out.append({"condition": "trend_colors", "from": list(ti.colors), "to": cc["base_colors"],
                            "why": cc.get("note")})
            if cc["mode"] == "any":
                out.append({"condition": "color_rule", "from": "max_overlap", "to": "any", "why": cc.get("note")})
        return out


def _task(c: A.Common, ti: TrendInput, trend: dict, outfit: O.Outfit, max_steps: int) -> dict:
    return {
        "season": A._season_label(c.period), "segments": list(c.segments) or "전체",
        "segment_basis": c.segment_basis,
        "target_categories": list(c.targets), "exclude_categories": c.excluded,
        "user_colors": list(ti.user_colors),
        "base_colors": list(ti.colors), "color_basis": ti.color_basis,
        "trend_categories": list(ti.categories),
        "outfit": _outfit_for_llm(outfit),
        "trend": llm_trend(trend),
        "vocabulary": {"colors": list(KF_COLORS)},
        "max_steps": max_steps,
    }


def _color_notes(ti: TrendInput, cc: dict | None) -> list[str]:
    """사용자 색·겹침 설명 (규칙 방식과 같은 문장)."""
    out = A.user_color_notes(ti)
    cm = cc and cc.get("cm")
    if cm is not None:
        if cm.unknown:
            out.append(f"K-Fashion 색으로 바꿀 수 없는 트렌드 색은 제외했습니다: {', '.join(cm.unknown)}")
        label = "사용자 색" if ti.color_basis in USER_COLOR_BASES else "트렌드 색"
        if cc["mode"] == "max_overlap" and cm.n_trend > 1 and cm.overlap < cm.n_trend:
            out.append(f"{label} {cm.n_trend}개 모두와 어울리는 색이 없어, 가장 많은 {cm.overlap}개와 "
                       f"어울리는 색({', '.join(cm.matching)})으로 찾았습니다.")
    elif cc is not None and not ti.colors:      # 트렌드 색을 푼 경우는 완화 문장에 적음
        out.append("트렌드에 색이 없어 색 조건 없이 21색 전체에서 찾았습니다.")
    return out


def _relaxed_note(r: dict) -> str:
    name = {"trend_categories": "트렌드 카테고리", "trend_colors": "트렌드 색", "color_rule": "색 규칙"}[r["condition"]]
    fmt = lambda v: ", ".join(v) if isinstance(v, list) and v else ("없음" if isinstance(v, list) else  # noqa: E731
                                                                   {"max_overlap": "가장 많이 겹치는 색",
                                                                    "any": "하나라도 어울리는 색"}.get(v, v))
    why = f" (LLM 메모: {r['why']})" if r.get("why") else ""
    return f"후보를 찾으려고 {name} 조건을 완화했습니다: {fmt(r['from'])} → {fmt(r['to'])}.{why}"


def _outfit_notes(box: ToolBox, areas: list[str]) -> list[str]:
    o, out = box.outfit, []
    if not o.used:
        out.append(O.SKIP_NOTES[o.skip_reason])
        return out
    ref = o.ref or {}
    out.append(f"무신사 스냅 착샷(상품 {ref.get('product_id')}, 트렌드 아이템 {ref.get('item_category')}와 일치도가 가장 높은 스냅)에서 "
               f"LLM이 본 함께 입은 옷: {O.describe(o, [*areas, *box.outfit_dropped])}. LLM 이미지 판단이라 틀릴 수 있습니다.")
    if box.outfit_dropped:
        out.append(f"착샷에 {', '.join(box.outfit_dropped)}이(가) 없어 짝 영역에서 뺐습니다.")
    if o.dropped:
        out.append(f"착샷 분석 값 중 어휘(vocabulary_v1)에 없는 값은 쓰지 않았습니다: {', '.join(o.dropped)}")
    t = box.tier
    if t:
        n = t["counts"]
        if t["tier"] == "A":
            out.append(f"{TIER_NAMES['A']}에서 골랐습니다.")
        elif t["tier"] == "B":
            out.append(f"{TIER_NAMES['A']}가 없어 핏 조건을 풀고 {TIER_NAMES['B']}에서 골랐습니다.")
        else:
            out.append(f"착샷 소재·핏이 맞는 재고가 없어 소재·핏 조건을 풀고 {TIER_NAMES['C']}에서 골랐습니다 "
                       f"(소재 정보가 없는 재고와 다른 소재 재고는 같게 보고 과잉재고 순위로)." if t["relaxed"]
                       else f"{TIER_NAMES['C']}에서 골랐습니다.")
        out.append(f"재고 단계별 후보 수: A {n['A']:,}개, B {n['B']:,}개, C {n['C']:,}개.")
    elif box.inv and box.cand_all is not None and len(box.cand_all) and not any(o.spec(a) for a in box.inv["areas"]):
        out.append("착샷에서 짝 영역 옷의 소재·핏을 판단하지 못해 소재·핏 조건 없이 찾았습니다.")
    return out


def attribute_reason(row, outfit: O.Outfit, tier: str | None) -> dict:
    """고른 상품의 소재·핏과 착샷 속 같은 영역 옷의 소재·핏 비교."""
    mats = [to_vocab(m) for m in row.materials] if row.materials is not None else []
    fit = row.fit if isinstance(row.fit, str) else None
    area = getattr(row, "kf_area", None)
    spec = outfit.spec(area) or {"materials": [], "fit": None}
    m = O.material_match(mats, to_vocab(getattr(row, "kf_category", None)), spec["materials"])
    matched = [x for x in mats if x in spec["materials"]]
    if m and not matched and "데님" in spec["materials"]:
        matched = ["데님"]                        # 카테고리 청바지
    return {"product_materials": mats, "product_fit": fit, "outfit_area": area,
            "outfit_materials": list(spec["materials"]), "outfit_fit": spec["fit"],
            "matched_materials": matched,
            "materials_match": m,                 # true/false, null = 미확인 또는 비교 안 함
            "fit_match": O.fit_match(fit, spec["fit"]),
            "tier": tier}


def run_agent_trend(req: dict, c: A.Common, ti: TrendInput, trend: dict, con, llm: LLM | None,
                    now: datetime | None = None, image_dir: str | None = None, max_steps: int = MAX_STEPS) -> dict:
    """LLM agent로 트렌드 1개 처리. 응답 모양은 agent.run_trend와 같고 search에 mode·steps·relaxed·착샷 기록이 더 있다."""
    now = now or datetime.now(timezone.utc)
    image_dir = image_dir or DEFAULT_IMAGE_DIR
    period = c.period
    season_basis = A._season_basis(c)
    search = {"mode": "llm", "trend_colors": ti.colors, "trend_color_basis": ti.color_basis,
              "trend_color_items": ti.color_items, "user_colors": ti.user_colors,
              "user_colors_excluded": ti.user_colors_excluded, "trend_categories": ti.categories,
              "excluded_categories": c.excluded, "areas": None, "colors": None, "color_overlap": None,
              "outfit_reference": None, "attribute_tier": None, "steps": [], "relaxed": [],
              "segments": list(c.segments), "segment_basis": c.segment_basis}

    def trend_error(code: str, message: str, **extra) -> dict:
        tr = A._trend_result(ti, "error", message, search, errors=[{"code": code, "message": message, **extra}])
        return A._output(req, "error", None, tr, season_basis)

    # 입력 오류는 LLM 전에 (규칙 방식과 같은 검사)
    if not ti.colors and not c.targets and not ti.categories:
        return trend_error("missing_fields",
                           "카테고리와 트렌드 색이 모두 없습니다 (사용자 카테고리, 트렌드 아이템의 "
                           "category_major·category_minor·colors 중 하나는 있어야 합니다). "
                           "02 결과의 items가 담겼는지 확인하세요.")
    try:
        resolve_pair_areas(list(c.targets), ti.categories)
    except ValueError as e:
        return trend_error("invalid_input", str(e))
    if ti.colors and not get_matching_colors(ti.colors).colors:
        return trend_error("invalid_input", f"알 수 있는 트렌드 색이 없습니다: {', '.join(ti.colors)}")

    box = ToolBox(c, ti, con, image_dir)

    def failed(reason: str, message: str) -> dict:
        search["steps"] = box.steps
        search["outfit_reference"] = box.outfit.to_dict() if box.outfit.ref or box.outfit.used else None
        msg = f"LLM agent 실패({reason}): {message}"
        return trend_error("llm_failed", msg, reason=reason)

    if llm is None:
        return failed("no_llm", "LLM이 연결되지 않았습니다. stock_agent.llm.load_llm()에 LLM을 연결하세요.")

    ref = O.pick_snap(trend, list(c.target_areas))
    box.outfit = O.Outfit("failed", None, ref)        # 분석 중 LLM이 실패하면 어떤 착샷이었는지 남김
    try:
        box.outfit = analyze_outfit(llm, ref)
    except AgentFailed as e:
        return failed(e.reason, e.message)

    messages: list[dict] = [{"role": "user", "content": json.dumps(_task(c, ti, trend, box.outfit, max_steps),
                                                                   ensure_ascii=False, default=str)}]
    try:
        while box.final is None:
            try:
                reply = llm.next(SYSTEM, messages, TOOLS)
            except TimeoutError as e:
                raise AgentFailed("timeout", f"LLM 응답 시간 초과 ({e})") from e
            except LLMUnavailable as e:
                raise AgentFailed("llm_unavailable", f"LLM 연결 실패 ({e})") from e
            except AgentFailed:
                raise
            except Exception as e:
                raise AgentFailed("invalid_response", f"LLM 오류: {type(e).__name__}: {e}") from e
            if not isinstance(reply, LLMReply) or not reply.tool_calls:
                raise AgentFailed("invalid_response", "LLM이 도구를 부르지 않고 답했습니다")
            messages.append({"role": "assistant", "content": reply.text,
                             "tool_calls": [{"id": t.id, "name": t.name, "args": t.args} for t in reply.tool_calls]})
            for t in reply.tool_calls:
                if len(box.steps) >= max_steps:
                    raise AgentFailed("step_limit", f"도구 호출이 {max_steps}번을 넘었습니다")
                if t.name not in TOOL_NAMES:
                    raise AgentFailed("invalid_response", f"없는 도구: {t.name!r}")
                try:
                    out = box.call(t.name, t.args)
                except AgentFailed as e:
                    # 실패한 호출도 steps에 남긴다 (무엇을 하려다 막혔는지)
                    box.steps.append({"step": len(box.steps) + 1, "tool": t.name, "args": dict(t.args or {})
                                      if isinstance(t.args, dict) else t.args, "n_candidates": None,
                                      "error": {"reason": e.reason, "message": e.message}})
                    if e.reason not in RETRYABLE:
                        raise
                    box.violations.append({"tool": t.name, "reason": e.reason, "message": e.message})
                    if len(box.violations) > MAX_RULE_RETRIES:
                        raise AgentFailed(e.reason, f"{e.message} (규칙 위반 {len(box.violations)}번째, "
                                                    f"다시 시도는 {MAX_RULE_RETRIES}번까지)") from e
                    out = {"error": e.reason, "message": e.message,
                           "retries_left": MAX_RULE_RETRIES - len(box.violations) + 1,
                           "hint": "이 호출은 실행되지 않았습니다. 메시지를 보고 규칙에 맞게 다시 부르세요."}
                messages.append({"role": "tool", "id": t.id, "name": t.name,
                                 "content": json.dumps(out, ensure_ascii=False, default=str)})
                if box.final is not None:
                    break
    except AgentFailed as e:
        return failed(e.reason, e.message)

    # ---------------------------------------------------------- 응답 조립 (규칙 방식과 같은 모양)
    cc, am = box.color_call, box.am
    relaxed = box.relaxed()
    areas = box.inv["areas"] if box.inv else (box.areas or [])
    colors = box.inv["colors"] if box.inv else (box.allowed_colors or [])
    n_cand = 0 if box.cand is None else int(len(box.cand))
    search.update({"steps": box.steps, "relaxed": relaxed, "areas": areas, "colors": colors,
                   "color_overlap": cc["cm"].overlap if cc and cc.get("cm") is not None else None,
                   "outfit_reference": {**box.outfit.to_dict(), "areas_removed": box.outfit_dropped},
                   "attribute_tier": box.tier,
                   "pair_combine": box.cat_call.get("combine")})

    limitations = [A.SAMPLE_WARNING, *period.warnings, A.segment_note(c)]
    if c.season_source == "inferred":
        limitations.append(f"시즌 {A._season_label(period)}은 사용자 입력이 아니라 쿼리 전처리 단계가 정한 값입니다.")
    limitations.append("LLM agent가 도구를 골라 조건을 정하고 상품을 골랐습니다 (search.steps).")
    limitations += A.pairing_notes(am, box.areas, c)
    if box.areas == []:
        limitations.append(f"제외 카테고리({', '.join(c.excluded)})를 빼니 찾을 짝 영역이 없습니다.")
    if am.basis == "all":
        limitations.append("카테고리 입력이 없어 상의·하의·아우터·원피스 전체에서 찾았습니다.")
    elif am.basis == "trend":
        limitations.append(f"사용자 카테고리가 없어 트렌드 카테고리({', '.join(am.given)})의 짝으로 찾았습니다.")
    limitations += _outfit_notes(box, areas)
    limitations += _color_notes(ti, cc)
    limitations += [_relaxed_note(r) for r in relaxed]
    limitations += box.extra_limits
    if box.violations:
        limitations.append(f"LLM이 규칙에 맞지 않는 호출을 해 다시 골랐습니다({len(box.violations)}번: "
                           + "; ".join(f"{v['tool']} {v['reason']}" for v in box.violations) + "). 자세한 내용은 search.steps")

    aid = box.final["article_id"]
    row = image = None
    if aid is not None:
        r = box.ranked[box.ranked["article_id"] == aid].iloc[0]
        row = SimpleNamespace(**r.to_dict())
        if has_image(image_dir, aid):
            image = load_image(image_dir, aid)
            limitations += take_notes(image_dir)
        else:
            limitations.append("LLM이 고른 상품에 H&M 이미지가 없어 사진 없이 반환합니다.")

    ti_used = replace(ti, colors=cc["base_colors"]) if cc else ti
    reason = None
    if row is not None:
        reason = A.build_reason(row, am, list(c.targets), ti_used, SEASON_LABELS_KO[period.season])
        if cc and cc["mode"] == "none":
            reason["color"]["rule"] = "색 조건 없음"
        elif cc and cc["mode"] == "any":
            reason["color"]["rule"] = reason["color"]["rule"].replace("어울리는 색 중 가장 많이 겹치는 색", "하나라도 어울리는 색")
        reason["attributes"] = attribute_reason(row, box.outfit, getattr(row, "tier", None) if box.tier else None)
        reason["decided_by"] = "llm"
        reason["llm_reason"] = box.final["reason"]
        reason["llm_why_not_higher"] = box.final.get("why_not_higher")    # 1위면 null

    attr = None
    if row is not None:
        shortlist = box.ranked.head(TOP_N)
        attr = {"scope": f"상위 {len(shortlist)}개 후보", **summarize_attributes(shortlist)}
    facts = {
        "season_label": A._season_label(period), "target": c.target or "카테고리 미지정",
        "area_basis": am.basis, "given_areas": am.given, "areas": areas,
        "trend_colors": ti_used.colors,
        "colors_relaxed": bool(ti.colors) and not ti_used.colors,
        "color_mode": cc["mode"] if cc else None,
        "color_label": "사용자 색" if ti.color_basis in USER_COLOR_BASES else "트렌드 색",
        "n_candidates": n_cand, "n_items": 0 if row is None else 1,
        "top1_rank": None if row is None else int(row.rank),
        "top1_category": None if row is None else to_vocab(row.kf_category),
        "top1_color": None if row is None else row.kf_color,
        "top1_stock_qty": None if row is None else int(row.stock_qty),
        "reason_detail": None if reason is None else A.reason_detail(reason),
    }
    summary, warn = A.safe_summary(A.TemplateSummarizer(), facts)
    limitations += warn

    errors = box.errors
    status = "no_results" if row is None else ("partial" if errors else "ok")
    tr = A._trend_result(ti, status, summary, search, rank=None if row is None else int(row.rank), reason=reason,
                         attribute_summary=attr, limitations=limitations, errors=errors)
    product = None if row is None else A.build_product(row, period, image)
    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds") if now.tzinfo else now.isoformat(timespec="seconds")
    resp = A._output(req, status, product, tr, season_basis,
                     evidence=A._evidence(fetched_at, period, areas, colors, box.trend_period,
                                          with_images=image is not None,
                                          outfit_ref=box.outfit.ref if box.outfit.used else None))
    problems = A.validate_output(resp)
    if problems:
        return A.error_output(req, "invalid_response", "응답 형식 검사 실패: " + "; ".join(problems))
    return resp


def run_agent(request: dict, con, llm: LLM | None, now: datetime | None = None, image_dir: str | None = None,
              max_steps: int = MAX_STEPS) -> dict:
    """요청 1건(트렌드 1개) → 응답. agent.run과 같은 입력, LLM agent로 처리."""
    from stock_agent.trend_input import read_trend
    req = request or {}
    err, c = A.check_common(req)
    if err:
        return err
    trend = req.get("trend")
    if not isinstance(trend, dict):
        return A.error_output(req, "missing_fields",
                              "필수 입력이 없습니다: trend. 02 트렌드 분석 결과의 트렌드 1개({style, items})를 "
                              f"trend 필드에 담아 주세요. {A._missing_detail(req, ['trend'])}")
    c = A.trend_common(c, trend)
    ti = read_trend(trend, list(c.target_areas), list(c.user_colors))
    return run_agent_trend(req, c, ti, trend, con, llm, now, image_dir, max_steps)
