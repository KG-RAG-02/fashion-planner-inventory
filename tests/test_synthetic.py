from datetime import date

import pandas as pd

from stock_agent.synthetic import STR_MAX, STR_MIN, UNSOLD_MAX, UNSOLD_MIN, build_stock


def _sales():
    return pd.DataFrame({
        "article_id": [f"{i:010d}" for i in range(8)],
        "segment": ["Ladieswear"] * 4 + ["Menswear"] * 4,
        "kf_area": ["하의"] * 8,
        "n_sold": [1000, 500, 10, 0, 300, 300, 5, 0],
        "last_sale": [date(2020, 9, 22), date(2020, 6, 1), date(2019, 1, 1), None,
                      date(2020, 9, 20), date(2019, 9, 1), date(2018, 10, 1), None],
        "max_price": [0.05, 0.03, 0.0169, None, 0.08, 0.08, 0.001, 0.9],
    })


def test_same_seed_same_result():
    pd.testing.assert_frame_equal(build_stock(_sales(), seed=1), build_stock(_sales(), seed=1))


def test_input_order_does_not_change_result():
    a = build_stock(_sales(), seed=1)
    b = build_stock(_sales().sample(frac=1, random_state=3), seed=1)
    pd.testing.assert_frame_equal(a, b)


def test_stock_rules():
    s = build_stock(_sales(), seed=1)
    assert (s.stock_qty >= 0).all()
    assert (s.inbound_qty == s.n_sold + s.stock_qty).all()
    sold = s[s.n_sold > 0]
    # ceil 때문에 실제 판매율은 목표값 이하이지만 범위 안
    assert sold.sell_through.between(STR_MIN - 0.01, STR_MAX).all()
    unsold = s[s.n_sold == 0]
    assert (unsold.sell_through == 0).all()
    assert unsold.stock_qty.between(UNSOLD_MIN, UNSOLD_MAX).all()
    assert (s.stock_value_krw == s.stock_qty * s.price_krw).all()
    assert (s.data_nature == "sample").all()


def test_popular_recent_items_sell_through_higher():
    s = build_stock(_sales(), seed=1).set_index("article_id")
    # 많이 팔리고 최근까지 팔린 상품 > 적게 팔리고 오래전에 끊긴 상품
    assert s.loc["0000000000", "sell_through"] > s.loc["0000000002", "sell_through"]
    # 같은 판매량이면 최근에 팔린 쪽이 높음
    assert s.loc["0000000004", "sell_through"] > s.loc["0000000005", "sell_through"]


def test_price_rules():
    s = build_stock(_sales(), seed=1).set_index("article_id")
    assert s.loc["0000000000", "price_krw"] == 50_000
    assert s.loc["0000000002", "price_krw"] == 16_900
    assert s.loc["0000000006", "price_krw"] == 3_000     # 하한
    assert s.loc["0000000007", "price_krw"] == 300_000   # 상한
    assert s.loc["0000000003", "price_krw"] == 30_000    # 가격 없음 → 같은 고객군·영역 중앙값
