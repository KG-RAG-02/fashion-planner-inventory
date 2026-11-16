"""/docs 설명서용 요청·응답 형식 (pydantic)

동작 규칙은 multi.run_request(02 결과 → 내부 형식)·agent.run이 정한다. 여기 모델은 설명서에 필드·예시를 보여 주는 용도라
요청은 느슨하게 받고(빠진 값·잘못된 값은 run이 400으로 알려 줌), 응답 모델은 설명서에만 쓴다.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- 요청 (02 트렌드 결과 그대로)

class SeasonRequestIn(BaseModel):
    """02 applied_filters.season (SeasonRequest)"""
    model_config = ConfigDict(extra="allow")
    year: Optional[int] = Field(None, description="연도. 예: 2027")
    season: Optional[str] = Field(None, description="SS 또는 FW")
    raw: Optional[str] = Field(None, description="사용자가 쓴 말 (03은 쓰지 않음)")
    origin: Optional[str] = Field(None, description="user_requested = 사용자가 말함, default_next = 02가 다음 시즌으로 정함")


class CategoryRefIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    raw: Optional[str] = None
    major: Optional[str] = Field(None, description="상의 / 하의 / 아우터 / 원피스")
    minor: Optional[str] = Field(None, description="세부 카테고리 21종. 있으면 이것을 씀")


class CategoryFilterIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    include: Optional[list[CategoryRefIn]] = Field(None, description="사용자 기획 카테고리. 있으면 이것의 짝을 찾음 "
                                                                     "(여러 개면 각 짝의 교집합, 교집합이 없으면 합집합). 완화하지 않음")
    exclude: Optional[list[CategoryRefIn]] = Field(None, description="제외 카테고리. 영역이면 짝 영역에서 빼고, 세부 카테고리면 "
                                                                     "그 상품을 후보에서 뺌. include와 겹치면 400")


class AttributeIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Optional[str] = Field(None, description="color만 씀 (material·fit·pattern·detail은 쓰지 않음)")
    value: Optional[str] = Field(None, description="21색 이름. 21색이 아니면 400")


class AppliedFiltersIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    season: Optional[SeasonRequestIn] = Field(None, description="**필수.** 02가 정함")
    categories: Optional[CategoryFilterIn] = None
    gender: Optional[str] = Field(None, description="남/여/공용 → 고객군. 여 → Ladieswear+Divided, 남 → Menswear+Divided, "
                                                    "공용 → 전체. 없으면 트렌드마다 Item gender로 정함(섞이면 합침, 공용·없음이면 전체)")
    attributes: Optional[list[AttributeIn]] = Field(
        None, description="사용자 색(type=color): 트렌드 아이템 색과 겹치는 사용자 색만 쓰고, 하나도 안 겹치면 사용자 색 전부를 씀"
                          "(limitations에 적음). 없으면 트렌드 아이템 색을 씀")


class StyleIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = Field(None, description="트렌드 ID. 예: style_007")
    name: Optional[str] = Field(None, description="트렌드 이름. 예: 정제된 맥시멀리스트")


class ProductMatchIn(BaseModel):
    """02 similar_products 한 항목 (무신사 유사 상품). 03은 kind·image_url·image_ref·has_image·multi_wearer_suspected·
    score·product_id와 CLIP 쿼리 텍스트용 materials·details(type=detail)를 쓴다"""
    model_config = ConfigDict(extra="allow")
    product_id: Optional[str] = None
    image_url: Optional[str] = Field(None, description="이미지 URL (CLIP 기준 이미지). 02 착샷은 없음")
    image_ref: Optional[str] = Field(
        None, description="이미지 참조. 착샷은 hf://datasets/disl/musinsa-snap-new@<revision>#row=<n> → 03이 R2 착샷 JPEG로 읽음")
    multi_wearer_suspected: Optional[bool] = Field(None, description="true면 여러 명이 찍혔을 수 있는 착샷 → 기준 이미지로 쓰지 않음")
    kind: Optional[str] = Field(None, description="snap_garment(무신사 스냅 착샷)를 먼저 쓰고, 없으면 catalog_item(무신사 상품 이미지)")
    score: Optional[float] = Field(None, description="트렌드 아이템과의 일치도. 같은 kind 안에서 가장 높은 1장을 씀")


class ItemIn(BaseModel):
    """02 트렌드 아이템. 03은 category_major·category_minor·colors·gender·similar_products를 쓴다"""
    model_config = ConfigDict(extra="allow")
    category_major: Optional[str] = Field(None, description="상의 / 하의 / 아우터 / 원피스")
    category_minor: Optional[str] = Field(None, description="세부 카테고리 21종. 예: 셔츠, 재킷")
    colors: Optional[list[str]] = Field(None, description="21색. 순서는 의미 없음")
    gender: Optional[str] = Field(None, description="요청 성별이 없을 때 고객군을 정하는 데 씀")
    similar_products: Optional[list[ProductMatchIn]] = Field(
        None, description="무신사 유사 상품. 사용자 카테고리 아이템(없으면 아이템 전체)에서 snap_garment 착샷(없으면 catalog_item "
                          "상품 이미지) 중 score가 가장 높은 1장을 기준으로 재고 후보를 소재·질감·디테일 유사도(CLIP: 흑백 이미지 + "
                          "소재·디테일 텍스트, 색 제외)로 거름(재고 색마다 유사도 상위 10%, 색마다 최소 1개). 둘 다 없으면 CLIP 필터 없이 찾음")


class TrendIn(BaseModel):
    """02 결과의 trends[] 한 항목 그대로"""
    model_config = ConfigDict(extra="allow")
    style: Optional[StyleIn] = None
    items: Optional[list[ItemIn]] = Field(
        None, description="트렌드 색(사용자 색이 없을 때): 사용자 카테고리 아이템의 색 → 없으면 아이템 전체의 색 → 없으면 색 조건 없음. "
                          "트렌드 카테고리: 아이템의 category_minor (없으면 category_major)")


class InventoryRequest(BaseModel):
    """03 stock-agent 요청 = 02 트렌드 결과(TrendAnalysisResult, 전처리된 쿼리 조건 applied_filters 포함) 그대로.
    applied_filters가 없으면 400. 입구(stock_agent/trend_request.py)에서 내부 형식으로 바꾼다."""
    model_config = ConfigDict(extra="allow")

    schema_version: Optional[str] = Field(None, description="**필수.** Trend producer schema 1.1")
    request_id: Optional[str] = Field(None, description="**필수.** 응답에 그대로 돌려줌")
    status: Optional[str] = Field(None, description="**필수.** ok → 처리 / no_results → 조회하지 않고 no_results / "
                                                    "error → 400 INVALID_REQUEST(reason=upstream_error)")
    applied_filters: Optional[AppliedFiltersIn] = Field(None, description="**필수.** 02가 적용한 사용자 조건")
    trends: Optional[list[TrendIn]] = Field(
        None, description="**필수.** 트렌드 목록 (1개여도 목록). 트렌드마다 상품 1개씩, 순서대로 돌려줌")
    evidence: Optional[list[Any]] = Field(None, description="02 근거 (03은 쓰지 않음)")
    limitations: Optional[list[str]] = Field(None, description="02 한계. status=no_results·error일 때 응답 summary에 옮김")


_BALLET = {"style": {"id": "style_012", "name": "발레코어", "definition": "발레복에서 온 가볍고 여린 실루엣"},
           "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"], "gender": "여"},
                     {"category_major": "하의", "category_minor": "스커트", "colors": ["화이트"], "gender": "여"}]}
_MAXI = {"style": {"id": "style_007", "name": "정제된 맥시멀리스트"},
         "items": [{"category_major": "아우터", "category_minor": "재킷", "colors": []},
                   {"category_major": "상의", "category_minor": "셔츠", "colors": []}]}

# 02 트렌드 결과(TrendAnalysisResult) 그대로 (similar_products 등은 줄임). contracts/examples/request-trend-result.json과 같다
TREND_RESULT_EXAMPLE = {
    "schema_version": "1.1",
    "request_id": "req_example_010",
    "status": "ok",
    "applied_filters": {
        "season": {"year": 2027, "season": "SS", "raw": "내년 봄여름", "origin": "user_requested"},
        "categories": {"include": [{"raw": "블라우스", "major": "상의", "minor": "블라우스"}], "exclude": []},
        "gender": "여",
        "attributes": [{"type": "color", "value": "화이트"}],
    },
    "trends": [{
        "style": {"id": "style_012", "name": "발레코어", "name_origin": "article", "seasons": ["2027SS"],
                  "definition": "발레복에서 온 가볍고 여린 실루엣",
                  "description": [{"text": "시폰·새틴 소재와 리본 디테일", "evidence_ids": ["ev_01"]}],
                  "focus_core": ["color", "material"], "focus_supporting": [], "focus_evidence_ids": []},
        "items": [
            {"id": "item_031", "name": "리본 블라우스", "category_major": "상의", "category_minor": "블라우스",
             "colors": ["핑크", "화이트"], "materials": ["시폰", "실크"], "fit": "루즈", "gender": "여",
             "approx": ["material:실크"], "evidence_ids": ["ev_01"], "similar_products": []},   # 원문 새틴 → 실크(근사)
            {"id": "item_032", "name": "튤 스커트", "category_major": "하의", "category_minor": "스커트",
             "colors": ["화이트"], "materials": [], "fit": None, "gender": "여", "similar_products": []},
        ],
    }],
    "evidence": [],
    "limitations": [],
}


def _example(request_id, trends, **kw):
    from stock_agent.trend_request import build_trend_result
    return build_trend_result(request_id, trends, **kw)


REQUEST_EXAMPLES = {
    "one_trend": {
        "summary": "트렌드 1개: 사용자 카테고리·색·성별 (소재 approx 값은 쓰지 않음)",
        "value": TREND_RESULT_EXAMPLE,
    },
    "multi_trends": {
        "summary": "트렌드 여러 개: 트렌드마다 1개씩, 순서대로",
        "value": _example("req_example_002", [_MAXI, _BALLET], include=["상의"], gender="여"),
    },
    "no_user_category": {
        "summary": "사용자 카테고리·성별 없음: 트렌드 카테고리의 짝, 고객군은 Item 성별, 시즌은 02가 정함",
        "value": _example("req_example_003", [_BALLET], year=2026, season="FW", origin="default_next"),
    },
    "upstream_no_results": {
        "summary": "02 결과 없음: 조회하지 않고 no_results (200)",
        "value": {**_example("req_example_004", []), "status": "no_results",
                  "limitations": ["2027SS에 해당하는 트렌드가 없습니다."]},
    },
    "missing_season": {
        "summary": "오류 예: 시즌 없음 → 400 INVALID_REQUEST",
        "value": _example("req_example_005", [_BALLET], season=None, include=["상의"]),
    },
}


# ---------------------------------------------------------------- 응답 (설명서용)

class Inventory(BaseModel):
    quantity: int = Field(description="재고량 (가상)")
    unit: str = "pieces"
    as_of: str = Field(description="데이터 기준일 2020-09-22")
    definition: str = "virtual_stock_v1"
    data_nature: str = "sample"


class Image(BaseModel):
    media_type: str = "image/jpeg"
    encoding: str = "base64"
    data: str = Field(description="사진 JPEG를 base64로 바꾼 문자열")
    size_bytes: int
    source: str = Field(description="H&M images/<앞 3자리>/<article_id>.jpg")
    data_nature: str = "fact"


class Product(BaseModel):
    """상품 정보만. 순위·이유는 같은 위치의 trend_results에"""
    style_id: str = Field(description="article_id 앞 7자리 (디자인)")
    sku_ids: list[str] = Field(description="[article_id] (디자인 + 색)")
    category: str = Field(description="세부 카테고리 (트렌드 팀 vocabulary_v1)")
    season: str
    attributes: dict[str, Any] = Field(description="color, material(vocabulary_v1, 예: 울/캐시미어), fit")
    inventory: Inventory
    image: Optional[Image] = Field(None, description="상품 사진. 후보 전체에 사진이 없으면 null")


class RecentTrend(BaseModel):
    weeks: int = 4
    recent_sold: Optional[int] = Field(None, description="최근 4주 판매 수")
    prev_sold: Optional[int] = Field(None, description="직전 4주 판매 수")
    basis: Optional[str] = Field(None, description="peer_relative = 또래 대비, too_few_sales = 판매 5개 미만이라 중립")
    value: Optional[float] = Field(None, description="순위에 쓴 또래 대비 추세 (-1~+1, 음수 = 또래보다 빨리 식음)")
    peer_value: Optional[float] = Field(None, description="같은 영역 후보 전체의 변화")
    raw_value: Optional[float] = Field(None, description="단순 변화 (최근 ÷ 직전 − 1)")


class Reason(BaseModel):
    pairing: dict[str, Any] = Field(description="짝 근거: basis(user/trend/all), target_categories, trend_categories, product_area")
    color: dict[str, Any] = Field(description="색 근거: product_color, matched_trend_colors, "
                                              "trend_color_basis(user_colors_in_trend/user_colors/user_category_items/all_items/none), rule")
    stock_value_krw: Optional[int] = Field(None, description="재고 금액 (가상)")
    sell_through: Optional[float] = Field(None, description="판매율 (가상)")
    recent_trend: RecentTrend
    season_share: Optional[float] = Field(None, description="판매 중 요청 시즌 달의 비중 (실제)")
    season_name: str
    score: Optional[float] = Field(None, description="네 지표 가중 평균 0~1")
    data_nature: dict[str, str]


class Error(BaseModel):
    code: Literal["INVALID_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "SOURCE_UNAVAILABLE", "TIMEOUT", "INTERNAL_ERROR"] = Field(
        description="팀 공통 오류 코드 (stock_agent/errors.py)")
    message: str
    reason: Optional[str] = Field(None, description="03 안쪽 원인: missing_fields, invalid_input, schema_version_mismatch, "
                                                    "upstream_error, db_unavailable, image_source_unavailable, timeout, "
                                                    "invalid_response, trend_failed, internal. 전체 오류에서 원인이 여러 개면 mixed")


class TrendResult(BaseModel):
    style: dict[str, Optional[str]] = Field(description="02 style의 id, name")
    status: Literal["ok", "partial", "no_results", "error"]
    product_index: int = Field(description="products 안의 위치 (= 이 항목의 위치)")
    rank: Optional[int] = Field(None, description="실제 순위. 1위에 사진이 없으면 2 이상일 수 있음")
    reason: Optional[Reason] = None
    summary: str = Field(description="추천 이유 한 줄 (템플릿)")
    search: dict[str, Any] = Field(description="이 트렌드로 찾은 조건: trend_colors(기준 색), trend_color_basis, user_colors, user_colors_excluded, trend_categories, "
                                               "areas(짝 영역), colors(최종 조회 색), color_mode(max_overlap/any/none), color_overlap, "
                                               "color_attempts(조회한 색 단계별 mode·colors·n_candidates), relaxed(완화한 트렌드 색 조건), "
                                               "excluded_categories, outfit_reference(CLIP 기준 이미지: status used/skipped, "
                                               "skip_reason no_reference/no_index/no_match/no_model/no_query/no_candidates, kind snap_garment/catalog_item, "
                                               "product_id, image_url, image_ref, score, source, item_id, item_category), clip(applied, skip_reason, "
                                               "image_used, text_used, n_before, n_after, n_indexed, keep_ratio, keep_rule, image_skip; 기준 이미지가 없으면 null), segments(찾은 고객군, []=전체), "
                                               "segment_basis(segment/gender/item_gender/all)")
    limitations: list[str]
    errors: list[Error] = []
    evidence_ids: list[str] = Field(description="이 트렌드의 근거 ID. 내용은 응답 evidence에서 찾음 (오류 트렌드는 [])")


class InventoryResponse(BaseModel):
    """03 응답. products[i] ↔ trend_results[i]. 공통 envelope의 findings·최상위 limitations는 쓰지 않음"""
    schema_version: str
    request_id: Optional[str]
    agent_id: str = "stock-agent"
    status: Literal["ok", "partial", "no_results", "error"]
    summary: str
    products: list[Optional[Product]] = Field(description="트렌드 순서대로 n개. 상품을 못 고른 트렌드는 null. 중복 허용")
    trend_results: list[TrendResult] = Field(description="트렌드 순서대로 n개")
    season_basis: Optional[dict[str, Any]] = Field(None, description="대신 쓴 과거 시즌 기간과 시즌 출처")
    evidence: list[dict[str, Any]] = Field(description="트렌드마다 데이터 출처. evidence_id = stock-agent:<style.id>:E-001, style_id")
    errors: list[Error] = Field(description="공통 입력 오류. 트렌드별 오류는 trend_results[].errors")
    input_provenance: Optional[dict[str, Any]] = Field(
        None, description="Trend source version and the explicit Stock input-adapter provenance"
    )


class ErrorResponse(BaseModel):
    """오류 응답 (같은 envelope)"""
    schema_version: str
    request_id: Optional[str]
    agent_id: str = "stock-agent"
    status: Literal["error"]
    summary: str
    products: list[Any] = []
    trend_results: list[Any] = []
    evidence: list[Any] = []
    errors: list[Error]


class HandoffProducer(BaseModel):
    agent_id: str = "stock-agent"
    schema_version: str = Field(description="Stock producer result schema version; currently 1.0")


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["vogue-handoff/0.1"]
    request_id: str = Field(min_length=1)
    input: dict[str, Any] = Field(description="Unchanged TrendAnalysisResult producer object")


class HandoffResponse(BaseModel):
    contract_version: Literal["vogue-handoff/0.1"] = "vogue-handoff/0.1"
    request_id: str
    producer: HandoffProducer
    status: Literal["ok", "partial", "no_results", "error"]
    result: InventoryResponse


RESPONSES = {
    200: {"model": InventoryResponse, "description": "정상 (status: ok / partial / no_results)"},
    400: {"model": ErrorResponse, "description": "INVALID_REQUEST: 필수 값 없음, 잘못된 값, 지원하지 않는 버전, 02 status=error. "
                                                 "원인은 errors[].reason"},
    500: {"model": ErrorResponse, "description": "INTERNAL_ERROR: 내부 오류"},
    503: {"model": ErrorResponse, "description": "SOURCE_UNAVAILABLE: DB 연결 실패(reason=db_unavailable), "
                                                 "이미지 자료원 조회 실패(reason=image_source_unavailable)"},
    504: {"model": ErrorResponse, "description": "TIMEOUT: HTTP 처리 제한 시간(STOCK_HANDOFF_TIMEOUT_SECONDS) 초과"},
}
