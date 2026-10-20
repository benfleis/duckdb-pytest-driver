from ducktest import test


@test()
def solo_body(ctx):
    ctx.client("duck").expects("SELECT 42 AS answer", [(42,)])
