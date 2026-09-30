from ducktest5 import test


@test()
def served_body(ctx):
    ctx.duckdb().expects("SELECT count(*) FROM read_csv('{DATA_DIR}/cities.csv')", [(3,)])
