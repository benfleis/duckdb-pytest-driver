from ducktest5 import test


@test()
def solo_body(ctx):
    ctx.duckdb().expects("SELECT 42 AS answer", [(42,)])
