from ducktest import test


@test()
def solo_body(ctx):
    ctx.client("duckdb").expects("SELECT 42 AS answer", [(42,)])
