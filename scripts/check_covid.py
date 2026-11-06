import duckdb

con = duckdb.connect("data/stock.duckdb", read_only=True)

con.sql("""
SELECT year(t_dat) AS 연도, month(t_dat) AS 월,
       ROUND(AVG(CASE WHEN sales_channel_id = 2 THEN 1 ELSE 0 END) * 100, 1) AS "채널2 비중(%)"
FROM transactions
WHERE month(t_dat) BETWEEN 1 AND 8 AND year(t_dat) IN (2019, 2020)
GROUP BY 1, 2 ORDER BY 2, 1
""").show()