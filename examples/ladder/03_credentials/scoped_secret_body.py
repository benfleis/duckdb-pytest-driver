from ducktest5 import test


@test()
def scoped_secret_body(ctx):
    held = ctx.credentials()
    assert [c.id for c in held] == ["fake_key"]
    ctx.client("duckdb").expects(
        "SELECT name, type, scope FROM duckdb_secrets()", [(c.secret_name, c.type, c.scope) for c in held]
    )
