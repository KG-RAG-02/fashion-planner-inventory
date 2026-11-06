"""테스트 공용 fixture: 작은 DuckDB (duckdb가 없으면 해당 테스트는 건너뜀)"""

import pytest


@pytest.fixture
def con():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect()
    c.execute("""CREATE TABLE article_attrs AS SELECT * FROM (VALUES
        ('a1','Ladieswear','하의','청바지','스카이블루',['데님'],'와이드',['봄','여름','가을','겨울']),
        ('a2','Ladieswear','하의','스커트','레드',[]::VARCHAR[],NULL,['봄','여름','가을','겨울']),
        ('a3','Ladieswear','아우터','패딩','베이지',[]::VARCHAR[],NULL,['가을','겨울']),
        ('a4','Menswear','하의','팬츠','그레이',['면'],NULL,['봄','여름','가을','겨울']),
        ('a5','Ladieswear','상의','탑','그레이',[]::VARCHAR[],NULL,['봄','여름','가을','겨울']),
        ('a6','Ladieswear','하의','팬츠','베이지',[]::VARCHAR[],NULL,['봄','여름','가을','겨울'])
      ) v(article_id,segment,kf_area,kf_category,kf_color,materials,fit,season_fit)""")
    c.execute("""CREATE TABLE stock_v1 AS SELECT * FROM (VALUES
        ('a1',100,30000,3000000,0.4,200),('a2',100,30000,3000000,0.4,200),
        ('a3',100,30000,3000000,0.4,200),('a4',100,30000,3000000,0.4,200),
        ('a5',100,30000,3000000,0.4,200),('a6',0,30000,0,0.9,200)
      ) v(article_id,stock_qty,price_krw,stock_value_krw,sell_through,n_sold)""")
    c.execute("""CREATE TABLE transactions AS SELECT * FROM (VALUES
        (DATE '2019-04-01','a1'),(DATE '2019-04-02','a1'),(DATE '2019-10-01','a1'),
        (DATE '2020-09-20','a1'),(DATE '2020-08-01','a1'),(DATE '2020-08-02','a4')
      ) v(t_dat, article_id)""")
    return c
