"""가상 재고 v1 (#D) — 결과는 모두 data_nature = "sample"

H&M에는 재고·입고량·원화 가격이 없다. 실제 판매 기록을 바탕으로 규칙에 따라 만든다.

규칙
1. 판매율(sell_through) 0.30 ~ 0.95
   - 같은 고객군·영역 안에서 많이 팔린 상품일수록(인기 순위), 마지막 판매일이
     기준일에 가까울수록(최근성) 높다. 둘을 반씩 섞고 작은 무작위 값을 더한다.
   - 판매율 = 0.30 + 0.65 × clip(0.5×인기 + 0.5×최근성 + 잡음, 0, 1)
2. 입고량 = ceil(누적 판매량 ÷ 판매율),  재고 = 입고량 − 누적 판매량 (음수 없음)
3. 한 번도 안 팔린 상품: 입고량 20~200개 무작위, 재고 = 입고량, 판매율 0
4. 가상 판매가(원) = 상품 최고 정규화 가격 × 1,000,000, 100원 단위 반올림, 3,000 ~ 300,000원
   (가격이 낮게 찍힌 거래는 할인으로 보고 최고가를 정가로 씀)
   안 팔려서 가격이 없는 상품은 같은 고객군·영역의 중앙값 가격을 씀
5. 재고 금액 = 재고 × 가상 판매가
6. 랜덤 시드 고정 → 같은 시드면 같은 결과

transactions 한 행 = 상품 1개 판매로 센다.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

VERSION = "virtual_stock_v1"
SEED = 20261105
AS_OF = date(2020, 9, 22)

STR_MIN, STR_MAX = 0.30, 0.95
NOISE_SD = 0.05
RECENCY_DAYS = 365
UNSOLD_MIN, UNSOLD_MAX = 20, 200
PRICE_SCALE = 1_000_000
PRICE_MIN, PRICE_MAX = 3_000, 300_000


def build_stock(sales: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    """상품별 판매 집계 → 가상 재고 표.

    sales 컬럼: article_id, segment, kf_area, n_sold(int, 0 가능),
               last_sale(date 또는 NaT), max_price(float 또는 NaN)
    """
    df = sales.sort_values("article_id").reset_index(drop=True).copy()
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, NOISE_SD, len(df))
    unsold_inbound = rng.integers(UNSOLD_MIN, UNSOLD_MAX + 1, len(df))

    sold = df["n_sold"] > 0

    # 인기: 같은 고객군·영역 안에서 판매량 백분위 (팔린 상품끼리 비교)
    pop = pd.Series(0.0, index=df.index)
    pop[sold] = df[sold].groupby(["segment", "kf_area"])["n_sold"].rank(pct=True)

    # 최근성: 기준일에 마지막으로 팔렸으면 1, 1년 이상 전이면 0
    last = pd.to_datetime(df["last_sale"])
    days = (pd.Timestamp(AS_OF) - last).dt.days.fillna(RECENCY_DAYS)
    recency = 1 - days.clip(0, RECENCY_DAYS) / RECENCY_DAYS

    base = (0.5 * pop + 0.5 * recency + noise).clip(0, 1)
    sell_through = (STR_MIN + (STR_MAX - STR_MIN) * base).round(4)

    inbound = np.where(sold, np.ceil(df["n_sold"] / sell_through), unsold_inbound).astype(int)
    stock = inbound - df["n_sold"].to_numpy()

    group_median = df.groupby(["segment", "kf_area"])["max_price"].transform("median")
    price = (df["max_price"].fillna(group_median).fillna(0) * PRICE_SCALE).round(-2).clip(PRICE_MIN, PRICE_MAX).astype(int)

    out = pd.DataFrame({
        "article_id": df["article_id"],
        "n_sold": df["n_sold"].astype(int),
        "inbound_qty": inbound,
        "stock_qty": stock.astype(int),
        "sell_through": np.where(sold, df["n_sold"] / inbound, 0.0).round(4),
        "price_krw": price,
    })
    out["stock_value_krw"] = out["stock_qty"].astype("int64") * out["price_krw"]
    out["as_of"] = AS_OF
    out["version"] = VERSION
    out["data_nature"] = "sample"
    return out
