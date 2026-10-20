from ducktest import test


@test()
def served_body(ctx):
    ctx.client("duck").expects("SELECT count(*) FROM read_csv('{DATA_DIR}/cities.csv')", [(3,)])
