import duckdb

con = duckdb.connect("data/stock.duckdb", read_only=True)
df = con.execute("""
    SELECT index_group_name, product_group_name, product_type_name, COUNT(*) AS n
    FROM articles
    GROUP BY ALL
    ORDER BY index_group_name, n DESC
""").df()

print(df.to_string())
print("행 수:", len(df))
df.to_csv("data/product_types.csv", index=False, encoding="utf-8-sig")