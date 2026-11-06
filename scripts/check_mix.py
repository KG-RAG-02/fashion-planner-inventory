import duckdb

con = duckdb.connect("data/stock.duckdb", read_only=True)

con.sql("""
WITH s AS (
    SELECT year(t.t_dat) AS y, a.product_type_name AS typ, COUNT(*) AS n
    FROM transactions t
    JOIN articles a USING (article_id)
    WHERE month(t.t_dat) BETWEEN 3 AND 5
      AND year(t.t_dat) IN (2019, 2020)
    GROUP BY 1, 2
),
share AS (
    SELECT y, typ, n * 100.0 / SUM(n) OVER (PARTITION BY y) AS pct FROM s
)
SELECT typ AS 상품타입,
       ROUND(MAX(pct) FILTER (WHERE y = 2019), 2) AS "2019 봄(%)",
       ROUND(MAX(pct) FILTER (WHERE y = 2020), 2) AS "2020 봄(%)",
       ROUND(MAX(pct) FILTER (WHERE y = 2020) - MAX(pct) FILTER (WHERE y = 2019), 2) AS "차이(%p)"
FROM share
WHERE typ IN ('Jacket', 'Coat', 'Blazer', 'Outdoor Waistcoat', 'Outdoor overall', 'Cardigan')
GROUP BY 1
ORDER BY "2019 봄(%)" DESC
""").show()