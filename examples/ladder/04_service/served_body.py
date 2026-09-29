from ducktest5 import test


@test()
def served_body(ctx):
    ctx.duckdb().expects(f"SELECT count(*) FROM read_csv('{ctx.roots['data_dir']}/cities.csv')", [(3,)])
