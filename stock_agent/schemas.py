"""/docs 설명서용 요청·응답 형식 (pydantic)

동작 규칙은 agent.run이 정한다. 여기 모델은 설명서에 필드·예시를 보여 주는 용도라
요청은 느슨하게 받고(빠진 값·잘못된 값은 run이 400으로 알려 줌), 응답 모델은 설명서에만 쓴다.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- 요청

class SeasonIn(BaseModel):
    value: Optional[str] = Field(None, description="시즌. 예: 2027-SS, 2026 FW, 2027 봄")
    source: Optional[str] = Field(
        None, description="user = 사용자가 말함, inferred = 02 쿼리 전처리가 추정 (그 밖의 값은 unknown으로 봄)")


class InventoryRequest(BaseModel):
    """04 stock-agent 요청. 02 쿼리 전처리·03 트렌드 분석 결과를 담는다."""
    model_config = ConfigDict(extra="allow")

    schema_version: Optional[str] = Field(None, description="공통 형식 버전. 1.0", examples=["1.0"])
    request_id: Optional[str] = Field(None, description="**필수.** 응답에 그대로 돌려줌")
    season: Optional[Union[str, SeasonIn]] = Field(
        None, description="**필수.** 02가 정함. 문자열(\"2027-SS\") 또는 {value, source}")
    target_category: Optional[str] = Field(
        None, description="사용자 기획 카테고리(02). 영역(상의·하의·아우터·원피스) 또는 세부 카테고리. 있으면 이것의 짝을 찾음")
    trend_categories: Optional[Union[list[str], str]] = Field(
        None, description="트렌드 카테고리(03). 사용자 카테고리가 없을 때 짝의 교집합을 찾음")
    trend_colors: Optional[Union[list[str], str]] = Field(
        None, description="트렌드 색(03, K-Fashion 21색). 있으면 짝 카테고리 안에서 조합색으로 매치. "
                          "트렌드에 색이 없으면 []. 카테고리와 색이 모두 없으면 400")
    segment: Optional[str] = Field(
        None, description="고객군: Ladieswear, Menswear, Divided, Baby/Children, Sport. 없으면 전체")


REQUEST_EXAMPLES = {
    "user_category": {
        "summary": "기본: 사용자 카테고리 + 트렌드 색",
        "value": {"schema_version": "1.0", "request_id": "req_example_001",
                  "season": {"value": "2027-SS", "source": "user"}, "target_category": "상의",
                  "segment": "Ladieswear", "trend_colors": ["라이트핑크", "화이트"]},
    },
    "trend_categories_no_color": {
        "summary": "트렌드에 색이 없을 때: 트렌드 카테고리만",
        "value": {"schema_version": "1.0", "request_id": "req_example_002",
                  "season": {"value": "2026-FW", "source": "inferred"}, "segment": "Ladieswear",
                  "trend_categories": ["재킷", "셔츠"], "trend_colors": []},
    },
    "missing_season": {
        "summary": "오류 예: 시즌 없음 → 400",
        "value": {"schema_version": "1.0", "request_id": "req_example_003", "target_category": "상의",
                  "trend_colors": ["화이트"]},
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


class RecentTrend(BaseModel):
    weeks: int = 4
    recent_sold: Optional[int] = Field(None, description="최근 4주 판매 수")
    prev_sold: Optional[int] = Field(None, description="직전 4주 판매 수")
    basis: Optional[str] = Field(None, description="peer_relative = 또래 대비, too_few_sales = 판매 5개 미만이라 중립")
    value: Optional[float] = Field(None, description="순위에 쓴 또래 대비 추세 (-1~+1, 음수 = 또래보다 빨리 식음)")
    peer_value: Optional[float] = Field(None, description="같은 영역 후보 전체의 변화")
    raw_value: Optional[float] = Field(None, description="단순 변화 (최근 ÷ 직전 − 1)")


class Reason(BaseModel):
    pairing: dict[str, Any] = Field(description="짝 근거: basis(user/trend/all), target_category, trend_categories, product_area")
    color: dict[str, Any] = Field(description="색 근거: product_color, matched_trend_color, rule")
    stock_value_krw: Optional[int] = Field(None, description="재고 금액 (가상)")
    sell_through: Optional[float] = Field(None, description="판매율 (가상)")
    recent_trend: RecentTrend
    season_share: Optional[float] = Field(None, description="판매 중 요청 시즌 달의 비중 (실제)")
    season_name: str
    score: Optional[float] = Field(None, description="네 지표 가중 평균 0~1")
    data_nature: dict[str, str]
    text: str = Field(description="추천 이유 한 줄 (템플릿)")


class Product(BaseModel):
    style_id: str
    sku_ids: list[str]
    category: str = Field(description="K-Fashion 세부 카테고리")
    season: str
    attributes: dict[str, Any] = Field(description="color, material, fit")
    inventory: Inventory
    rank: int = Field(description="실제 순위. 1위에 사진이 없으면 2 이상일 수 있음")
    matched_trend_color: Optional[str]
    reason: Reason
    image: Optional[Image] = Field(None, description="상품 사진. 후보 전체에 사진이 없으면 null")
    evidence_ids: list[str]


class Error(BaseModel):
    code: str = Field(description="missing_fields, invalid_input, schema_version_mismatch, invalid_response, "
                                  "trend_failed, INTERNAL_ERROR")
    message: str


class InventoryResponse(BaseModel):
    """공통 envelope + InventoryProductResult (+ 04 추가 필드)"""
    schema_version: str
    request_id: Optional[str]
    agent_id: str = "stock-agent"
    status: Literal["ok", "partial", "no_results", "error"]
    summary: str
    products: list[Product] = Field(description="이미지가 있는 최상위 1개")
    attribute_summary: Optional[dict[str, Any]] = Field(None, description="상위 50개 후보의 색·소재·핏 분포")
    season_basis: Optional[dict[str, Any]] = Field(None, description="대신 쓴 과거 시즌 기간과 시즌 출처")
    findings: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    limitations: list[str]
    errors: list[Error]


class ErrorResponse(BaseModel):
    """오류 응답 (같은 envelope, products는 빈 목록)"""
    schema_version: str
    request_id: Optional[str]
    agent_id: str = "stock-agent"
    status: Literal["error"]
    summary: str
    products: list[Any] = []
    findings: list[Any] = []
    evidence: list[Any] = []
    limitations: list[str] = []
    errors: list[Error]


class Health(BaseModel):
    status: Literal["ok", "error"]
    agent_id: str
    db: str
    missing_tables: list[str]
    images: str
    images_found: bool


RESPONSES = {
    200: {"model": InventoryResponse, "description": "정상 (status: ok / partial / no_results)"},
    400: {"model": ErrorResponse, "description": "입력 오류: 필수 값 없음(missing_fields), 잘못된 값(invalid_input), "
                                                 "지원하지 않는 버전(schema_version_mismatch)"},
    500: {"model": ErrorResponse, "description": "내부 오류 (INTERNAL_ERROR)"},
}
