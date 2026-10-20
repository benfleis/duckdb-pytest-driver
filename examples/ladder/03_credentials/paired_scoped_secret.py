from ducktest import test


@test()
def paired_scoped_secret(ctx):
    keys = ctx.credential("fake_key")
    ctx.client("duckdb").expects(
        "SELECT name, type, scope FROM duckdb_secrets()", [(keys.secret_name, keys.type, keys.scope)]
    )
    ctx.run_paired()
